# -*- coding: utf-8 -*-
"""验证：ez 一键处理后，Root 控制器是否正确带动角色

用户报「root ik 不会带动角色移动，只会带动手柄移动」。
上一轮诊断发现：那个模型**没有跑过 ez 一键处理**（profile.root 为 None，
没有 ez_Root），discover 把 Hips 当成了 root，而手柄全都没挂在它下面。

本脚本验证 ez 处理后**应该**是什么行为，并复现"没挂上"的对比。

用法: blender -b --factory-startup -P _autorig_work\verify_root.py
"""
import bpy, os, sys, json, shutil
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(__file__))
ASS = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ASS, "ez_v2b_animation"))
import ez_discovery as D
import ez_builder as B
import ez_metrics as M

log = []
def P(s=""):
    log.append(str(s))

fails = []
def chk(cond, msg):
    if not cond:
        fails.append(msg)
        P("  !! 失败: %s" % msg)
    else:
        P("  OK  %s" % msg)


def upd(n=12):
    for _ in range(n):
        bpy.context.view_layer.update()
        bpy.context.evaluated_depsgraph_get().update()


def unhide():
    for c in bpy.data.collections:
        c.hide_viewport = False
    vl = bpy.context.scene.view_layers[0]

    def walk(lc):
        lc.exclude = False
        lc.hide_viewport = False
        for ch in lc.children:
            walk(ch)
    walk(vl.layer_collection)


def wh(arm, bone):
    dg = bpy.context.evaluated_depsgraph_get()
    ev = arm.evaluated_get(dg)
    return (arm.matrix_world @ ev.pose.bones[bone].matrix).to_translation().copy()


def move_and_measure(arm, root_name, watch, dist=0.30):
    """移动 Root 骨骼 dist（世界单位），返回各部位位移。

    注意：本地 1 单位对应多少世界单位，要用 ez_metrics.local_to_world 实测，
    **不能假设 matrix_local 含缩放**。
    实测踩过（Chocolate 骨架缩放 0.01）：
      bone.matrix_local 的缩放是 1.0，真正的关系在世界矩阵上。
      直接用 matrix_local 反推本地位移，会少乘 1/0.01，
      于是"移动 0.30"实际只动了 0.003，测试误报"未跟随"。
    """
    pbr = arm.pose.bones.get(root_name)
    if pbr is None:
        return None
    l2w = M.local_to_world(arm, None) or 1.0
    base = {n: wh(arm, n) for n in watch if arm.pose.bones.get(n)}
    mb = pbr.matrix_basis.copy()
    # 世界位移 -> 骨骼本地位移：除以"本地到世界"的换算系数
    local_delta = Vector((dist / l2w, 0.0, 0.0))
    pbr.matrix_basis = mb.copy()
    pbr.matrix_basis.translation = mb.translation + local_delta
    arm.update_tag()
    upd(16)
    out = {}
    for n in base:
        out[n] = (wh(arm, n) - base[n]).length
    pbr.matrix_basis = mb
    arm.update_tag()
    upd(12)
    return out


P("=" * 78)
P("Root 控制器行为验证")
P("=" * 78)

work = os.path.join(HERE, "work")
os.makedirs(work, exist_ok=True)

CASES = [
    ("Mafuyu", os.path.join(ASS, "Mafuyu.blend"), "Armature"),
    ("Karin", os.path.join(ASS, "角色.blend"), "Armature"),
    ("Chocolate", os.path.join(ASS, "角色.blend"), "Armature.001"),
]

for label, src, arm_name in CASES:
    if not os.path.isfile(src):
        continue
    dst = os.path.join(work, "root_%s.blend" % label)
    shutil.copy2(src, dst)
    bpy.ops.wm.open_mainfile(filepath=dst)
    arm = bpy.data.objects.get(arm_name)
    if arm is None:
        continue
    unhide()
    upd()

    P("")
    P("=" * 78)
    P("用例: %s" % label)
    P("=" * 78)

    # ---------- 处理前
    info0 = D.discover(arm)
    root0 = info0.get("root")
    raw0 = arm.get("rig_profile")
    P("  处理前:")
    P("    discover root = %s" % root0)
    P("    profile 存在  = %s" % (raw0 is not None))
    if raw0:
        P("    profile.root  = %r" % json.loads(raw0).get("root"))

    # ---------- 跑一键处理
    rep = B.build(arm)
    upd(12)
    P("")
    P("  一键处理完成（骨骼 +%d，约束 +%d，驱动 +%d）" % (
        len(rep["created"]["bones"]), rep["created"]["constraints"],
        rep["created"]["drivers"]))

    info = D.discover(arm)
    raw = arm.get("rig_profile")
    cfg = json.loads(raw) if raw else {}
    root_name = cfg.get("root") or info.get("root")
    hips_ctrl = cfg.get("hips_ctrl")

    P("")
    P("  处理后:")
    P("    profile.root = %s" % root_name)
    P("    profile.hips = %s" % hips_ctrl)

    chk(root_name is not None, "%s: profile 里有 root" % label)
    if not root_name:
        continue
    rb = arm.data.bones.get(root_name)
    chk(rb is not None, "%s: root 骨骼 %s 存在" % (label, root_name))
    if rb is None:
        continue

    # ---------- 层级检查
    kids = [b.name for b in rb.children]
    P("")
    P("  %s 的直接子级 %d 个:" % (root_name, len(kids)))
    for k in kids:
        P("    %s" % k)

    expect = []
    for limb in ("arm", "leg"):
        for side in ("L", "R"):
            e = info["ik"]["limbs"][limb].get(side)
            if e:
                expect.append(e["handle"])
                if e.get("pole"):
                    expect.append(e["pole"])
    if info["ik"]["head"]:
        expect.append(info["ik"]["head"]["handle"])
    if hips_ctrl:
        expect.append(hips_ctrl)
    for n in ("IKFK_Arm.L", "IKFK_Arm.R", "IKFK_Leg.L", "IKFK_Leg.R"):
        if arm.data.bones.get(n):
            expect.append(n)

    missing = [n for n in expect if n not in kids]
    P("")
    P("  期望挂在 %s 下的控制器 %d 个" % (root_name, len(expect)))
    if missing:
        P("  未挂上 %d 个: %s" % (len(missing), missing))
    else:
        P("  全部已挂上")
    chk(not missing, "%s: 所有控制器都挂在 Root 下（缺 %s）" % (
        label, missing or "无"))

    # ---------- 实测拖动
    P("")
    P("  实测：移动 %s +0.30" % root_name)
    watch = []
    for limb in ("arm", "leg"):
        for side in ("L", "R"):
            e = info["ik"]["limbs"][limb].get(side)
            if e:
                watch.append(e["handle"])
                if e.get("rot_owner"):
                    watch.append(e["rot_owner"])
    if info["ik"]["head"]:
        watch.append(info["ik"]["head"]["handle"])
    if hips_ctrl and arm.pose.bones.get(hips_ctrl):
        watch.append(hips_ctrl)
    for n in ("Hips", "Head", "Left Hand", "Right Hand", "Foot_L", "Foot_R",
              "LeftHand", "RightHand", "LeftFoot", "RightFoot"):
        if arm.pose.bones.get(n):
            watch.append(n)
    watch = list(dict.fromkeys(watch))

    res = move_and_measure(arm, root_name, watch)
    if res is None:
        P("    无法移动 %s" % root_name)
        continue

    P("    %-30s %10s  %s" % ("部位", "位移", "判定"))
    P("    " + "-" * 62)
    for n, d in sorted(res.items(), key=lambda x: -x[1]):
        tag = "跟随" if d > 0.25 else ("未跟随  <-- 问题" if d < 0.02 else "部分")
        P("    %-30s %10.5f  %s" % (n, d, tag))

    # 判据：本体骨骼 + 手柄 都必须跟随
    body = [n for n in res if n in ("Hips", "Head", "Left Hand", "Right Hand",
                                    "Foot_L", "Foot_R", "LeftHand", "RightHand",
                                    "LeftFoot", "RightFoot")]
    handles = [n for n in res if n in expect and n not in body]
    if body:
        bmin = min(res[n] for n in body)
        chk(bmin > 0.25, "%s: 本体骨骼跟随 Root（最小 %.5f）" % (label, bmin))
    if handles:
        hmin = min(res[n] for n in handles)
        chk(hmin > 0.25, "%s: 手柄跟随 Root（最小 %.5f，%d 个）" % (
            label, hmin, len(handles)))

    # ---------- 姿态不能被带偏
    P("")
    P("  姿态检查（Root 移动后，Hips 在骨架空间的偏移）:")
    if arm.pose.bones.get("Hips"):
        pbh = arm.pose.bones["Hips"]
        o = pbh.matrix_basis.copy()
        # 移动 Root（同样用实测的本地到世界换算）
        pbr = arm.pose.bones[root_name]
        mb = pbr.matrix_basis.copy()
        l2w = M.local_to_world(arm, None) or 1.0
        pbr.matrix_basis = mb.copy()
        pbr.matrix_basis.translation = mb.translation + \
            Vector((0.30 / l2w, 0.0, 0.0))
        arm.update_tag()
        upd(16)
        delta = (pbh.matrix_basis.to_translation() - o.to_translation()).length
        pbr.matrix_basis = mb
        arm.update_tag()
        upd(12)
        P("    Hips 自身 matrix_basis 偏移 = %.6f" % delta)
        chk(delta < 0.001,
            "%s: Root 移动时 Hips 姿态没被带偏（%.6f）" % (label, delta))

    bpy.ops.wm.save_as_mainfile(filepath=dst)

P("")
P("=" * 78)
if fails:
    P("失败 %d 项:" % len(fails))
    for f in fails:
        P("  - %s" % f)
else:
    P("全部通过：Root 控制器正确带动整个角色")
P("=" * 78)

out = os.path.join(HERE, "verify_root.txt")
open(out, "w", encoding="utf-8").write("\n".join(log))
print("\n".join(log))
sys.stdout.flush()
if fails:
    sys.exit(1)
