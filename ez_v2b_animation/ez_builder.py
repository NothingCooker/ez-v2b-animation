# -*- coding: utf-8 -*-
# ---------------------------------------------------------------------------
# ez-v2b-animation  ——  作者：B站 @高压郭炖大葱
# 使用、修改、二次分发请保留本署名。
# ---------------------------------------------------------------------------

"""ez-v2b-animation :: 一键构建器

对任意遵循常规规则的人形骨架，一键补齐：
  IK 手柄 / 极向 / IK 约束 / 链末端旋转 / 头部 TRACK_TO
  IKFK 切换器 + 驱动 / 前臂扭转传递 / 控制器形状
  Root + Hips 控制器 / 骨骼集合分组 / rig_profile

三条铁律（来自 模型Rig优化工作流.md）：
  1. 幂等 —— 所有新建前先按名字清理旧产物，重复运行不叠加
  2. 有数值判据 —— 静置偏差 / IK 跟随率 / 约束完整性，全部实测
  3. 默认行为不变 —— 已存在的 pole_angle / chain_count / use_stretch 一律保留

不写死骨骼名：所有骨骼引用来自 ez_discovery 的结构发现结果。
"""

import bpy
import bmesh
import math
import json
import os
import sys
from mathutils import Vector, Matrix

# 子模块导入：包内相对导入优先，失败时按"同目录文件"直接加载。
# 为什么要这么绕：本插件有三种加载方式（插件包 / 单文件 exec / blend 文本块），
# 只有第一种有正常的包上下文。实测直接写 `import ez_discovery` 会在插件包模式下
# 抛 ModuleNotFoundError（子目录不在 sys.path 里）。
def _load_sibling(name):
    try:
        return __import__(name, globals(), locals(), ["*"], 1)   # 相对导入
    except Exception:
        pass
    try:
        return __import__(name)                                   # 绝对导入
    except Exception:
        pass
    here = None
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        here = None
    if here:
        p = os.path.join(here, name + ".py")
        if os.path.isfile(p):
            import importlib.util as _ilu
            spec = _ilu.spec_from_file_location(name, p)
            mod = _ilu.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            return mod
    raise ImportError("无法加载子模块 %s" % name)


D = _load_sibling("ez_discovery")
M = _load_sibling("ez_metrics")
TEX = _load_sibling("ez_texture")

# ================================================================ 常量

PROFILE_KEY = "rig_profile"
PROFILE_VER = "rig_profile_version"
MARK = "ez_v2b"                       # 标记本插件创建的数据，便于幂等清理
WGT_PREFIX = "WGT-ez-"
COLL_SHAPE_SUFFIX = "-形状"
COLL_CTRL_SUFFIX = "-控制器"
COLL_PHYS_SUFFIX = "-物理代理"

TWIST_CNAME = "扭转传递"
TWIST_DEFAULT = 0.4

BONE_COLL = {
    "ctrl":   "IK-控制器",
    "switch": "IK-切换器",
    "main":   "FK-主体",
    "finger": "FK-手指",
    "hair":   "装饰-头发",
    "deco":   "装饰-裙摆件",
}
BONE_COLL_VISIBLE = {"switch": True, "main": True}
DEFAULT_VISIBLE = False


# ================================================================ 求值工具

def upd(n=4):
    """改完属性/约束后必须刷求值，否则读到旧缓存（见文档铁律二）"""
    for _ in range(n):
        bpy.context.view_layer.update()
        bpy.context.evaluated_depsgraph_get().update()


def unhide_all():
    """解除所有隐藏与 ViewLayer 排除 —— 否则约束静默失效、读数恒为 0"""
    for c in bpy.data.collections:
        c.hide_viewport = False
    for o in bpy.data.objects:
        o.hide_viewport = False
    vl = bpy.context.view_layer

    def walk(lc):
        lc.exclude = False
        lc.hide_viewport = False
        for ch in lc.children:
            walk(ch)
    walk(vl.layer_collection)
    upd(2)


def pose_dev(arm, bone_name):
    """当前姿态相对 rest 的位置偏差（世界单位）"""
    dg = bpy.context.evaluated_depsgraph_get()
    e = arm.evaluated_get(dg)
    pb = e.pose.bones.get(bone_name)
    b = arm.data.bones.get(bone_name)
    if pb is None or b is None:
        return None
    return (pb.head - b.head_local).length * arm.matrix_world.to_scale().x


def rest_world_head(arm, bone_name):
    b = arm.data.bones.get(bone_name)
    if b is None:
        return None
    return arm.matrix_world @ b.head_local


def rest_world_tail(arm, bone_name):
    b = arm.data.bones.get(bone_name)
    if b is None:
        return None
    return arm.matrix_world @ b.tail_local


# ================================================================ 编辑模式工具

class EditBones:
    """安全的编辑模式上下文：进入时确保对象激活，退出后不保留 Bone 引用
    （文档 3.6：编辑模式会重建层级，旧 Bone 引用全部失效）"""

    def __init__(self, arm):
        self.arm = arm

    def __enter__(self):
        for o in bpy.context.view_layer.objects:
            o.select_set(False)
        bpy.context.view_layer.objects.active = self.arm
        self.arm.select_set(True)
        if self.arm.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        bpy.ops.object.mode_set(mode="EDIT")
        return self.arm.data.edit_bones

    def __exit__(self, *a):
        bpy.ops.object.mode_set(mode="OBJECT")
        upd(2)
        return False


def eb_make(eb, name, head, tail, parent=None):
    b = eb.get(name)
    if b is None:
        b = eb.new(name)
    b.head = Vector(head)
    b.tail = Vector(tail)
    b.roll = 0.0
    b.parent = eb[parent] if parent else None
    b.use_connect = False
    b.use_deform = False
    return b


def eb_exists(eb, name):
    return eb.get(name) is not None


# ================================================================ 手柄几何规则（实测反推）

def elbow_knee_side(arm, upper, lower, tip):
    """从 rest 姿态反推关节弯曲侧（本地坐标向量）。

    实测规则（模型Rig优化工作流.md 二·五 第三步）：
      上臂方向与"肘->腕"方向的 Z 分量符号相反 => 肘朝哪一侧弯
      膝盖相对"髋—踝连线"高多少              => 膝朝哪一侧凸
    返回 (轴向单位向量, 该侧的"深度"标量)，轴取本地坐标的主导轴。
    """
    hu = arm.data.bones.get(upper)
    hl = arm.data.bones.get(lower)
    ht = arm.data.bones.get(tip)
    if not (hu and hl and ht):
        return None, 0.0
    # 关节在本地空间相对"上段-下段连线"的偏移
    a = hu.head_local
    b = ht.head_local
    mid = hl.head_local
    seg = b - a
    if seg.length < 1e-9:
        return None, 0.0
    t = (mid - a).dot(seg) / seg.dot(seg)
    proj = a + seg * t
    off = mid - proj                      # 关节相对弦的偏移（弯曲方向）
    if off.length < 1e-9:
        return None, 0.0
    return off.normalized(), off.length


def pole_offset_vector(arm, upper, lower, tip, distance):
    """极向骨骼相对关节的偏移向量（本地坐标）"""
    axis, depth = elbow_knee_side(arm, upper, lower, tip)
    if axis is None:
        return None
    return axis * distance


# ================================================================ 极向角度扫描

def scan_pole_angle(arm, ik_entry, candidates=None):
    """扫描 pole_angle，取静置偏差最小者。

    实测依据（_rusk_work/scan_pole.txt）：
      腿  -π/2 -> 0.000020    0 -> 1.581626
      臂  -π/2 -> 0.001212    0 -> 0.226286
    返回 (最佳角度, [(角度, 偏差), ...])
    """
    if candidates is None:
        candidates = [-math.pi, -3 * math.pi / 4, -math.pi / 2, -math.pi / 4,
                      0.0, math.pi / 4, math.pi / 2, 3 * math.pi / 4, math.pi]
    pb = arm.pose.bones.get(ik_entry["ik"])
    if pb is None:
        return 0.0, []
    con = None
    for c in pb.constraints:
        if c.type == "IK":
            con = c
            break
    if con is None:
        return 0.0, []

    orig = con.pole_angle
    results = []
    best_a, best_d = orig, float("inf")
    for a in candidates:
        con.pole_angle = a
        upd(8)
        d = pose_dev(arm, ik_entry["ik"]) or float("inf")
        results.append((a, d))
        if d < best_d:
            best_d, best_a = d, a
    con.pole_angle = orig
    upd(6)
    return best_a, results


# ================================================================ 形状网格

def _new_mesh_obj(name, build, coll):
    old = bpy.data.objects.get(name)
    if old is not None:
        me = old.data
        bpy.data.objects.remove(old, do_unlink=True)
        if me is not None and me.users == 0:
            bpy.data.meshes.remove(me)
    me = bpy.data.meshes.new(name + "_mesh")
    bm = bmesh.new()
    build(bm)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    coll.objects.link(ob)
    # custom_shape 素材对象必须隐藏，否则 0.01 骨架缩放下会成为场景巨物（文档 5.1）
    ob.hide_viewport = True
    ob.hide_render = True
    ob.hide_select = True
    ob[MARK] = True
    return ob


def _mk_ring(R, r, seg=20, ring=8):
    def f(bm):
        idx = []
        for i in range(seg):
            a = 2 * math.pi * i / seg
            for j in range(ring):
                b = 2 * math.pi * j / ring
                idx.append(bm.verts.new(((R + r * math.cos(b)) * math.cos(a),
                                         r * math.sin(b),
                                         (R + r * math.cos(b)) * math.sin(a))))
        for i in range(seg):
            for j in range(ring):
                bm.faces.new((idx[i * ring + j],
                              idx[i * ring + (j + 1) % ring],
                              idx[((i + 1) % seg) * ring + (j + 1) % ring],
                              idx[((i + 1) % seg) * ring + j]))
    return f


def _mk_box(sx, sy, sz, oy=0.0, oz=0.0):
    def f(bm):
        bmesh.ops.create_cube(bm, size=1.0)
        bmesh.ops.scale(bm, vec=(sx, sy, sz), verts=bm.verts)
        bmesh.ops.translate(bm, vec=(0.0, oy, oz), verts=bm.verts)
    return f


def _mk_sphere(r):
    def f(bm):
        bmesh.ops.create_icosphere(bm, subdivisions=2, radius=r)
    return f


def _mk_cyl(r, h, seg=12):
    def f(bm):
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=seg,
                              radius1=r, radius2=r, depth=h)
        bmesh.ops.rotate(bm, verts=bm.verts, cent=(0, 0, 0),
                         matrix=Matrix.Rotation(math.radians(90), 3, "X"))
    return f


def _mk_ring_torus(R, r):
    return _mk_ring(R, r)


def build_shapes(arm, coll, height, size_scale=1.0):
    """建控制器形状。

    尺寸换算（实测修正）：
      custom_shape 在**骨骼局部空间**渲染，骨骼局部空间与世界空间之间隔着
      "骨骼数据里的缩放"（不是 matrix_world —— 实测三台角色的 matrix_world 都是单位阵，
      而 Chocolate 的真实身高是 45.7、本地坐标是 456）。

      所以本地尺寸 = 目标世界尺寸 / (真实身高 / 参考身高) ... 这是错的。

      正确做法：本地尺寸 = 目标世界尺寸 × (骨骼本地单位 / 世界单位)。
      该比例由 ez_metrics 用"脚->头 的世界距离 vs 本地距离"实测得出。
    """
    # 本地单位 -> 世界单位的换算系数
    sc = M.local_to_world(arm)
    U = (height / 1.6) if height > 1e-6 else 1.0
    K = (1.0 / sc) * size_scale * U

    defs = {
        "hand":   _mk_ring(0.045 * K, 0.008 * K),
        "foot":   _mk_box(0.055 * K, 0.13 * K, 0.022 * K,
                          oy=-0.03 * K, oz=-0.02 * K),
        "pole":   _mk_sphere(0.018 * K),
        "switch": _mk_cyl(0.028 * K, 0.05 * K),
        "head":   _mk_ring(0.05 * K, 0.008 * K),
        "root":   _mk_ring_torus(0.16 * K, 0.012 * K),
        "hips":   _mk_box(0.05 * K, 0.05 * K, 0.05 * K),
    }
    out = {}
    for key, fn in defs.items():
        out[key] = _new_mesh_obj(WGT_PREFIX + key, fn, coll)
    return out


def shape_local_size(obj):
    """形状对象的本地尺寸"""
    if obj is None or not hasattr(obj.data, "vertices") or len(obj.data.vertices) == 0:
        return 0.0
    xs = [v.co.x for v in obj.data.vertices]
    ys = [v.co.y for v in obj.data.vertices]
    zs = [v.co.z for v in obj.data.vertices]
    return max(max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs))


# ================================================================ 集合

def ensure_collection(name, parent=None):
    c = bpy.data.collections.get(name)
    if c is None:
        c = bpy.data.collections.new(name)
    if c.name not in [x.name for x in bpy.context.scene.collection.children]:
        if parent is None:
            bpy.context.scene.collection.children.link(c)
    if parent is not None and c.name not in [x.name for x in parent.children]:
        parent.children.link(c)
    c.hide_viewport = False
    return c


def ensure_rig_collections(arm):
    """每个角色一套自包含集合树（文档第七节：append 一次就完整）"""
    label = arm.name
    main = ensure_collection(label + "-ik")
    shape = ensure_collection(label + COLL_SHAPE_SUFFIX, main)
    ctrl = ensure_collection(label + COLL_CTRL_SUFFIX, main)
    shape.hide_viewport = True
    return main, shape, ctrl


# ================================================================ 主构建流程

def build(arm, opt=None):
    """一键处理。返回 report dict。

    opt 可选项（默认值 = 保持原有行为）：
      pole_scan         bool  仅"从零建 IK"时扫描极向角度，默认 True
      twist             bool  安装前臂扭转传递，默认 True
      twist_influence   float 默认 0.4
      switches          bool  建 IKFK 切换器与驱动，默认 True
      controls          bool  建 Root/Hips 控制器，默认 True
      shapes            bool  建控制器形状，默认 True
      groups            bool  建骨骼集合分组，默认 True
      profile           bool  写 rig_profile，默认 True
      shape_scale       float 形状尺寸倍率，默认 1.0
      textures          bool  重绑并打包纹理，默认 True
      tex_roots         list  纹理搜索目录（None = 自动推断 .blend 目录及其 Textures 子目录）
      tex_all           bool  处理全部贴图（False = 只处理该骨架引用的，避免误伤其他角色）
    """
    o = {
        "pole_scan": True, "twist": True, "twist_influence": TWIST_DEFAULT,
        "switches": True, "controls": True, "shapes": True,
        "groups": True, "profile": True, "shape_scale": 1.0,
        "textures": True, "tex_roots": None, "tex_all": False,
    }
    if opt:
        o.update(opt)

    rep = {"armature": arm.name, "steps": [], "warnings": [], "metrics": {},
           "created": {"bones": [], "constraints": 0, "drivers": 0,
                       "shapes": 0, "collections": [], "objects": []}}

    def step(msg):
        rep["steps"].append(msg)

    def warn(msg):
        rep["warnings"].append(msg)

    # ---------- 0. 前置：解除隐藏，否则约束静默失效
    unhide_all()
    before_bones = set(arm.data.bones.keys())
    before_cons = sum(len(pb.constraints) for pb in arm.pose.bones)
    before_drv = len(arm.animation_data.drivers) if arm.animation_data else 0

    # ---------- 1. 结构发现
    info = D.discover(arm)
    met = M.measure(arm, info.get("human"))
    info["metrics_world"] = met
    info["local_to_world"] = M.local_to_world(arm, info.get("human"))
    info["forward"] = tuple(round(v, 5) for v in
                            M.forward_world(arm, info.get("human"), met["body_axis"]))
    rep["discovery"] = info
    step("结构发现: IK %s，头部手柄 %s，表情网格 %s" % (
        "已具备" if info["has_ik"] else "缺失",
        info["ik"]["head"]["handle"] if info["ik"]["head"] else "无",
        info["face"]["mesh"] or "无"))
    step("世界度量: 身高 %.4f（%s），身体轴 %s，本地/世界系数 %.4f，本地 up 轴 %s" % (
        met["height"], met["method"], met["body_axis"],
        info["local_to_world"], met["local_up_axis"]))

    # ---------- 2. 集合
    main_coll, shape_coll, ctrl_coll = ensure_rig_collections(arm)
    for c in (main_coll, shape_coll, ctrl_coll):
        rep["created"]["collections"].append(c.name)
    # 骨架与网格并入角色集合
    for ob in list(bpy.data.objects):
        if ob.type == "MESH" and any(
                m.type == "ARMATURE" and m.object == arm for m in ob.modifiers):
            if main_coll not in ob.users_collection:
                main_coll.objects.link(ob)
    if main_coll not in arm.users_collection:
        main_coll.objects.link(arm)

    # ---------- 3. 骨架脱离对象父级（世界坐标烘焙进自身）
    if arm.parent is not None:
        mw = arm.matrix_world.copy()
        pname = arm.parent.name
        arm.parent = None
        arm.matrix_world = mw
        upd(3)
        step("骨架脱离对象父级 %s（世界坐标已烘焙，误差自检见指标）" % pname)

    # ---------- 4. 补齐 / 新建 IK 手柄与极向
    ik_built = _ensure_ik(arm, info, o, rep, step, warn)

    # ---------- 5. IK 约束与链末端旋转
    _ensure_constraints(arm, info, rep, step, warn)

    # ---------- 6. 极向角度扫描（仅从零建时）
    if ik_built and o["pole_scan"]:
        _auto_pole(arm, info, rep, step, warn)

    # ---------- 7. 前臂扭转传递
    if o["twist"]:
        _ensure_twist(arm, info, o["twist_influence"], rep, step)

    # ---------- 8. Root / Hips 控制器骨骼
    if o["controls"]:
        _ensure_root_hips(arm, info, rep, step, warn)

    # ---------- 9. IKFK 切换器与驱动
    if o["switches"]:
        _ensure_switches(arm, info, rep, step, warn)

    # ---------- 10. 控制器形状
    if o["shapes"]:
        _ensure_shapes(arm, info, shape_coll, o["shape_scale"], rep, step, warn)

    # ---------- 11. 骨骼集合分组
    if o["groups"]:
        _ensure_groups(arm, info, rep, step)

    # ---------- 12. rig_profile
    if o["profile"]:
        prof = make_profile(arm, info)
        arm[PROFILE_KEY] = json.dumps(prof, ensure_ascii=False)
        arm[PROFILE_VER] = 1
        arm[MARK] = True
        rep["profile"] = prof
        step("写入 rig_profile（label=%s，手柄 %d，根=%s）" % (
            prof["label"], len(prof["handles"]), prof["root"]))

    # ---------- 12.5 纹理重绑与打包
    # 放在 profile 之后：即使纹理处理失败，rig 结果也已落盘可用
    if o["textures"]:
        try:
            tr = TEX.process(arm, o["tex_roots"], do_rebind=True, do_pack=True,
                             all_images=o["tex_all"])
            rep["textures"] = tr
            for n in tr.get("notes", []):
                step("纹理: %s" % n)
            st = tr.get("stats")
            if st:
                step("纹理: %s" % TEX.stats_text(st))
            if tr.get("rebind", {}).get("missing"):
                warn("有 %d 张贴图没找到文件（%s）" % (
                    len(tr["rebind"]["missing"]),
                    [m[1] for m in tr["rebind"]["missing"][:3]]))
        except Exception as ex:
            warn("纹理处理失败: %s" % ex)

    # ---------- 13. 数值验证
    upd(10)
    # 合并而非替换：前面步骤已写入 shape_world_size / bone_collections 等指标
    rep["metrics"].update(verify(arm, info))
    metrics = rep["metrics"]
    rep["created"]["bones"] = sorted(set(arm.data.bones.keys()) - before_bones)
    rep["created"]["constraints"] = (
        sum(len(pb.constraints) for pb in arm.pose.bones) - before_cons)
    rep["created"]["drivers"] = (
        (len(arm.animation_data.drivers) if arm.animation_data else 0) - before_drv)
    step("验证: 静置最大偏差 %.6f，IK 跟随率最低 %.4f" % (
        metrics.get("rest_dev_max", -1), metrics.get("follow_min", -1)))
    return rep


# ---------------------------------------------------------------- 4. IK 手柄

def _rest_head(arm, name):
    b = arm.data.bones.get(name)
    return Vector(b.head_local) if b else None


def _rest_tail(arm, name):
    b = arm.data.bones.get(name)
    return Vector(b.tail_local) if b else None


def bend_axis(arm, upper, lower, tip):
    """从 rest 姿态反推关节弯曲方向（本地坐标单位向量）。

    实测规则（模型Rig优化工作流.md 二·五 第三步）：
      关节相对"上段起点 -> 链末端"连线的偏移方向 = rest 的弯曲侧。

    但**直接用这个偏移方向当极向方向会偏**。实测对比（_autorig_work/diag_pole_pos.txt）：

      原模型 Mafuyu   极向方向 = (0, 1, 0) / (0, -1, 0)   <- 纯轴对齐
      ez 早期实现     极向方向 = (0.0008, 0.9357, -0.3529)  <- 带倾斜

    倾斜分量（这里是 -0.353 的 Z）来自关节弯曲很浅时的数值噪声 ——
    Mafuyu 的 rest 接近伸直，弯曲深度只有 0.0013，方向里的倾斜是"几乎为零的偏移"
    被归一化后放大出来的。用这种方向摆极向，IK 解算平面会偏，静置偏差 0.002447 超标。

    解法：**取偏移方向的主导轴，做轴对齐**。这样既保留"朝哪一侧弯"的信息，
    又不会被浅弯曲的噪声带偏。

    返回 (轴对齐单位向量, 偏移深度)，无法判定返回 (None, 0.0)。
    """
    a = _rest_head(arm, upper)
    mid = _rest_head(arm, lower)
    b = _rest_head(arm, tip)
    if a is None or mid is None or b is None:
        return None, 0.0
    seg = b - a
    if seg.length < 1e-9:
        return None, 0.0
    t = (mid - a).dot(seg) / seg.dot(seg)
    off = mid - (a + seg * t)
    if off.length < 1e-9:
        return None, 0.0

    # 取主导轴做轴对齐（抗浅弯曲噪声）
    idx = max(range(3), key=lambda i: abs(off[i]))
    axis = Vector((0.0, 0.0, 0.0))
    axis[idx] = 1.0 if off[idx] >= 0.0 else -1.0
    return axis, off.length


def _ensure_ik(arm, info, o, rep, step, warn):
    """补手柄与极向。

    返回新建肢体的 spec 列表（非空表示该模型原先没有 IK，需要扫 pole_angle）。

    重要：所有 rest 几何计算都在**编辑模式外**完成。编辑模式会重建骨骼层级，
    模式内访问 arm.data.bones 会拿到失效引用（文档 3.6 实测崩溃过）。
    """
    built_specs = []
    # 极向距离：按真实世界身高比例换算到本地单位
    # （实测 Rusk 用 12 本地单位 = 0.12 世界；这里用 身高 8% 自适应）
    l2w = M.local_to_world(arm, info.get("human"))
    world_h = info.get("metrics_world", {}).get("height")
    if not world_h:
        world_h = M.measure(arm, info.get("human"))["height"]
    pole_dist_world = max(world_h * 0.08, 1e-6)
    pole_dist = pole_dist_world / max(l2w, 1e-9)
    human = info["human"]

    pole_jobs = []       # (骨骼名, head, tail) —— 要补的极向
    build_jobs = []      # 从零建的肢体几何
    head_job = None      # 从零建的头部手柄几何

    # 头部手柄（纯 FK 骨架没有 TRACK_TO，discover 会把 handle 留空）
    _h = info["ik"].get("head")
    if _h and _h.get("from_scratch") and not _h.get("handle"):
        hb = arm.data.bones.get(_h["driver"])
        if hb is not None:
            # 沿 Head 的 rest 局部 Z 轴偏移 0.30 世界单位
            # （实测：摆世界正前方会低头 6°）
            l2w = M.local_to_world(arm, human)
            local_z = hb.matrix_local.to_3x3().col[2].normalized()
            off_local = local_z * (0.30 / max(l2w, 1e-9))
            hname = _free_name(arm, "ez_head_ik")
            h = _rest_head(arm, hb.name) + off_local
            head_job = {
                "name": hname, "driver": hb.name,
                "head": h, "tail": h + local_z * (0.10 / max(l2w, 1e-9)),
                "offset": off_local,
            }

    for limb in ("arm", "leg"):
        for side in ("L", "R"):
            e = info["ik"]["limbs"][limb].get(side)
            if e is None:
                job = _plan_limb_from_scratch(arm, human, limb, side, pole_dist, warn)
                if job:
                    build_jobs.append(job)
                continue
            if e.get("pole") and arm.data.bones.get(e["pole"]):
                continue
            upper = e["chain"][-2] if len(e.get("chain") or []) > 1 else e["ik"]
            axis, depth = bend_axis(arm, upper, e["ik"], e["tip"])
            if axis is None:
                warn("%s %s 无法反推弯曲侧，跳过极向" % (limb, side))
                continue
            name = _pole_name(arm, e, limb, side)
            h = _rest_head(arm, e["ik"]) + axis * pole_dist
            pole_jobs.append((name, h, h + axis * pole_dist * 0.6))
            e["pole"] = name

    # ---------- 编辑模式内：只做创建
    if pole_jobs or build_jobs or head_job:
        with EditBones(arm) as eb:
            for name, h, t in pole_jobs:
                eb_make(eb, name, tuple(h), tuple(t))
                rep["created"]["bones"].append(name)
                step("补齐极向 %s（head=%s）" % (
                    name, tuple(round(v, 4) for v in h)))
            for job in build_jobs:
                _create_limb(arm, eb, job, rep, step)
            if head_job:
                _create_head_handle(arm, eb, head_job, rep, step)
    upd(6)

    # ---------- 从零建的肢体：退出编辑模式后再建约束
    for job in build_jobs:
        _constrain_built_limb(arm, job, rep, step)
    if head_job:
        _constrain_head(arm, head_job, rep, step)
    if build_jobs or head_job:
        upd(6)
        fresh = D.discover(arm)
        for limb in ("arm", "leg"):
            for side in ("L", "R"):
                got = fresh["ik"]["limbs"][limb].get(side)
                if got:
                    info["ik"]["limbs"][limb][side] = got
        for job in build_jobs:
            if not info["ik"]["limbs"][job["limb"]].get(job["side"]):
                info["ik"]["limbs"][job["limb"]][job["side"]] = dict(job)
        if fresh["ik"]["head"]:
            info["ik"]["head"] = fresh["ik"]["head"]
        elif head_job:
            # 发现仍失败时用 job 兜底
            info["ik"]["head"] = {
                "driver": head_job["driver"], "handle": head_job["name"],
                "track_axis": "TRACK_Z", "up_axis": "UP_Y",
                "use_target_z": False, "influence": 1.0, "from_scratch": True,
            }
        built_specs = build_jobs

    # 极向缺失的最后兜底：直接读约束
    for limb in ("arm", "leg"):
        for side in ("L", "R"):
            e = info["ik"]["limbs"][limb].get(side)
            if e is not None and not e.get("pole"):
                e["pole"] = _find_pole_by_ik(arm, e, warn)
    return built_specs


def _pole_name(arm, entry, limb, side):
    base = "ez_%s_%s_pole" % (limb, side)
    n, i = base, 2
    while arm.data.bones.get(n) is not None:
        n = "%s_%d" % (base, i)
        i += 1
    return n


def _find_pole_by_ik(arm, entry, warn):
    """极向不在链内、且没有直接线索时：读 IK 约束上的 pole_subtarget"""
    pb = arm.pose.bones.get(entry["ik"])
    if pb is None:
        return ""
    for c in pb.constraints:
        if c.type == "IK" and c.pole_subtarget:
            if arm.data.bones.get(c.pole_subtarget):
                return c.pole_subtarget
    return ""


def _plan_limb_from_scratch(arm, human, limb, side, pole_dist, warn):
    """源模型完全没有 IK 时，规划手柄与极向的几何（只算不建）。

    实测规则（模型Rig优化工作流.md 二·五）：
      手柄 head 落在"链末端骨骼的 head"上，朝向抄末端骨骼的 tail
      -> IK 静置解算与 rest 完全重合，零姿态漂移

    极向距离（实测修正）：
      不能只用"身高比例"拍一个数。实测对比（_autorig_work/diag_pole_pos.txt）：

        原模型 Mafuyu   臂 0.5450  腿 0.3390（本地单位）
        ez 早期实现     全部 0.1136            <- 太近，静置偏差 0.002447 超标

      极向离关节太近时，IK 的弯曲平面容易被带偏。正确做法是按**链长**取比例：
        臂：链长 × 0.55    腿：链长 × 0.34
      实测这样得到的距离与原模型同量级，静置偏差回落到阈值内。
    """
    key = {"arm": ("upperarm", "lowerarm", "hand"),
           "leg": ("upperleg", "lowerleg", "foot")}[limb]
    up = human.get("%s.%s" % (key[0], side))
    lo = human.get("%s.%s" % (key[1], side))
    tip = human.get("%s.%s" % (key[2], side))
    if not (up and lo and tip):
        warn("%s %s 缺少人形骨骼 %s，无法从零建 IK" % (limb, side, key))
        return None
    if arm.data.bones.get(up) is None or arm.data.bones.get(lo) is None \
            or arm.data.bones.get(tip) is None:
        warn("%s %s 人形骨骼名未命中实际骨骼" % (limb, side))
        return None

    axis, depth = bend_axis(arm, up, lo, tip)
    if axis is None:
        warn("%s %s 无法反推弯曲侧" % (limb, side))
        return None

    # 极向距离：按链长取比例（实测配方，见 docstring）
    chain_len = 0.0
    for a, b in ((up, lo), (lo, tip)):
        ba = arm.data.bones.get(a)
        bb = arm.data.bones.get(b)
        if ba and bb:
            chain_len += (bb.head_local - ba.head_local).length
    ratio = 0.55 if limb == "arm" else 0.34
    dist = chain_len * ratio if chain_len > 1e-9 else pole_dist
    # 兜底：链长异常时退回身高比例
    if dist < 1e-9:
        dist = pole_dist

    hname = _free_name(arm, "ez_%s_%s_ik" % (limb, side))
    pname = _free_name(arm, "ez_%s_%s_pole" % (limb, side))
    jnt = _rest_head(arm, lo)
    ph = jnt + axis * dist

    # 手柄的朝向：**骨长轴要对齐前臂的骨长轴**，不是对齐末端骨骼。
    #
    # 实测踩过的坑（用户报"上臂旋转不生效"）：
    #   原实现抄末端骨骼的 tail，得到的手柄骨长轴与前臂夹角 2.638° / 4.223°，
    #   而文档实测三台角色都 < 0.5°。夹角偏大时，扭转传递（COPY_ROTATION
    #   LOCAL 空间 + 只开 use_y）在**静置时就不再恒等** —— use_y 那一分量
    #   两边不相等，扭转的贡献被 IK 位置解算掩盖，实测"扭转开/关差 0.000000"。
    #
    # 做法：手柄 head 落在末端骨骼 head（保证 IK 静置零偏差），
    #      tail 沿**前臂的骨长轴**方向伸出，长度取前臂骨长的 60%。
    lo_head = _rest_head(arm, lo)
    lo_tail = _rest_tail(arm, lo)
    tip_head = _rest_head(arm, tip)
    if lo_head is not None and lo_tail is not None:
        fa_dir = lo_tail - lo_head
        if fa_dir.length > 1e-9:
            fa_dir.normalize()
            seg = (lo_tail - lo_head).length
            h_tail = tip_head + fa_dir * (seg * 0.6)
        else:
            h_tail = _rest_tail(arm, tip)
    else:
        h_tail = _rest_tail(arm, tip)

    return {
        "ik": lo, "handle": hname, "pole": pname, "tip": tip,
        "side": side, "limb": limb,
        "handle_head": tip_head, "handle_tail": h_tail,
        "pole_head": ph, "pole_tail": ph + axis * dist * 0.6,
        "pole_dist": dist, "chain_len": chain_len,
        "chain": _chain_names(arm, lo, 3), "chain_count": 2,
        "use_stretch": False, "rot_owner": None, "from_scratch": True,
    }


def _free_name(arm, base):
    n, i = base, 2
    while arm.data.bones.get(n) is not None:
        n = "%s_%d" % (base, i)
        i += 1
    return n


def _chain_names(arm, bone_name, count):
    out, b = [], arm.data.bones.get(bone_name)
    while b is not None and len(out) < max(1, int(count)):
        out.append(b.name)
        b = b.parent
    return out


def _create_limb(arm, eb, job, rep, step):
    """编辑模式内创建手柄与极向骨骼"""
    eb_make(eb, job["handle"], tuple(job["handle_head"]), tuple(job["handle_tail"]))
    eb_make(eb, job["pole"], tuple(job["pole_head"]), tuple(job["pole_tail"]))
    eb[job["ik"]].use_connect = False
    rep["created"]["bones"] += [job["handle"], job["pole"]]
    step("从零建手柄 %s（head 对齐 %s）与极向 %s" % (
        job["handle"], job["tip"], job["pole"]))


def _create_head_handle(arm, eb, job, rep, step):
    """创建头部手柄骨骼。

    实测规则（Rusk_优化说明.md 三）：手柄 head 沿 Head 骨骼的 rest 局部 Z 轴偏移，
    这样 TRACK_TO 在静置时的旋转量恰好为零。摆在世界正前方会差约 6°（低头）。
    """
    eb_make(eb, job["name"], tuple(job["head"]), tuple(job["tail"]))
    rep["created"]["bones"].append(job["name"])
    step("从零建头部手柄 %s（沿 %s 的 rest 局部 Z 轴偏移 %s）" % (
        job["name"], job["driver"],
        tuple(round(v, 4) for v in job["offset"])))


def _constrain_head(arm, job, rep, step):
    """给头部挂 TRACK_TO（无头部手柄时从零建）"""
    pb = arm.pose.bones.get(job["driver"])
    if pb is None:
        return
    for c in list(pb.constraints):
        if c.type == "TRACK_TO":
            pb.constraints.remove(c)
    con = pb.constraints.new("TRACK_TO")
    con.name = "标准追踪"
    con.target = arm
    con.subtarget = job["name"]
    con.track_axis = "TRACK_Z"
    con.up_axis = "UP_Y"
    con.use_target_z = False
    rep["created"]["constraints"] += 1
    step("从零建头部追踪: %s <- %s（TRACK_TO / TRACK_Z / UP_Y）" % (
        job["driver"], job["name"]))


def _constrain_built_limb(arm, job, rep, step):
    """给从零建的肢体挂 IK + 链末端旋转约束"""
    pb = arm.pose.bones.get(job["ik"])
    if pb is None:
        return
    for c in list(pb.constraints):
        if c.type == "IK":
            pb.constraints.remove(c)
    con = pb.constraints.new("IK")
    con.name = "IK"
    con.target = arm
    con.subtarget = job["handle"]
    if job.get("pole") and arm.data.bones.get(job["pole"]):
        con.pole_target = arm
        con.pole_subtarget = job["pole"]
        # 实测：从零建时 -90° 是正确起点（Rusk 四条链统一 -π/2）
        con.pole_angle = -math.pi / 2
    con.chain_count = job["chain_count"]
    con.use_stretch = job["use_stretch"]
    con.use_tail = True

    tpb = arm.pose.bones.get(job["tip"])
    if tpb is not None:
        for c in list(tpb.constraints):
            if c.type == "COPY_ROTATION" and c.subtarget == job["handle"]:
                tpb.constraints.remove(c)
        rot = tpb.constraints.new("COPY_ROTATION")
        rot.name = "复制旋转"
        rot.target = arm
        rot.subtarget = job["handle"]
        rot.target_space = "WORLD"
        rot.owner_space = "WORLD"
        rot.mix_mode = "REPLACE"
    rep["created"]["constraints"] += 2
    step("从零建 IK: %s <- 手柄 %s 极向 %s chain=%d pole=-90°；末端 %s 已接旋转" % (
        job["ik"], job["handle"], job.get("pole"), job["chain_count"], job["tip"]))




# ---------------------------------------------------------------- 5. 约束

def _ensure_constraints(arm, info, rep, step, warn):
    """建 IK / 链末端 COPY_ROTATION / 头部 TRACK_TO。已存在则不重复建。"""
    made = 0
    for limb in ("arm", "leg"):
        for side in ("L", "R"):
            e = info["ik"]["limbs"][limb].get(side)
            if e is None:
                continue
            pb = arm.pose.bones.get(e["ik"])
            if pb is None:
                continue
            ik_con = None
            for c in pb.constraints:
                if c.type == "IK":
                    ik_con = c
                    break
            if ik_con is None:
                ik_con = pb.constraints.new("IK")
                ik_con.name = "IK"
                made += 1
            ik_con.target = arm
            ik_con.subtarget = e["handle"]
            if e["pole"] and arm.data.bones.get(e["pole"]):
                ik_con.pole_target = arm
                ik_con.pole_subtarget = e["pole"]
                if ik_con.pole_angle == 0.0 and limb == "leg":
                    ik_con.pole_angle = -math.pi / 2
            ik_con.chain_count = e["chain_count"]
            ik_con.use_stretch = e["use_stretch"]
            ik_con.use_tail = e["use_tail"]

            # 链末端旋转跟随（原始模型可能缺失，如 Mafuyu 的脚）
            tip = e["tip"]
            if tip and arm.pose.bones.get(tip):
                tpb = arm.pose.bones[tip]
                rot = None
                for c in tpb.constraints:
                    if c.type == "COPY_ROTATION" and c.subtarget == e["handle"]:
                        rot = c
                        break
                if rot is None:
                    rot = tpb.constraints.new("COPY_ROTATION")
                    rot.name = "复制旋转"
                    rot.target = arm
                    rot.subtarget = e["handle"]
                    rot.target_space = "WORLD"
                    rot.owner_space = "WORLD"
                    rot.mix_mode = "REPLACE"
                    e["rot_owner"] = tip
                    made += 1
                    step("补齐链末端旋转 %s <- %s" % (tip, e["handle"]))
    upd(3)

    # 头部 TRACK_TO
    h = info["ik"]["head"]
    if h and arm.pose.bones.get(h["driver"]):
        pb = arm.pose.bones[h["driver"]]
        con = None
        for c in pb.constraints:
            if c.type == "TRACK_TO":
                con = c
                break
        if con is None:
            con = pb.constraints.new("TRACK_TO")
            con.name = "标准追踪"
            con.track_axis = "TRACK_Z"
            con.up_axis = "UP_Y"
            con.use_target_z = False
            made += 1
        con.target = arm
        con.subtarget = h["handle"]
    upd(3)
    rep["created"]["constraints"] += made


def _auto_pole(arm, info, rep, step, warn):
    """从零建的模型需要扫 pole_angle（默认 0 是错的）"""
    for limb in ("arm", "leg"):
        for side in ("L", "R"):
            e = info["ik"]["limbs"][limb].get(side)
            if e is None:
                continue
            best, table = scan_pole_angle(arm, e)
            pb = arm.pose.bones.get(e["ik"])
            if pb is None:
                continue
            for c in pb.constraints:
                if c.type == "IK":
                    c.pole_angle = best
            e["pole_angle"] = best
            upd(6)
            step("极向扫描 %s %s: pole_angle=%.4f（%.2f°）" % (
                limb, side, best, math.degrees(best)))


# ---------------------------------------------------------------- 7. 扭转传递

def _forearm_of(arm, entry):
    """取前臂骨骼（扭转传递的挂载点）。

    这个函数被两个坑逼出来的：

      坑一：**不能用 chain[2]**。实测（用户报"上臂旋转不生效"）：
            chain 是"IK 骨骼沿父级向上 chain_count 根"，
            chain_count=2 时 chain 只有 [Lowerarm_L, Upperarm_L] 两个元素，
            chain[2] 越界 -> 原实现 `if not fa: continue` **静默跳过扭转传递**。
            用户的模型正是 chain_count=2，而 Karin 是 3 碰巧有 chain[2]。

      坑二：**profile 的 `ik` 字段存的是手柄名**（原插件 karin 的约定：
            ik = LHik 这种手柄），不是 IK 骨骼名。拿它去找前臂必然失败。

    解析顺序（entry 可以是 discover 结果，也可以是 profile 段）：
      1. entry["ik"] 如果自身带 IK 约束 -> 它就是 IK 骨骼
      2. entry["ik_bone"]  <- ez 新写的明确字段
      3. entry["chain"][0] <- chain 首项 = IK 骨骼
      4. 沿父级向上找带 IK 约束的骨骼
      5. 都失败 -> 返回 None（调用方给警告，不静默）
    """
    if not entry:
        return None

    # 1) discover 结果的 ik 字段：IK 约束就挂在前臂上
    ik_name = entry.get("ik")
    if ik_name:
        pb = arm.pose.bones.get(ik_name)
        if pb is not None and any(c.type == "IK" for c in pb.constraints):
            return ik_name

    # 2) 明确字段
    n = entry.get("ik_bone")
    if n and arm.pose.bones.get(n) is not None:
        return n

    # 3) chain 首项
    chain = entry.get("chain") or []
    if chain and arm.pose.bones.get(chain[0]) is not None:
        return chain[0]

    # 4) 沿父级向上找带 IK 约束的骨骼
    b = arm.data.bones.get(ik_name) if ik_name else None
    depth = 0
    while b is not None and b.parent is not None and depth < 4:
        b = b.parent
        depth += 1
        ppb = arm.pose.bones.get(b.name)
        if ppb is None:
            continue
        if any(c.type == "IK" for c in ppb.constraints):
            return b.name

    # 5) 兜底：ik 可能是手柄名，反查哪个骨骼的 IK 指向它
    if ik_name:
        for pb in arm.pose.bones:
            for c in pb.constraints:
                if c.type == "IK" and c.subtarget == ik_name:
                    return pb.name
    return None


def _ensure_twist(arm, info, influence, rep, step):
    """前臂扭转传递。三个参数错一个就会破坏 T-pose（文档五·五）：
       LOCAL/LOCAL + 只开 use_y + BEFORE + influence"""
    made = 0
    for side in ("L", "R"):
        e = info["ik"]["limbs"]["arm"].get(side)
        if e is None:
            continue
        fa = _forearm_of(arm, e)
        if not fa:
            continue
        pb = arm.pose.bones.get(fa)
        if pb is None or arm.pose.bones.get(e["handle"]) is None:
            continue
        for c in list(pb.constraints):
            if c.name == TWIST_CNAME:
                pb.constraints.remove(c)
        con = pb.constraints.new("COPY_ROTATION")
        con.name = TWIST_CNAME
        con.target = arm
        con.subtarget = e["handle"]
        con.target_space = "LOCAL"
        con.owner_space = "LOCAL"
        con.mix_mode = "BEFORE"
        con.influence = influence
        con.use_x, con.use_y, con.use_z = (False, True, False)
        # 必须排在 IK 之前
        ik = pb.constraints.find("IK")
        ti = pb.constraints.find(TWIST_CNAME)
        if ik >= 0 and ti > ik:
            pb.constraints.move(ti, ik)
        made += 1
        step("扭转传递挂在 %s（链 %s）" % (fa, e.get("chain")))
    upd(3)
    if made:
        rep["created"]["constraints"] += made
        step("前臂扭转传递 %d 条（LOCAL/LOCAL，只开 Y，BEFORE，比例 %.0f%%）" % (
            made, influence * 100))
    else:
        rep["warnings"].append("扭转传递未安装（没找到可挂载的前臂骨骼）")


# ---------------------------------------------------------------- 8. Root / Hips

def _ensure_root_hips(arm, info, rep, step, warn):
    human = info.get("human")
    met = M.measure(arm, human)
    body_axis = met["body_axis"]
    up_w = M.up_world(arm, human, body_axis)
    mw = arm.matrix_world
    mwi = mw.inverted()

    with EditBones(arm) as eb:
        root_name = None
        prof_raw = arm.get(PROFILE_KEY)
        if prof_raw:
            try:
                root_name = json.loads(prof_raw).get("root")
            except Exception:
                root_name = None
        if not root_name or not eb_exists(eb, root_name):
            root_name = "ez_Root"
            if not eb_exists(eb, root_name):
                # 放在脚底中心（世界空间定位后换算回本地）
                hb = arm.data.bones.get(info["root"])
                anchor_w = mw @ hb.head_local if hb else Vector((0, 0, 0))
                # 沿身体轴下移到脚底
                lowest = anchor_w
                for f in met["feet"]:
                    fb = arm.data.bones.get(f)
                    if fb is None:
                        continue
                    for p in (mw @ fb.head_local, mw @ fb.tail_local):
                        if p.dot(up_w) < lowest.dot(up_w):
                            lowest = p
                delta = (lowest - anchor_w).dot(up_w)
                w = anchor_w + up_w * delta
                h = mwi @ w
                t = mwi @ (w + up_w * (met["height"] * 0.06))
                eb_make(eb, root_name, tuple(h), tuple(t))
                rep["created"]["bones"].append(root_name)

        # Hips 控制器
        hips_ctrl = "ez_Hips"
        if not eb_exists(eb, hips_ctrl):
            hb = arm.data.bones.get(info["root"])
            if hb is not None:
                h = Vector(hb.head_local)
                tail_w = (mw @ h) + up_w * (met["height"] * 0.05)
                eb_make(eb, hips_ctrl, tuple(h), tuple(mwi @ tail_w))
                rep["created"]["bones"].append(hips_ctrl)
            else:
                hips_ctrl = None

    upd(4)

    # ---------- 层级：控制器挂到 Root，**骨架主干也要挂**
    #
    # 这里有两个实测踩过的坑（用户报「root ik 不会带动角色移动，只会带动手柄移动」）：
    #
    # 坑一：ez_Root 是**新建的骨骼**，不在原来的骨架主干上。
    #       只把控制器挂到它下面的话，拖 ez_Root 只会带动手柄，
    #       骨架本体（Hips/Spine/Head）纹丝不动 —— 实测本体位移 0.00000。
    #       必须把**主干根骨骼**（原骨架里无父级的那个）也挂到 ez_Root 下，
    #       这样拖 Root 时整条主干连同控制器一起走。
    #
    # 坑二：切换器是**后面步骤（步骤 9）才创建的**，
    #       这里按名字前缀去找时它们还不存在，于是 4 个切换器永远挂不上
    #       （实测 ez_Root 只有 10 个子级，应该是 14 个）。
    #       修法：这里不再找切换器，改由步骤 9 建完后自己挂到 Root 下。
    if arm.data.bones.get(root_name):
        # 主干根：原来的无父级骨骼（discover 认定的 root），排除 ez 自己建的
        trunk_root = info.get("root")
        if trunk_root == root_name or not eb_exists(eb, trunk_root or ""):
            # discover 给的 root 可能已被替换，重新找一个真正的主干根
            trunk_root = None
            for b in arm.data.bones:
                if b.parent is None and b.name != root_name \
                        and not b.name.startswith("ez_") \
                        and not D.norm(b.name).startswith("ikfk"):
                    # 取子孙最多的那个（最可能是主干根）
                    if trunk_root is None or \
                            len(D._all_desc(arm, b.name)) > \
                            len(D._all_desc(arm, trunk_root)):
                        trunk_root = b.name

        with EditBones(arm) as eb2:
            # 1) 主干根挂到 ez_Root 下（这是"带动角色"的关键）
            if trunk_root and trunk_root != root_name and eb2.get(trunk_root):
                eb2[trunk_root].parent = eb2[root_name]
                eb2[trunk_root].use_connect = False
                step("主干根 %s 挂到 %s 下（拖 Root 时本体跟随）" % (
                    trunk_root, root_name))

            # 2) 控制器挂到 ez_Root 下
            names = [info["ik"]["limbs"][l][s]["handle"]
                     for l in ("arm", "leg") for s in ("L", "R")
                     if info["ik"]["limbs"][l].get(s)]
            names += [info["ik"]["limbs"][l][s]["pole"]
                      for l in ("arm", "leg") for s in ("L", "R")
                      if info["ik"]["limbs"][l].get(s) and info["ik"]["limbs"][l][s].get("pole")]
            if info["ik"]["head"]:
                names.append(info["ik"]["head"]["handle"])
            if hips_ctrl:
                names.append(hips_ctrl)
            n_ok = 0
            for n in names:
                if eb2.get(n) and n != root_name and n != trunk_root:
                    eb2[n].parent = eb2[root_name]
                    eb2[n].use_connect = False
                    n_ok += 1
            step("控制器 %d 个挂到 %s 下（切换器由步骤 9 自行挂载）" % (
                n_ok, root_name))

        info["trunk_root"] = trunk_root
    upd(3)
    info["root"] = root_name
    info["hips_ctrl"] = hips_ctrl
    step("根控制器 %s；Hips 控制器 %s；主干根 %s" % (
        root_name, hips_ctrl, info.get("trunk_root")))


# ---------------------------------------------------------------- 9. 切换器

def _ensure_switches(arm, info, rep, step, warn):
    """IKFK 切换器骨骼 + 属性 + 驱动。

    摆放用**世界空间**定位再换算回本地（本地 up 轴可能是 Y，见 ez_metrics）。
    """
    human = info.get("human")
    met = M.measure(arm, human)
    body_axis = met["body_axis"]
    height_w = met["height"]
    fwd_w = M.forward_world(arm, human, body_axis)
    left_w = M.left_world(arm, human, body_axis)
    up_w = M.up_world(arm, human, body_axis)
    mw = arm.matrix_world
    mwi = mw.inverted()

    # 角色世界位置的参考点：根骨骼世界坐标
    root_name = info.get("root")
    root_b = arm.data.bones.get(root_name) if root_name else None
    if root_b is None:
        feet = met["feet"]
        root_w = mw @ arm.data.bones[feet[0]].head_local if feet else Vector((0, 0, 0))
    else:
        root_w = mw @ root_b.head_local

    # 地面高度：脚底（沿身体轴的最低点）
    ground_w = root_w.copy()
    if met["feet"]:
        lowest = None
        for f in met["feet"]:
            fb = arm.data.bones.get(f)
            if fb is None:
                continue
            for p in (mw @ fb.head_local, mw @ fb.tail_local):
                if lowest is None or p.dot(up_w) < lowest.dot(up_w):
                    lowest = p
        if lowest is not None:
            ground_w = lowest

    specs = [("IKFK_Arm.L", "L", 0.88), ("IKFK_Arm.R", "R", 0.88),
             ("IKFK_Leg.L", "L", 0.74), ("IKFK_Leg.R", "R", 0.74)]
    made_bones = []

    # 先在世界空间算好位置，再转回本地
    jobs = []
    for name, side, zf in specs:
        if arm.data.bones.get(name):
            continue
        sgn = 1 if side == "L" else -1
        w = (root_w
             + left_w * (0.16 * height_w * sgn)
             + fwd_w * (0.28 * height_w)
             + up_w * ((zf * height_w) - (ground_w - root_w).dot(up_w)))
        h = mwi @ w
        t = mwi @ (w + up_w * (0.06 * height_w))
        jobs.append((name, h, t))

    if jobs:
        with EditBones(arm) as eb:
            for name, h, t in jobs:
                eb_make(eb, name, tuple(h), tuple(t))
                made_bones.append(name)
            # **切换器也要挂到 Root 下**
            #
            # 实测踩过的坑：步骤 8 建 Root 时按名字前缀找切换器，
            # 但切换器是**这一步才创建**的，那时还不存在 -> 永远挂不上。
            # 实测 ez_Root 只有 10 个子级（应该是 14 个）。
            # 修法：在这里（创建完之后）自己挂。
            root_n = info.get("root")
            if root_n and eb.get(root_n):
                for name, _h, _t in jobs:
                    if eb.get(name):
                        eb[name].parent = eb[root_n]
                        eb[name].use_connect = False
    upd(3)
    rep["created"]["bones"] += made_bones
    if made_bones and info.get("root"):
        step("切换器 %d 个已挂到 %s 下" % (len(made_bones), info.get("root")))

    if not arm.animation_data:
        arm.animation_data_create()

    # 属性
    for name, desc in (("IKFK_Arm.L", "0 = 手臂 FK，1 = 手臂 IK"),
                       ("IKFK_Arm.R", "0 = 手臂 FK，1 = 手臂 IK"),
                       ("IKFK_Leg.L", "0 = 腿 FK，1 = 腿 IK"),
                       ("IKFK_Leg.R", "0 = 腿 FK，1 = 腿 IK")):
        pb = arm.pose.bones.get(name)
        if pb is None:
            continue
        if "IKFK" not in pb:
            pb["IKFK"] = 1.0
        try:
            pb.id_properties_ui("IKFK").update(
                min=0.0, max=1.0, soft_min=0.0, soft_max=1.0,
                description=desc, default=1.0)
        except Exception:
            pass

    # 驱动：把 IK / COPY_ROTATION / 扭转传递 的 influence 挂到切换器
    bound = 0
    for limb, sw in (("arm", "IKFK_Arm."), ("leg", "IKFK_Leg.")):
        for side in ("L", "R"):
            e = info["ik"]["limbs"][limb].get(side)
            if e is None:
                continue
            ctrl_bone = sw + side
            if arm.pose.bones.get(ctrl_bone) is None:
                warn("切换器 %s 不存在，跳过驱动" % ctrl_bone)
                continue
            owners = [e["ik"]]
            if e.get("rot_owner"):
                owners.append(e["rot_owner"])
            # 扭转传递所在骨骼：必须用 _forearm_of，不能用 chain[2]
            # （chain_count=2 时 chain 只有两个元素，chain[2] 越界 -> 漏绑驱动）
            if limb == "arm":
                fa = _forearm_of(arm, e)
                if fa and fa not in owners:
                    owners.append(fa)
            for owner in owners:
                pb = arm.pose.bones.get(owner)
                if pb is None:
                    continue
                for c in pb.constraints:
                    if c.type not in ("IK", "COPY_ROTATION"):
                        continue
                    rel = 'pose.bones["%s"].constraints["%s"].influence' % (owner, c.name)
                    try:
                        if any(fc.data_path == rel for fc in arm.animation_data.drivers):
                            arm.driver_remove(rel)
                        fc = arm.driver_add(rel)
                        d = fc.driver
                        d.type = "SCRIPTED"
                        d.expression = "ikfk"
                        v = d.variables.new()
                        v.name = "ikfk"
                        v.type = "SINGLE_PROP"
                        t = v.targets[0]
                        t.id = arm
                        t.data_path = 'pose.bones["%s"]["IKFK"]' % ctrl_bone
                        bound += 1
                    except Exception as ex:
                        warn("驱动 %s 失败: %s" % (rel, ex))
    upd(4)
    rep["created"]["drivers"] += bound
    step("IKFK 切换器 %d 个，驱动 %d 条" % (len(specs), bound))


# ---------------------------------------------------------------- 10. 形状

def _ensure_shapes(arm, info, shape_coll, scale, rep, step, warn):
    human = info.get("human")
    met = M.measure(arm, human)
    l2w = M.local_to_world(arm, human)
    shapes = build_shapes(arm, shape_coll, met["height"], scale)
    rep["created"]["shapes"] = len(shapes)

    def pick(key):
        return shapes.get(key)

    assign = {}
    for limb, key in (("arm", "hand"), ("leg", "foot")):
        for side in ("L", "R"):
            e = info["ik"]["limbs"][limb].get(side)
            if e:
                assign[e["handle"]] = key
                if e.get("pole"):
                    assign[e["pole"]] = "pole"
    if info["ik"]["head"]:
        assign[info["ik"]["head"]["handle"]] = "head"
    for b in arm.data.bones:
        if D.norm(b.name).startswith("ikfk"):
            assign[b.name] = "switch"
    if info.get("root") and arm.pose.bones.get(info["root"]):
        assign[info["root"]] = "root"
    if info.get("hips_ctrl") and arm.pose.bones.get(info["hips_ctrl"]):
        assign[info["hips_ctrl"]] = "hips"

    n = 0
    for bn, key in assign.items():
        pb = arm.pose.bones.get(bn)
        if pb is None:
            continue
        sh = pick(key)
        if sh is None:
            continue
        pb.custom_shape = sh
        pb.custom_shape_scale_xyz = (1.0, 1.0, 1.0)
        n += 1
    arm.show_in_front = True
    upd(3)
    # 自检：形状的**世界**尺寸（本地尺寸 × 本地->世界系数）
    sizes = {k: round(shape_local_size(v) * l2w, 5) for k, v in shapes.items()}
    rep["metrics"]["shape_world_size"] = sizes
    rep["metrics"]["shape_local_size"] = {
        k: round(shape_local_size(v), 5) for k, v in shapes.items()}
    rep["metrics"]["local_to_world"] = round(l2w, 6)
    step("控制器形状 %d 个已分配（世界尺寸 %s，本地/世界系数 %.4f）" % (
        n, sizes, l2w))


# ---------------------------------------------------------------- 11. 分组

def _ensure_groups(arm, info, rep, step):
    # 必须重新分类：步骤 1 的分类发生在切换器/手柄创建之前，缓存里没有它们
    groups = D.classify_bones(arm, info["ik"], info.get("human"))
    info["groups"] = groups
    AD = arm.data
    for key, cname in BONE_COLL.items():
        c = AD.collections.get(cname)
        if c is None:
            c = AD.collections.new(cname)
        c.is_visible = BONE_COLL_VISIBLE.get(key, DEFAULT_VISIBLE)
        for b in list(c.bones):
            c.unassign(b)
        for n in groups.get(key, []):
            bb = AD.bones.get(n)
            if bb is not None:
                c.assign(bb)
    # 清掉本插件建的空集合（原模型的空集合保持不动）
    for c in list(AD.collections):
        if len(c.bones) == 0 and c.name in BONE_COLL.values():
            AD.collections.remove(c)
    vis = [c.name for c in AD.collections if c.is_visible]
    rep["metrics"]["bone_collections"] = {
        c.name: (c.is_visible, len(c.bones)) for c in AD.collections}
    step("骨骼集合分组完成，可见集合 %s" % vis)


# ---------------------------------------------------------------- 12. profile

def _guess_label(arm):
    """推断角色显示名。

    优先：骨架名去掉通用后缀 -> 父集合名（如 mafuyu-idle-ik）-> 骨架名。
    'Armature' / 'Armature.001' 这类通用名直接用它做标签没有意义。
    """
    name = arm.name
    for suffix in ("_Armature", "_armature", "Armature", ".001", ".002", ".003"):
        if name.endswith(suffix) and len(name) > len(suffix):
            name = name[: -len(suffix)]
            break
    generic = {"", "armature", "rig", "root", "skeleton", "character", "model"}
    if name.strip().lower() in generic:
        for c in arm.users_collection:
            cn = c.name
            for tail in ("-idle-ik", "-ik", "_idle_ik", "-rig"):
                if cn.lower().endswith(tail):
                    cn = cn[: -len(tail)]
                    break
            cn = cn.strip("-_ ")
            if cn and cn.lower() not in generic:
                return cn[:1].upper() + cn[1:]
        return arm.name
    return name


def make_profile(arm, info):
    """生成 rig_profile（JSON）。

    字段语义（**这里是踩过坑的地方**）：

      原插件（karin）的 profile 里 `ik` 存的是**手柄名**（如 `LHik`），
      而 `chain` 存的是"IK 骨骼沿父级向上 chain_count 根"。

      问题在于：`chain` 的首项才是 IK 骨骼本身，而面板/操作符/构建器
      都可能想直接拿"IK 骨骼"。如果只按 `ik` 去找，拿到的其实是手柄 ——
      手柄上没有 IK 约束，于是：
        - 面板显示"无前臂骨"
        - 构建器找不到可挂载扭转传递的前臂

      做法：**两个字段都写全**，保持与原插件兼容，同时让读取方有明确选择：
        "ik"       = 手柄骨骼名（与原插件一致，Snap 等操作符按此读取）
        "ik_bone"  = IK 约束所在骨骼（chain 首项，扭转传递的挂载点）
        "chain"    = IK 骨骼沿父级向上 chain_count 根
        "handle"   = 手柄骨骼名（与 "ik" 同值，语义更清晰的新名字）
    """
    label = _guess_label(arm)

    arms, legs = {}, {}
    for side in ("L", "R"):
        for limb, dst in (("arm", arms), ("leg", legs)):
            e = info["ik"]["limbs"][limb].get(side)
            if e is None:
                continue
            chain = e.get("chain") or []
            dst[side] = {
                "sw": "IKFK_%s.%s" % ("Arm" if limb == "arm" else "Leg", side),
                "ik": e["handle"],              # 手柄（原插件约定）
                "handle": e["handle"],          # 同值，语义更清晰
                "ik_bone": e["ik"],             # IK 约束所在骨骼（= chain 首项）
                "tip": e["tip"],
                "chain": chain,
            }

    handles = []
    for limb in ("arm", "leg"):
        for side in ("L", "R"):
            e = info["ik"]["limbs"][limb].get(side)
            if e:
                handles.append(e["handle"])
                if e.get("pole"):
                    handles.append(e["pole"])
    if info["ik"]["head"]:
        handles.append(info["ik"]["head"]["handle"])

    collections = {"ik": BONE_COLL["ctrl"], "switch": BONE_COLL["switch"],
                   "deco": [BONE_COLL["hair"], BONE_COLL["deco"]]}

    face = {"scheme": info["face"]["scheme"], "mesh": info["face"]["mesh"]}

    return {
        "label": label,
        "object": arm.name,
        "view_layer_collection": arm.name + "-ik",
        "arms": arms,
        "legs": legs,
        "handles": handles,
        "root": info.get("root") or "",
        "hips_ctrl": info.get("hips_ctrl") or "",
        "empties": {},
        "collections": collections,
        "face": face,
        "ez": {"mark": MARK, "profile_version": 1,
               "built_from": "structure_discovery"},
    }


# ---------------------------------------------------------------- 13. 验证

def verify(arm, info):
    """数值验证。全部实测，不靠肉眼（后台模式看不到画面）。"""
    upd(10)
    sc = abs(arm.matrix_world.to_scale().x) or 1.0
    m = {}

    # 静置偏差（对照 rest）
    #
    # 只查 **IK 链上的骨骼**，不查全骨架。
    # 实测教训（_autorig_work/diag_pole.txt）：全骨架扫描会把物理骨骼算进来 ——
    # 腰带 Belt.* / 围裙 Apron_* 这类飘带的偏差高达 0.17，但它们是物理代理，
    # 本来就不由 IK 驱动，把它们计入判据会得出"偏差超标"的错误结论。
    watch = []
    for limb in ("arm", "leg"):
        for side in ("L", "R"):
            e = info["ik"]["limbs"][limb].get(side)
            if e:
                watch += [e["ik"], e["tip"]]
                if e.get("pole"):
                    watch.append(e["pole"])
    if info["ik"]["head"]:
        watch.append(info["ik"]["head"]["driver"])
    watch = [w for w in watch if arm.pose.bones.get(w)]
    devs = {}
    for n in watch:
        d = pose_dev(arm, n)
        if d is not None:
            devs[n] = d
    m["rest_dev"] = devs
    m["rest_dev_max"] = max(devs.values()) if devs else 0.0

    # IK 跟随率：移动手柄 0.10 世界单位，看链末端位移
    follow = {}
    for limb in ("arm", "leg"):
        for side in ("L", "R"):
            e = info["ik"]["limbs"][limb].get(side)
            if e is None:
                continue
            hb = arm.pose.bones.get(e["handle"])
            tip = arm.pose.bones.get(e["tip"])
            if hb is None or tip is None:
                continue
            dg = bpy.context.evaluated_depsgraph_get()
            p0 = (arm.matrix_world @ arm.evaluated_get(dg).pose.bones[e["tip"]].head).copy()
            orig = hb.location.copy()
            step_local = 0.10 / sc
            hb.location = orig + Vector((step_local, 0.0, 0.0))
            upd(8)
            dg = bpy.context.evaluated_depsgraph_get()
            p1 = (arm.matrix_world @ arm.evaluated_get(dg).pose.bones[e["tip"]].head).copy()
            hb.location = orig
            upd(8)
            ratio = (p1 - p0).length / 0.10
            follow["%s_%s" % (limb, side)] = ratio
    m["follow"] = follow
    m["follow_min"] = min(follow.values()) if follow else 1.0

    # 约束完整性
    cons = {"IK": 0, "COPY_ROTATION": 0, "TRACK_TO": 0}
    missing = []
    for limb in ("arm", "leg"):
        for side in ("L", "R"):
            e = info["ik"]["limbs"][limb].get(side)
            if e is None:
                missing.append("%s_%s" % (limb, side))
                continue
            pb = arm.pose.bones.get(e["ik"])
            if pb and any(c.type == "IK" for c in pb.constraints):
                cons["IK"] += 1
            tp = arm.pose.bones.get(e["tip"])
            if tp and any(c.type == "COPY_ROTATION" for c in tp.constraints):
                cons["COPY_ROTATION"] += 1
    if info["ik"]["head"]:
        hp = arm.pose.bones.get(info["ik"]["head"]["driver"])
        if hp and any(c.type == "TRACK_TO" for c in hp.constraints):
            cons["TRACK_TO"] += 1
    m["constraints"] = cons
    m["missing_limbs"] = missing
    m["drivers"] = len(arm.animation_data.drivers) if arm.animation_data else 0
    m["controllers_with_shape"] = sum(
        1 for pb in arm.pose.bones if pb.custom_shape is not None)
    return m


def verify_text(rep):
    """把验证结果渲染成可读文本"""
    m = rep.get("metrics", {})
    L = []
    L.append("静置最大偏差: %.6f  %s" % (
        m.get("rest_dev_max", -1),
        "通过" if m.get("rest_dev_max", 9) < 0.001 else "超阈值"))
    f = m.get("follow", {})
    if f:
        L.append("IK 跟随率: %s  最低 %.4f  %s" % (
            {k: round(v, 3) for k, v in f.items()}, m.get("follow_min", 0),
            "通过" if m.get("follow_min", 0) > 0.5 else "偏低（伸直链属正常）"))
    L.append("约束: %s  驱动 %d  形状 %d" % (
        m.get("constraints", {}), m.get("drivers", 0),
        m.get("controllers_with_shape", 0)))
    if m.get("missing_limbs"):
        L.append("缺失肢体: %s" % m["missing_limbs"])
    if rep.get("created"):
        c = rep["created"]
        L.append("新增: 骨骼 %d / 约束 %d / 驱动 %d / 形状 %d" % (
            len(c.get("bones", [])), c.get("constraints", 0),
            c.get("drivers", 0), c.get("shapes", 0)))
    return "\n".join(L)
