# -*- coding: utf-8 -*-
# ---------------------------------------------------------------------------
# ez-v2b-animation  ——  作者：B站 @高压郭炖大葱
# 使用、修改、二次分发请保留本署名。
# ---------------------------------------------------------------------------
# 由 port_karin.py 从 karin_ik_tools 移植并适配（勿手改，改 port_karin.py 重跑）
# 命名空间: karin.* -> ez.*  /  KARIN_OT_ -> EZ_OT_  /  面板分类 -> EZ V2B
# 共享工具由 ez_bridge 注入（_rig / _ensure_rigs / _upd / _face_meshes 等）

import bpy
import math
import json
import os
import re
import sys
import time
import wave
import queue
import threading
import subprocess
import numpy as np
from mathutils import Matrix, Vector, Quaternion, Euler

try:
    from .ez_bridge import *
    from .ez_bridge import D, M, B, RIGS, SIDE_ITEMS
except Exception:
    from ez_bridge import *
    from ez_bridge import D, M, B, RIGS, SIDE_ITEMS

TWIST_CNAME = "扭转传递"
TWIST_DEFAULT = 0.4

# ---------------------------------------------------------------- 前臂定位
def _ez_forearm(arm, arm_cfg):
    """取前臂骨骼（扭转传递的挂载点）。

    这个函数被两个坑逼出来的（用户先后报"上臂旋转不生效" / "依旧显示无前臂骨"）：

      坑一：chain 是"IK 骨骼沿父级向上 chain_count 根"。
            chain_count=2 时只有 [Lowerarm, Upperarm] 两个元素，chain[2] 越界。
            原实现 `chain[2] if len(chain) > 2 else None` -> None -> 面板显示"无前臂骨"。

      坑二：**profile 的 `ik` 字段存的是手柄名，不是 IK 骨骼名**。
            （原插件 karin 的约定就是如此：ik = LHik 这种手柄）
            手柄上没有 IK 约束，拿它去找前臂必然失败。

    解析顺序（每一层都有实测依据）：
      1. profile["ik_bone"]      <- ez 新写的字段，明确就是 IK 约束所在骨骼
      2. profile["chain"][0]     <- chain 首项 = IK 骨骼（向上数的第一根）
      3. profile["ik"] 当骨骼名试   <- 兼容"ik 存 IK 骨骼"的写法
      4. profile["ik"] 当手柄名，在场景里反查哪个骨骼的 IK 约束指向它
      5. 都失败 -> 返回 None（调用方给提示）
    """
    if not arm_cfg:
        return None

    # 1) 明确字段
    n = arm_cfg.get("ik_bone")
    if n and arm.pose.bones.get(n) is not None:
        return n

    # 2) chain 首项
    chain = arm_cfg.get("chain") or []
    if chain and arm.pose.bones.get(chain[0]) is not None:
        return chain[0]

    # 3) ik 字段当骨骼名
    n = arm_cfg.get("ik")
    if n and arm.pose.bones.get(n) is not None:
        pb = arm.pose.bones[n]
        # 有 IK 约束 -> 它就是 IK 骨骼，直接用
        if any(c.type == "IK" for c in pb.constraints):
            return n
        # 没有 -> 可能是手柄，继续往下试

    # 4) ik 当手柄名，反查哪个骨骼的 IK 约束指向它
    if n:
        for pb in arm.pose.bones:
            for c in pb.constraints:
                if c.type == "IK" and c.subtarget == n:
                    return pb.name

    # 5) 兜底：把 ik 字段原样返回（调用方会判 None）
    return n or None



def _rig(name):
    rigs = _ensure_rigs()
    cfg = rigs.get(name)
    if cfg is None:
        return None, None
    arm = bpy.data.objects.get(cfg["object"])
    if arm is None or arm.type != "ARMATURE":
        return cfg, None
    return cfg, arm


def _has_switch(arm, cfg):
    """该角色是否已经装好切换器"""
    if arm is None:
        return False
    for side in ("L", "R"):
        if arm.pose.bones.get(cfg["arms"][side]["sw"]) is None:
            return False
    return True


def p_upd():
    for _ in range(3):
        bpy.context.view_layer.update()
        bpy.context.evaluated_depsgraph_get().update()


def p_eval_matrix(arm, bone_name):
    dg = bpy.context.evaluated_depsgraph_get()
    return arm.evaluated_get(dg).pose.bones[bone_name].matrix.copy()


def p_apply_world_rotation(arm, bone_name, target_matrix):
    """只把世界旋转写入 FK 通道，保留位置（肩部等非连接骨骼不被位移干扰）"""
    pb = arm.pose.bones[bone_name]
    if pb.parent is None:
        rest_rel = pb.bone.matrix_local
        parent_pose = Matrix.Identity(4)
    else:
        rest_rel = pb.parent.bone.matrix_local.inverted() @ pb.bone.matrix_local
        parent_pose = pb.parent.matrix
    basis = (parent_pose @ rest_rel).inverted() @ target_matrix.to_3x3().to_4x4()
    pb.rotation_quaternion = basis.to_quaternion()


def p_sides(side):
    return ["L", "R"] if side == "BOTH" else [side]


def p_set_switch(arm, bone_name, value):
    pb = arm.pose.bones.get(bone_name)
    if pb is not None:
        pb["IKFK"] = value


def p_ensure_fk_truth(arm, chain, sw_bone):
    """处于 IK 模式时先把解算结果烘焙进 FK 骨骼，使 FK 通道等于可见姿态"""
    pb_sw = arm.pose.bones.get(sw_bone)
    if pb_sw is None or float(pb_sw.get("IKFK", 1.0)) <= 0.5:
        return
    mats = [p_eval_matrix(arm, n) for n in chain if arm.pose.bones.get(n)]
    p_set_switch(arm, sw_bone, 0.0)
    p_upd()
    for n, m in zip([n for n in chain if arm.pose.bones.get(n)], mats):
        p_apply_world_rotation(arm, n, m)
        p_upd()


class _SnapBase:
    rig: bpy.props.EnumProperty(name="角色", items=_rig_items)
    side: bpy.props.EnumProperty(name="侧", items=SIDE_ITEMS, default="BOTH")

    table = "arms"

    def _do(self, context, bake_back):
        cfg, arm = _rig(self.rig)
        if arm is None:
            self.report({"WARNING"}, "未找到骨架 %s" % (self.rig,))
            return {"CANCELLED"}
        if not _has_switch(arm, cfg):
            self.report({"WARNING"}, "%s 尚未安装 IK/FK 切换器" % cfg["label"])
            return {"CANCELLED"}
        for s in p_sides(self.side):
            c = cfg[self.table][s]
            if bake_back:
                mats = [p_eval_matrix(arm, n) for n in c["chain"]]
                p_set_switch(arm, c["sw"], 0.0)
                p_upd()
                for n, m in zip(c["chain"], mats):
                    p_apply_world_rotation(arm, n, m)
                    p_upd()
            else:
                p_ensure_fk_truth(arm, c["chain"], c["sw"])
                p_set_switch(arm, c["sw"], 0.0)
                p_upd()
                world = arm.matrix_world @ p_eval_matrix(arm, c["tip"])
                p_set_ctrl_to_world(arm, cfg, c["ik"], world)
                p_upd()
                p_set_switch(arm, c["sw"], 1.0)
                p_upd()
        return {"FINISHED"}


def p_ctrl_parent_xform(arm, e):
    """空物体上 CHILD_OF 约束带来的父级变换（世界空间）"""
    x = Matrix.Identity(4)
    for c in e.constraints:
        if c.type != "CHILD_OF":
            continue
        if c.subtarget:
            pb = arm.pose.bones.get(c.subtarget)
            if pb is not None:
                x = (arm.matrix_world @ pb.matrix) @ c.inverse_matrix
        else:
            x = arm.matrix_world @ c.inverse_matrix
    return x


def p_set_ctrl_to_world(arm, cfg, ik_bone, world_matrix):
    """把 IK 控制柄移到给定世界矩阵。

    关键：骨骼本身被空物体的 COPY_TRANSFORMS 完全覆盖，直接写 pb.matrix 是无效的，
    必须移动空物体；没有空物体时才退回写骨骼。
    """
    ename = cfg.get("empties", {}).get(ik_bone)
    e = bpy.data.objects.get(ename) if ename else None
    if e is None:
        arm.pose.bones[ik_bone].matrix = arm.matrix_world.inverted() @ world_matrix
        return
    e.matrix_basis = p_ctrl_parent_xform(arm, e).inverted() @ world_matrix


class EZ_OT_snap_fk_to_ik(_SnapBase, bpy.types.Operator):
    bl_idname = "ez.snap_fk_to_ik"
    bl_label = "FK → IK"
    bl_description = "把 IK 控制器对齐到当前可见的 FK 姿势并切到 IK（任何模式下都不跳变）"
    bl_options = {"REGISTER", "UNDO"}
    table = "arms"

    def execute(self, context):
        return self._do(context, bake_back=False)


class EZ_OT_snap_ik_to_fk(_SnapBase, bpy.types.Operator):
    bl_idname = "ez.snap_ik_to_fk"
    bl_label = "IK → FK"
    bl_description = "把 IK 解算结果烘焙到 FK 骨骼并切到 FK（不跳变）"
    bl_options = {"REGISTER", "UNDO"}
    table = "arms"

    def execute(self, context):
        return self._do(context, bake_back=True)


class EZ_OT_snap_fk_to_ik_leg(_SnapBase, bpy.types.Operator):
    bl_idname = "ez.snap_fk_to_ik_leg"
    bl_label = "腿 FK → IK"
    bl_description = "把脚部 IK 控制器对齐到当前 FK 脚位"
    bl_options = {"REGISTER", "UNDO"}
    table = "legs"

    def execute(self, context):
        return self._do(context, bake_back=False)


class EZ_OT_snap_ik_to_fk_leg(_SnapBase, bpy.types.Operator):
    bl_idname = "ez.snap_ik_to_fk_leg"
    bl_label = "腿 IK → FK"
    bl_description = "把腿部 IK 解算结果烘焙到 FK 骨骼"
    bl_options = {"REGISTER", "UNDO"}
    table = "legs"

    def execute(self, context):
        return self._do(context, bake_back=True)


class EZ_OT_switch_mode(bpy.types.Operator):
    bl_idname = "ez.switch_mode"
    bl_label = "直接切换"
    bl_description = "直接切换模式（有跳变，建议用 Snap 按钮）"
    bl_options = {"REGISTER", "UNDO"}

    rig: bpy.props.EnumProperty(name="角色", items=_rig_items)
    side: bpy.props.EnumProperty(name="侧", items=SIDE_ITEMS, default="BOTH")
    target: bpy.props.EnumProperty(items=[("IK", "IK", ""), ("FK", "FK", "")], default="IK")
    use_legs: bpy.props.BoolProperty(name="包含腿部", default=False)

    def execute(self, context):
        cfg, arm = _rig(self.rig)
        if arm is None:
            return {"CANCELLED"}
        v = 1.0 if self.target == "IK" else 0.0
        for table, is_leg in (("arms", False), ("legs", True)):
            if is_leg and not self.use_legs:
                continue
            for s in p_sides(self.side):
                p_set_switch(arm, cfg[table][s]["sw"], v)
        p_upd()
        return {"FINISHED"}


class EZ_OT_reset_handles(bpy.types.Operator):
    bl_idname = "ez.reset_handles"
    bl_label = "控制器归位"
    bl_description = "把 IK 控制器骨骼移回默认（静置）位置"
    bl_options = {"REGISTER", "UNDO"}

    rig: bpy.props.EnumProperty(name="角色", items=_rig_items)

    def execute(self, context):
        cfg, arm = _rig(self.rig)
        if arm is None:
            return {"CANCELLED"}
        # 控制器已从空物体改为骨架内的骨骼（自定义形状）：
        # 手柄 9 根 + Hips + Root 直接清 pose 即回到静置位置。
        # Hips 必须显式列出 —— 它不在 cfg["handles"] 里。
        names = list(cfg["handles"]) + ["Hips", cfg.get("root", "")]
        for n in [x for x in dict.fromkeys(names) if x]:
            pb = arm.pose.bones.get(n)
            if pb is None:
                continue
            pb.location = (0.0, 0.0, 0.0)
            pb.rotation_mode = "QUATERNION"
            pb.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
            pb.rotation_euler = (0.0, 0.0, 0.0)
            pb.scale = (1.0, 1.0, 1.0)
        p_upd()
        return {"FINISHED"}


class EZ_OT_toggle_empty_control(bpy.types.Operator):
    bl_idname = "ez.toggle_empty_control"
    bl_label = "空物体控制 开/关"
    bl_description = "切换空物体对 IK 骨骼的控制；关闭时会把当前位置写回骨骼，不跳变"
    bl_options = {"REGISTER", "UNDO"}

    rig: bpy.props.EnumProperty(name="角色", items=_rig_items)

    def execute(self, context):
        cfg, arm = _rig(self.rig)
        if arm is None:
            return {"CANCELLED"}
        pairs = []
        for bone_name, ename in cfg["empties"].items():
            pb = arm.pose.bones.get(bone_name)
            if pb is None:
                continue
            for c in pb.constraints:
                if c.name == "Empty 控制":
                    pairs.append((pb, bpy.data.objects.get(ename), c))
        if not pairs:
            self.report({"WARNING"}, "未找到空物体控制约束")
            return {"CANCELLED"}
        new_mute = not pairs[0][2].mute
        if new_mute:
            inv = arm.matrix_world.inverted()
            for pb, e, c in pairs:
                if e is not None:
                    pb.matrix = inv @ e.matrix_world.copy()
            p_upd()
        for pb, e, c in pairs:
            c.mute = new_mute
        p_upd()
        self.report({"INFO"}, "%s 空物体控制已%s" % (cfg["label"], "关闭" if new_mute else "开启"))
        return {"FINISHED"}


class EZ_OT_toggle_ik_bones(bpy.types.Operator):
    bl_idname = "ez.toggle_ik_bones"
    bl_label = "显隐 IK 骨骼"
    bl_description = "显示/隐藏 IK 控制器骨骼（自定义形状骨骼）"
    bl_options = {"REGISTER", "UNDO"}

    rig: bpy.props.EnumProperty(name="角色", items=_rig_items)

    def execute(self, context):
        cfg, arm = _rig(self.rig)
        if arm is None:
            return {"CANCELLED"}
        c = arm.data.collections.get(cfg["collections"]["ik"])
        if c is None:
            return {"CANCELLED"}
        c.is_visible = not c.is_visible
        return {"FINISHED"}


class EZ_OT_select_empties(bpy.types.Operator):
    bl_idname = "ez.select_empties"
    bl_label = "选中全部控制器"
    bl_description = "选中该角色的所有 IK 控制器骨骼（含 Hips 与 Root）"
    bl_options = {"REGISTER", "UNDO"}

    rig: bpy.props.EnumProperty(name="角色", items=_rig_items)

    def execute(self, context):
        cfg, arm = _rig(self.rig)
        if arm is None:
            return {"CANCELLED"}
        # 控制器已改为骨架内的骨骼（自定义形状），选中它们而不是空物体
        names = [x for x in dict.fromkeys(list(cfg["handles"]) + ["Hips", cfg.get("root", "")]) if x]
        pbs = [arm.pose.bones.get(n) for n in names]
        pbs = [p for p in pbs if p is not None]
        if not pbs:
            return {"CANCELLED"}
        for o in bpy.context.selected_objects:
            o.select_set(False)
        arm.select_set(True)
        bpy.context.view_layer.objects.active = arm
        try:
            if context.mode != "POSE":
                bpy.ops.object.mode_set(mode="POSE")
        except Exception:
            pass
        for p in pbs:
            p.bone.select = True
        arm.data.bones.active = arm.data.bones[pbs[0].name]
        return {"FINISHED"}


class EZ_OT_toggle_decoration(bpy.types.Operator):
    bl_idname = "ez.toggle_decoration"
    bl_label = "显隐装饰骨骼"
    bl_description = "显示/隐藏头发、服装等装饰骨骼"
    bl_options = {"REGISTER", "UNDO"}

    rig: bpy.props.EnumProperty(name="角色", items=_rig_items)

    def execute(self, context):
        cfg, arm = _rig(self.rig)
        if arm is None:
            return {"CANCELLED"}
        names = cfg["collections"]["deco"]
        state = None
        for n in names:
            c = arm.data.collections.get(n)
            if c is not None:
                if state is None:
                    state = c.is_visible
                c.is_visible = not state
        return {"FINISHED"}


class EZ_OT_show_rig(bpy.types.Operator):
    bl_idname = "ez.show_rig"
    bl_label = "显示角色"
    bl_description = "在视图层中只显示选定的角色（两个角色位置重叠，互斥显示）"
    bl_options = {"REGISTER", "UNDO"}

    rig: bpy.props.EnumProperty(name="角色", items=_rig_items_all)

    def execute(self, context):
        vl = context.scene.view_layers[0]
        for name, cfg in RIGS.items():
            lc = vl.layer_collection.children.get(cfg["view_layer_collection"])
            if lc is None:
                continue
            lc.exclude = (name != self.rig)
        return {"FINISHED"}


class EZ_OT_select_rig(bpy.types.Operator):
    bl_idname = "ez.select_rig"
    bl_label = "选中骨架并进入姿态模式"
    bl_options = {"REGISTER", "UNDO"}

    rig: bpy.props.EnumProperty(name="角色", items=_rig_items)

    def execute(self, context):
        cfg, arm = _rig(self.rig)
        if arm is None:
            self.report({"WARNING"}, "未找到骨架 %s" % self.rig)
            return {"CANCELLED"}
        vl = context.scene.view_layers[0]
        lc = vl.layer_collection.children.get(cfg["view_layer_collection"])
        if lc is not None and lc.exclude:
            lc.exclude = False
        for o in bpy.context.selected_objects:
            o.select_set(False)
        arm.select_set(True)
        bpy.context.view_layer.objects.active = arm
        if arm.mode != "POSE":
            bpy.ops.object.mode_set(mode="POSE")
        return {"FINISHED"}


class EZ_OT_refresh_rigs(bpy.types.Operator):
    bl_idname = "ez.refresh_rigs"
    bl_label = "重新扫描场景里的角色"
    bl_description = "重新读取各骨架上的 rig_profile，刷新角色列表"

    def execute(self, context):
        global _LAST_SIG
        _LAST_SIG = None
        rigs, notes = _load_rigs()
        for n in notes:
            self.report({"WARNING"}, n)
        if not rigs:
            self.report({"WARNING"}, "未发现带 rig_profile 的骨架")
        else:
            self.report({"INFO"}, "发现 %d 个角色: %s" % (len(rigs), "、".join(sorted(rigs))))
        _redraw()
        return {"FINISHED"}


class EZ_OT_toggle_rig_panel(bpy.types.Operator):
    bl_idname = "ez.toggle_rig_panel"
    bl_label = "折叠/展开该角色的面板"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    rig: bpy.props.EnumProperty(name="角色", items=_rig_items)

    def execute(self, context):
        cfg, arm = _rig(self.rig)
        if arm is None:
            return {"CANCELLED"}
        arm["kls_open"] = not bool(arm.get("kls_open", True))
        _redraw()
        return {"FINISHED"}


class EZ_OT_twist_enable(bpy.types.Operator):
    bl_idname = "ez.twist_enable"
    bl_label = "安装前臂扭转传递"
    bl_description = ("给两侧前臂各加一条 COPY_ROTATION（目标=IK 手柄，LOCAL 空间、"
                      "只取绕骨长轴的扭转、mix_mode=BEFORE 排在 IK 之前）。"
                      "静置零变化，转手腕时小臂按 influence 比例跟随")
    bl_options = {"REGISTER", "UNDO"}

    rig: bpy.props.EnumProperty(name="角色", items=_rig_items)

    def execute(self, context):
        cfg, arm = _rig(self.rig)
        if arm is None:
            self.report({"ERROR"}, "未找到骨架")
            return {"CANCELLED"}
        made = []
        for s in ("L", "R"):
            ac = cfg["arms"][s]
            fa = _ez_forearm(arm, ac)
            handle = ac.get("ik")
            pb = arm.pose.bones.get(fa) if fa else None
            if pb is None or arm.pose.bones.get(handle) is None:
                continue
            for c in list(pb.constraints):
                if c.name == TWIST_CNAME:
                    pb.constraints.remove(c)
            con = pb.constraints.new("COPY_ROTATION")
            con.name = TWIST_CNAME
            con.target = arm
            con.subtarget = handle
            con.target_space = "LOCAL"
            con.owner_space = "LOCAL"
            con.mix_mode = "BEFORE"
            con.influence = TWIST_DEFAULT
            con.use_x, con.use_y, con.use_z = (False, True, False)
            ik = pb.constraints.find("IK")
            ti = pb.constraints.find(TWIST_CNAME)
            if ik >= 0 and ti > ik:
                pb.constraints.move(ti, ik)
            made.append(fa)
        if not made:
            self.report({"WARNING"}, "没有可处理的前臂，请检查该角色的 profile")
            return {"CANCELLED"}
        self.report({"INFO"}, "已安装扭转传递: %s（比例 %.0f%%）" % (
            ", ".join(made), TWIST_DEFAULT * 100))
        _redraw()
        return {"FINISHED"}


class EZ_OT_rig_panel_all(bpy.types.Operator):
    bl_idname = "ez.rig_panel_all"
    bl_label = "全部展开或折叠"
    bl_description = "一次性展开/折叠所有角色的面板（折叠状态保存在各自的骨架上）"
    bl_options = {"REGISTER", "UNDO"}

    mode: bpy.props.EnumProperty(
        name="模式", items=[("OPEN", "全部展开", ""), ("CLOSE", "全部折叠", "")],
        default="OPEN")

    def execute(self, context):
        rigs = _ensure_rigs()
        if not rigs:
            self.report({"WARNING"}, "未发现角色")
            return {"CANCELLED"}
        val = (self.mode == "OPEN")
        n = 0
        for k in rigs:
            cfg, arm = _rig(k)
            if arm is None:
                continue
            arm["kls_open"] = val
            n += 1
        self.report({"INFO"}, "已%s %d 个角色面板" % ("展开" if val else "折叠", n))
        _redraw()
        return {"FINISHED"}


class EZ_PT_ik_fk(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "EZ V2B"
    bl_label = "IK / FK 控制"
    bl_idname = "EZ_PT_ik_fk"

    def draw(self, context):
        layout = self.layout
        rigs = _ensure_rigs()
        if not rigs:
            layout.label(text="未发现角色", icon="ERROR")
            layout.label(text="骨架需带 rig_profile 属性", icon="INFO")
            layout.operator("ez.refresh_rigs", icon="FILE_REFRESH")
            return

        row = layout.row(align=True)
        row.label(text="已发现 %d 个角色" % len(rigs), icon="ARMATURE_DATA")
        row.operator("ez.refresh_rigs", text="", icon="FILE_REFRESH")
        row.operator("ez.rig_panel_all", text="", icon="TRIA_DOWN").mode = "OPEN"
        row.operator("ez.rig_panel_all", text="", icon="TRIA_RIGHT").mode = "CLOSE"

        for rig_name in sorted(rigs):
            cfg, arm = _rig(rig_name)
            box = layout.box()
            open_ = bool(arm.get("kls_open", True)) if arm else False
            head = box.row(align=True)
            o = head.operator("ez.toggle_rig_panel", text="",
                              icon="TRIA_DOWN" if open_ else "TRIA_RIGHT", emboss=False)
            o.rig = rig_name
            head.label(text=cfg["label"], icon="ARMATURE_DATA")
            if arm is None:
                box.label(text="未找到骨架 %s" % cfg["object"], icon="ERROR")
                continue
            head.operator("ez.select_rig", text="", icon="RESTRICT_SELECT_OFF").rig = rig_name
            if not _has_switch(arm, cfg):
                box.label(text="尚未安装 IK/FK 切换器", icon="INFO")
                continue
            if not open_:
                # 折叠时给一行摘要，免得看起来像坏了
                npb = len([c for c in cfg["arms"].values()
                           if arm.pose.bones.get(c.get("sw", ""))]) \
                    + len([c for c in cfg["legs"].values()
                           if arm.pose.bones.get(c.get("sw", ""))])
                box.label(text="已折叠 — 臂/腿切换器 %d 项，点箭头展开" % npb, icon="INFO")
                continue

            col = box.column(align=True)
            col.label(text="手臂 IK / FK")
            for s in ("L", "R"):
                c = cfg["arms"][s]
                pb = arm.pose.bones.get(c["sw"])
                if pb is None:
                    continue
                r = col.row(align=True)
                r.label(text="%s臂" % ("左" if s == "L" else "右"))
                r.prop(pb, '["IKFK"]', slider=True, text="")
                r.label(text="IK" if float(pb.get("IKFK", 1.0)) > 0.5 else "FK")

            r = box.row(align=True)
            o = r.operator("ez.snap_fk_to_ik", text="FK → IK 左")
            o.rig = rig_name
            o.side = "L"
            o = r.operator("ez.snap_fk_to_ik", text="右")
            o.rig = rig_name
            o.side = "R"
            r = box.row(align=True)
            o = r.operator("ez.snap_ik_to_fk", text="IK → FK 左")
            o.rig = rig_name
            o.side = "L"
            o = r.operator("ez.snap_ik_to_fk", text="右")
            o.rig = rig_name
            o.side = "R"
            o = box.operator("ez.snap_fk_to_ik", text="双臂 FK → IK")
            o.rig = rig_name
            o.side = "BOTH"

            col = box.column(align=True)
            col.label(text="前臂扭转传递")
            for s in ("L", "R"):
                ac = cfg["arms"][s]
                fa = _ez_forearm(arm, ac)
                pb_fa = arm.pose.bones.get(fa) if fa else None
                r = col.row(align=True)
                r.label(text="%s臂" % ("左" if s == "L" else "右"))
                if pb_fa is None:
                    r.label(text="无前臂骨", icon="INFO")
                    continue
                tcon = next((x for x in pb_fa.constraints if x.name == TWIST_CNAME), None)
                if tcon is None:
                    r.label(text="未安装", icon="INFO")
                    r.operator("ez.twist_enable", text="", icon="ADD").rig = rig_name
                    continue
                r.prop(tcon, "influence", slider=True, text="")
                r.label(text="%.0f%%" % round(tcon.influence * 100))

            col = box.column(align=True)
            col.label(text="腿部 IK 开关")
            for s in ("L", "R"):
                c = cfg["legs"][s]
                pb = arm.pose.bones.get(c["sw"])
                if pb is None:
                    continue
                r = col.row(align=True)
                r.label(text="%s腿" % ("左" if s == "L" else "右"))
                r.prop(pb, '["IKFK"]', slider=True, text="")
            o = box.operator("ez.snap_fk_to_ik_leg", text="双腿 FK → IK")
            o.rig = rig_name
            o.side = "BOTH"

            col = box.column(align=True)
            o = col.operator("ez.select_empties", text="选中全部控制器", icon="BONE_DATA")
            o.rig = rig_name
            o = col.operator("ez.reset_handles", text="控制器归位", icon="LOOP_BACK")
            o.rig = rig_name
            o = col.operator("ez.toggle_ik_bones", text="显隐 IK 骨骼", icon="BONE_DATA")
            o.rig = rig_name
            o = col.operator("ez.toggle_decoration", text="显隐装饰骨骼", icon="HIDE_ON")
            o.rig = rig_name

            if "IK_Stretch" in arm.keys():
                r = box.row(align=True)
                r.prop(arm, '["IK_Stretch"]', text="IK 拉伸")

            # 姿态槽位
            col = box.column(align=True)
            col.label(text="姿态槽")
            for slot in (1, 2, 3, 4):
                r = col.row(align=True)
                r.label(text="槽 %d" % slot)
                o = r.operator("ez.pose_slot", text="存")
                o.rig = rig_name
                o.slot = slot
                o.action = "SAVE"
                o = r.operator("ez.pose_slot", text="取")
                o.rig = rig_name
                o.slot = slot
                o.action = "LOAD"

        row = layout.row(align=True)
        row.operator("ez.toggle_engine", icon="RENDER_STILL")

