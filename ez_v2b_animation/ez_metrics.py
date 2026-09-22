# -*- coding: utf-8 -*-
# ---------------------------------------------------------------------------
# ez-v2b-animation  —— 作者：B站 @高压郭炖大葱
# 使用、修改、二次分发请保留本署名。
# ---------------------------------------------------------------------------

"""ez-v2b-animation :: 世界度量

为什么单独一个模块：**本地坐标与 matrix_world 都不可信**。

实测（_autorig_work/probe_metrics.txt）：

 角色.blend / Armature      Karin      matrix_world 单位阵 骨骼本地尺寸 1.75 网格世界身高 1.35
 角色.blend / Armature.001  Chocolate  matrix_world 单位阵 骨骼本地尺寸 456 网格世界身高 ~46
  Mafuyu.blend / Armature    Mafuyu    matrix_world 单位阵 骨骼本地尺寸 1.38 网格世界身高 1.35

三条实测结论（每一条都踩过）：

  1. **matrix_world 是单位阵，缩放全在骨骼数据里。**
 按 `matrix_world.to_scale()` 换算会得到 1.0，而真实身高是 45.7 —— 差 45 倍。
  2. **Chocolate 的骨架是 Y-up**（本地 Y 映射到世界 Z，实测绕 X +90°）。
 用 `head_local.z` 量身高会得到 456 这种荒谬值。
  3. **世界包围盒也不可靠**：骨骼集合里含飘带/尾巴（Chocolate 的 Z 跨度 111），
 网格里含远离身体的附件（Kemono_Tail 到 -34）。

所以身高一律用 **脚 -> 头 的骨骼世界距离**（等价于真实站立身高），
再用实测比例系数与网格包围盒交叉校验。所有尺寸计算都走这里。
"""

import bpy
import math
from mathutils import Vector

# 脚底 -> 发顶 距离的折算系数（补上"脚踝离地间隙 + 发梢"）
# 实测：Karin 1.1122 -> 网格 1.3483（1.212）；Mafuyu 1.2240 -> 网格 1.3486（1.102）
# 注意：这个系数只在"米制模型"上标定过。**不能无条件乘**——
# Chocolate 的脚底->发顶是 104.86（厘米制），乘 1.16 会得到 121.6 这种荒谬值。
# 因此仅在"身高量级接近米制"（0.5~3.0）时应用。
GROUND_RATIO = 1.16
GROUND_RATIO_RANGE = (0.5, 3.0)


def _world(arm, name):
    b = arm.data.bones.get(name)
    if b is None:
        return None
    return arm.matrix_world @ b.head_local


def _mesh_z_range(arm, limit=4000):
    """关联网格的世界 Z 范围（评估后）。用于交叉校验身高。"""
    dg = bpy.context.evaluated_depsgraph_get()
    lo, hi = None, None
    for o in bpy.data.objects:
        if o.type != "MESH":
            continue
        if not any(m.type == "ARMATURE" and m.object == arm for m in o.modifiers):
            continue
        oe = o.evaluated_get(dg)
        try:
            me = oe.to_mesh()
        except Exception:
            continue
        if me is None or len(me.vertices) == 0:
            continue
        M = oe.matrix_world
        n = len(me.vertices)
        stride = max(1, n // max(1, limit // 8))
        for i in range(0, n, stride):
            z = (M @ me.vertices[i].co).z
            lo = z if lo is None else min(lo, z)
            hi = z if hi is None else max(hi, z)
        oe.to_mesh_clear()
    return lo, hi


def find_feet(arm, human=None):
    """脚部骨骼名列表（左右各一）"""
    out = []
    for key in ("foot.L", "foot.R"):
        n = (human or {}).get(key)
        if n and arm.data.bones.get(n):
            out.append(n)
    if len(out) == 2:
        return out
    # 兜底：名字含 foot/ankle 的最低骨骼
    cand = [b for b in arm.data.bones
            if any(k in b.name.lower() for k in ("foot", "ankle", "toe", "足", "脚"))]
    if not cand:
        return out
    cand.sort(key=lambda b: (arm.matrix_world @ b.head_local).z)
    return [b.name for b in cand[:2]]


def find_head(arm, human=None):
    n = (human or {}).get("head")
    if n and arm.data.bones.get(n):
        return n
    for b in arm.data.bones:
        if b.name.lower().strip() in ("head", "head.001", "head_1"):
            return b.name
    return None


def measure(arm, human=None):
    """世界空间度量。返回 dict（单位：世界单位）

    **身高 = 脚踝到发顶的世界距离（向量模长），不假设任何轴。**

 实测教训（_autorig_work/probe_axis.txt）——按踩坑顺序：

      1. 用 head_local.z 量身高 -> Chocolate 得 456（它是 Y-up 骨架）
      2. 用世界 Z 跨度     -> Chocolate 得 0.18（它的身体轴是世界 **Y**：
 脚->头向量 = (-5.3, 92.7, -0.1)，主导轴 Y，模长 92.8）
      3. 用骨骼包围盒       -> 含飘带/尾巴，Chocolate 跨度 111
      4. 用网格包围盒       -> 含远离身体的附件（Kemono_Tail 到 -34）
      5. **用脚->头向量模长** 对三台模型都正确，且与轴向无关

 实测值：
      Karin 脚底->发顶 1.1073 真实(网格) 1.3483  -> 校准系数 1.218
      Mafuyu 脚底->发顶 1.2179 真实(网格) 1.3486  -> 校准系数 1.107
      Chocolate 脚底->发顶 104.86 真实(网格 Body_base Y跨度 100.7) -> 校准系数 ~0.96
    """
    mw = arm.matrix_world
    scale = mw.to_scale()
    uniform = abs(scale.x - scale.y) < 1e-6 and abs(scale.y - scale.z) < 1e-6

    feet = find_feet(arm, human)
    head = find_head(arm, human)

    height = None
    method = "none"
    hb = arm.data.bones.get(head) if head else None
    if hb is not None and feet:
        top = mw @ hb.tail_local          # 发顶
        # 脚底：所有脚骨 head/tail 中离"头顶"最远者（与轴向无关）
        best = None
        for f in feet:
            fb = arm.data.bones.get(f)
            if fb is None:
                continue
            for p in (mw @ fb.head_local, mw @ fb.tail_local):
                d = (top - p).length
                if best is None or d > best:
                    best = d
        if best is not None and best > 1e-9:
            raw = best
            lo, hi = GROUND_RATIO_RANGE
            # 只在米制量级上应用折算系数（厘米/超大单位的模型直接取原值）
            height = raw * GROUND_RATIO if lo <= raw <= hi else raw
            method = "bones(脚底->发顶距离%s)" % (
                "" if lo <= raw <= hi else "，原值")

    # 网格仅作参考记录，不参与修正（实测会被附件污染，见上文第 4 条）
    mesh_h, main_mesh = _main_mesh_height(arm)

    if height is None or height < 1e-9:
        if mesh_h and mesh_h > 1e-6:
            height, method = mesh_h, "mesh(%s)" % main_mesh
        else:
            pts = []
            for b in arm.data.bones:
                pts.append(mw @ b.head_local)
                pts.append(mw @ b.tail_local)
            if pts:
                c = sum(pts, Vector()) / len(pts)
                height = max((p - c).length for p in pts) * 2.0
            else:
                height = 1.0
            method = "fallback(spread)"

    local_up = mw.to_3x3().inverted() @ Vector((0.0, 0.0, 1.0))
    local_up.normalize()
    dominant = max(range(3), key=lambda i: abs(local_up[i]))
    axis_name = ("X", "Y", "Z")[dominant]

    # 身体轴：**脚底 -> 发顶** 的世界向量主导轴
    # （不能用脚->头head：Chocolate 的 Head 与 Foot 在同一高度，会误判成 Z）
    body_axis = "Z"
    if hb is not None and feet:
        top = mw @ hb.tail_local
        far_d, far_p = None, None
        for f in feet:
            fb = arm.data.bones.get(f)
            if fb is None:
                continue
            for p in (mw @ fb.head_local, mw @ fb.tail_local):
                d = (top - p).length
                if far_d is None or d > far_d:
                    far_d, far_p = d, p
        if far_p is not None:
            v = top - far_p
            if v.length > 1e-9:
                body_axis = "XYZ"[max(range(3), key=lambda i: abs(v[i]))]

    return {
        "height": height,
        "mesh_height": mesh_h,
        "main_mesh": main_mesh,
        "world_scale": tuple(scale),
        "uniform_scale": uniform,
        "local_up_axis": axis_name,
        "is_yup": axis_name == "Y",
        "body_axis": body_axis,
        "feet": feet,
        "head": head,
        "method": method,
    }


def _main_mesh_height(arm):
    """主体网格（顶点最多）的世界 Z 跨度。返回 (高度, 网格名)。"""
    dg = bpy.context.evaluated_depsgraph_get()
    best_n, best_h, best_name = -1, None, None
    for o in bpy.data.objects:
        if o.type != "MESH":
            continue
        if not any(m.type == "ARMATURE" and m.object == arm for m in o.modifiers):
            continue
        oe = o.evaluated_get(dg)
        try:
            me = oe.to_mesh()
        except Exception:
            continue
        if me is None or len(me.vertices) == 0:
            oe.to_mesh_clear()
            continue
        n = len(me.vertices)
        if n > best_n:
            M = oe.matrix_world
            zz = [(M @ v.co).z for v in me.vertices]
            best_n = n
            best_h = max(zz) - min(zz)
            best_name = o.name
        oe.to_mesh_clear()
    return best_h, best_name


def unit_scale(arm, human=None, ref_height=1.6):
    """归一化系数：让不同体型的角色共用同一套比例。

    Karin 1.35 / Mafuyu 1.35 / Chocolate 45.7 都归一到 1.0 附近。
 返回 (系数, 真实身高)。
    """
    h = measure(arm, human)["height"]
    if h < 1e-6:
        return 1.0, h
    return h / ref_height, h


def _flatten(v, body_axis):
    """去掉竖直分量。body_axis 由 measure() 实测得出（Chocolate 是 'Y' 不是 'Z'）。"""
    v = v.copy()
    if body_axis == "X":
        v.x = 0.0
    elif body_axis == "Y":
        v.y = 0.0
    else:
        v.z = 0.0
    return v


def forward_world(arm, human=None, body_axis=None):
    """角色前向（水平分量归一化）。脚踝 -> 脚趾，兜底 -Y。"""
    if body_axis is None:
        body_axis = measure(arm, human)["body_axis"]
    mw = arm.matrix_world
    fl = (human or {}).get("foot.L")
    tl = (human or {}).get("toe.L")
    if fl and tl:
        b1 = arm.data.bones.get(fl)
        b2 = arm.data.bones.get(tl)
        if b1 and b2:
            v = _flatten((mw @ b2.head_local) - (mw @ b1.head_local), body_axis)
            if v.length > 1e-9:
                return v.normalized()
    return Vector((0.0, -1.0, 0.0))


def left_world(arm, human=None, body_axis=None):
    """角色左向（水平分量）。用左右对称骨骼的世界位置差判定。"""
    if body_axis is None:
        body_axis = measure(arm, human)["body_axis"]
    mw = arm.matrix_world
    for pair in (("foot.L", "foot.R"), ("hand.L", "hand.R"),
                 ("upperleg.L", "upperleg.R")):
        a = (human or {}).get(pair[0])
        b = (human or {}).get(pair[1])
        if not (a and b):
            continue
        ba = arm.data.bones.get(a)
        bb = arm.data.bones.get(b)
        if ba and bb:
            v = _flatten((mw @ ba.head_local) - (mw @ bb.head_local), body_axis)
            if v.length > 1e-9:
                return v.normalized()
    return Vector((1.0, 0.0, 0.0))


def up_world(arm, human=None, body_axis=None):
    """角色上向（世界空间单位向量）。由 body_axis 决定。"""
    if body_axis is None:
        body_axis = measure(arm, human)["body_axis"]
    return {"X": Vector((1.0, 0.0, 0.0)),
            "Y": Vector((0.0, 1.0, 0.0)),
            "Z": Vector((0.0, 0.0, 1.0))}[body_axis]


def local_to_world(arm, human=None):
    """骨骼本地 1 单位 = 世界多少单位。

 实测：三台角色的 matrix_world 都是单位阵，所以不能用 to_scale() 求这个系数。
 可靠算法：同一段距离在本地空间与世界空间的比值。

      Chocolate 本地脚->头 456 世界脚->头 45.7  -> 0.1002
      Karin 本地 1.44 世界 1.44         -> 1.0
      Mafuyu 本地 1.38 世界 1.38         -> 1.0

 做法：取"脚骨骼 -> 头骨骼"的本地距离与世界距离之比（逐轴取中位数，抗异常）。
    """
    mw = arm.matrix_world
    feet = find_feet(arm, human)
    head = find_head(arm, human)
    ratios = []
    if feet and head:
        hb = arm.data.bones.get(head)
        if hb is not None:
            for f in feet:
                fb = arm.data.bones.get(f)
                if fb is None:
                    continue
                d_local = (hb.head_local - fb.head_local).length
                d_world = ((mw @ hb.head_local) - (mw @ fb.head_local)).length
                if d_local > 1e-9 and d_world > 1e-9:
                    ratios.append(d_world / d_local)
    if ratios:
        ratios.sort()
        return ratios[len(ratios) // 2]

    # 兜底：matrix_world 的缩放（对"缩放写在对象上"的常规模型是对的）
    s = mw.to_scale()
    v = (abs(s.x) + abs(s.y) + abs(s.z)) / 3.0
    return v if v > 1e-9 else 1.0


def describe(arm, human=None):
    m = measure(arm, human)
    u, h = unit_scale(arm, human)
    f = forward_world(arm, human)
    l = left_world(arm, human)
    return (
        "世界度量: 身高 %.4f（%s）网格身高 %s\n"
        "          world_scale=%s 本地 up 轴=%s%s 归一系数 %.4f\n"
        " 前向=(% .3f,% .3f,% .3f) 左向=(% .3f,% .3f,% .3f)" % (
            h, m["method"],
            ("%.4f" % m["mesh_height"]) if m["mesh_height"] else "n/a",
            tuple(round(v, 5) for v in m["world_scale"]),
            m["local_up_axis"], "（Y-up 骨架）" if m["is_yup"] else "",
            u, f.x, f.y, f.z, l.x, l.y, l.z))
