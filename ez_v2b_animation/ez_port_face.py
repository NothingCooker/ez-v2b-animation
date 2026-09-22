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

# ---------------------------------------------------------------- 表情（形态键）
# 两个角色都自带一整套 MMD 标准表情键（Karin idx 170-211 / Chocolate idx 570-618），
# 每个键本身就是做好的组合形变，所以直接一键应用即可，不需要手工拼接。
FACE_GROUPS = [
    ("情绪", True, [
        ("笑い", "笑"), ("にっこり", "微笑"), ("にこり", "浅笑"), ("にやり", "坏笑"),
        ("なごみ", "安详"), ("じと目", "半睁眼"), ("ｷﾘｯ", "锐利"), ("真面目", "认真"),
        ("困る", "为难"), ("はぅ", "害羞"), ("びっくり", "惊讶"), ("怒り", "愤怒"),
        ("悲しい", "悲伤"), ("恐ろしい子！", "惊悚"), ("星目", "星星眼"), ("はぁと", "心形眼"),
    ]),
    ("口型", True, [
        ("あ", "あ a"), ("い", "い i"), ("う", "う u"), ("え", "え e"), ("お", "お o"),
        ("ん", "ん n"), ("VRC.v_aa", "VRC a"), ("VRC.v_ih", "VRC i"),
        ("VRC.v_ou", "VRC u"), ("VRC.v_E", "VRC e"), ("VRC.v_oh", "VRC o"),
        ("VRC.v_nn", "VRC n"), ("vrc.v_aa", "vrc a"), ("vrc.v_ih", "vrc i"),
        ("vrc.v_ou", "vrc u"), ("vrc.v_e", "vrc e"), ("vrc.v_oh", "vrc o"),
        ("vrc.v_nn", "vrc n"),
    ]),
    ("眼睛", False, [
        ("まばたき", "闭眼"), ("ウィンク", "左眨"), ("ウィンク右", "右眨"),
        ("ウィンク２", "左眨2"), ("ｳｨﾝｸ２右", "右眨2"),
        ("瞳小", "瞳小"), ("瞳大", "瞳大"),
        ("ハイライト消し", "高光灭"), ("光下", "光下"), ("上", "看上"), ("下", "看下"),
    ]),
    ("嘴", False, [
        ("口角上げ", "嘴角上"), ("口角下げ", "嘴角下"),
        ("口横広げ", "嘴角宽"), ("口角広げ", "嘴角宽2"),
        ("はんっ！", "撇嘴"), ("ぺろっ", "吐舌"), ("てへぺろ", "吐舌2"),
        ("ω□", "ω□"), ("∧", "∧"), ("Λ", "Λ"), ("▲", "▲"),
        ("ワ", "ワ"), ("ω", "ω"), ("□", "□"),
    ]),
    ("脸颊", False, [
        ("頬染め", "脸红"), ("涙", "泪"), ("はちゅ目", "哈欠眼"),
    ]),
]

# 组合表情：点一下把若干键一起设到目标值（会先清掉情绪组）
FACE_PRESETS = [
    ("害羞", [("頬染め", 1.0), ("はぅ", 0.9)]),
    ("得意", [("にやり", 1.0), ("じと目", 0.6)]),
    ("哭泣", [("涙", 1.0), ("悲しい", 1.0)]),
    ("愤怒", [("怒り", 1.0), ("頬染め", 0.6)]),
    ("困惑", [("困る", 1.0), ("はぅ", 0.6)]),
    ("惊讶", [("びっくり", 1.0), ("星目", 1.0)]),
    ("黑化", [("ハイライト消し", 1.0), ("にやり", 0.8)]),
    ("疲惫", [("じと目", 1.0), ("下", 0.8)]),
]



def _face_meshes(arm):
    out = []
    for o in bpy.data.objects:
        if o.type != "MESH" or o.data.shape_keys is None:
            continue
        if any(m.type == "ARMATURE" and m.object == arm for m in o.modifiers):
            out.append(o)
    return out


def _face_has(arm, key_name):
    for o in _face_meshes(arm):
        if o.data.shape_keys.key_blocks.get(key_name) is not None:
            return True
    return False


def _face_value(arm, key_name):
    """取所有网格上的最大值（同名键通常同步，取最大值更稳妥）"""
    v = 0.0
    for o in _face_meshes(arm):
        kb = o.data.shape_keys.key_blocks.get(key_name)
        if kb is not None and kb.value > v:
            v = kb.value
    return v


def _redraw():
    try:
        for area in bpy.context.screen.areas:
            area.tag_redraw()
    except Exception:
        pass


def _face_set(arm, key_name, value):
    n = 0
    for o in _face_meshes(arm):
        kb = o.data.shape_keys.key_blocks.get(key_name)
        if kb is not None:
            kb.value = value
            n += 1
    return n


def _face_clear(arm):
    n = 0
    for group_name, exclusive, items in FACE_GROUPS:
        for key_name, _label in items:
            n += _face_set(arm, key_name, 0.0)
    return n


def _face_clear_group(arm, group_name):
    for gname, exclusive, items in FACE_GROUPS:
        if gname != group_name:
            continue
        for key_name, _label in items:
            _face_set(arm, key_name, 0.0)


def _face_keyframe(arm, frame):
    """只 K 面板涉及的表情键：当前非零的，以及已经有关键帧的（保证归零动作也被记录）。
    不碰 kisekae_* 之类的换装开关，避免污染动作。"""
    face_keys = set()
    for _gname, _exclusive, items in FACE_GROUPS:
        for k, _label in items:
            face_keys.add(k)
    total = 0
    for o in _face_meshes(arm):
        sk = o.data.shape_keys
        existing = set()
        if sk.animation_data and sk.animation_data.action:
            for fc in sk.animation_data.action.fcurves:
                m = re.match(r'key_blocks\["(.+)"\]\.value', fc.data_path)
                if m:
                    existing.add(m.group(1))
        for kb in sk.key_blocks:
            if kb.name not in face_keys:
                continue
            if kb.value != 0.0 or kb.name in existing:
                kb.keyframe_insert("value", frame=frame)
                total += 1
    return total


class EZ_OT_face_apply(bpy.types.Operator):
    bl_idname = "ez.face_apply"
    bl_label = "表情"
    bl_description = "一键应用表情（情绪类互斥，其他类可叠加；再点一次取消）"
    bl_options = {"REGISTER", "UNDO"}

    rig: bpy.props.EnumProperty(name="角色", items=_rig_items)
    group: bpy.props.StringProperty(default="情绪")
    key: bpy.props.StringProperty(default="")

    def execute(self, context):
        cfg, arm = _rig(self.rig)
        if arm is None or not self.key:
            return {"CANCELLED"}
        is_exclusive = False
        for name, exclusive, its in FACE_GROUPS:
            if name == self.group:
                is_exclusive = exclusive
                break
        was_on = _face_value(arm, self.key) > 0.5
        if is_exclusive:
            _face_clear_group(arm, self.group)
        _face_set(arm, self.key, 0.0 if was_on else 1.0)
        _redraw()
        return {"FINISHED"}


class EZ_OT_face_preset(bpy.types.Operator):
    bl_idname = "ez.face_preset"
    bl_label = "组合表情"
    bl_description = "一键应用组合表情（先清掉情绪组，再叠加上各键的目标值）"
    bl_options = {"REGISTER", "UNDO"}

    rig: bpy.props.EnumProperty(name="角色", items=_rig_items)
    preset: bpy.props.StringProperty(default="")

    def execute(self, context):
        cfg, arm = _rig(self.rig)
        if arm is None:
            return {"CANCELLED"}
        target = None
        for name, pairs in FACE_PRESETS:
            if name == self.preset:
                target = pairs
                break
        if target is None:
            return {"CANCELLED"}
        _face_clear_group(arm, "情绪")
        for key_name, val in target:
            _face_set(arm, key_name, val)
        _redraw()
        return {"FINISHED"}


class EZ_OT_face_clear(bpy.types.Operator):
    bl_idname = "ez.face_clear"
    bl_label = "清空表情"
    bl_description = "把两个角色面板涉及的所有表情键归零"
    bl_options = {"REGISTER", "UNDO"}

    rig: bpy.props.EnumProperty(name="角色", items=_rig_items)

    def execute(self, context):
        cfg, arm = _rig(self.rig)
        if arm is None:
            return {"CANCELLED"}
        n = _face_clear(arm)
        _redraw()
        self.report({"INFO"}, "已归零 %d 个形态键" % n)
        return {"FINISHED"}


class EZ_OT_face_key(bpy.types.Operator):
    bl_idname = "ez.face_key"
    bl_label = "K 到当前帧"
    bl_description = "在当前帧给表情打关键帧（非零键，以及已有动画的键）"
    bl_options = {"REGISTER", "UNDO"}

    rig: bpy.props.EnumProperty(name="角色", items=_rig_items)

    def execute(self, context):
        cfg, arm = _rig(self.rig)
        if arm is None:
            return {"CANCELLED"}
        n = _face_keyframe(arm, context.scene.frame_current)
        _redraw()
        self.report({"INFO"}, "已给 %d 个形态键打帧（帧 %d）" % (n, context.scene.frame_current))
        return {"FINISHED"}


def _capture_pose(cfg, arm):
    data = {"bones": {}, "empties": {}}
    for pb in arm.pose.bones:
        data["bones"][pb.name] = [round(v, 6) for v in
                                  list(pb.location) + list(pb.rotation_quaternion) + list(pb.scale)]
    for _bone_name, ename in cfg["empties"].items():
        e = bpy.data.objects.get(ename)
        if e is not None:
            data["empties"][ename] = [round(v, 6) for v in
                                      list(e.location) + list(e.rotation_quaternion) + list(e.scale)]
    return json.dumps(data)


def _apply_pose(cfg, arm, raw):
    try:
        data = json.loads(raw)
    except Exception:
        return 0
    n = 0
    for name, v in data.get("bones", {}).items():
        pb = arm.pose.bones.get(name)
        if pb is None or len(v) < 10:
            continue
        pb.location = v[0:3]
        pb.rotation_quaternion = v[3:7]
        pb.scale = v[7:10]
        n += 1
    for ename, v in data.get("empties", {}).items():
        e = bpy.data.objects.get(ename)
        if e is None or len(v) < 10:
            continue
        e.rotation_mode = "QUATERNION"
        e.location = v[0:3]
        e.rotation_quaternion = v[3:7]
        e.scale = v[7:10]
    return n


class EZ_OT_pose_slot(bpy.types.Operator):
    bl_idname = "ez.pose_slot"
    bl_label = "姿态槽"
    bl_description = "把当前姿态存进槽位，或从槽位取回（含 IK 控制器位置）"
    bl_options = {"REGISTER", "UNDO"}

    rig: bpy.props.EnumProperty(name="角色", items=_rig_items)
    slot: bpy.props.IntProperty(default=1, min=1, max=4)
    action: bpy.props.EnumProperty(items=[("SAVE", "存", ""), ("LOAD", "取", "")], default="SAVE")

    def execute(self, context):
        cfg, arm = _rig(self.rig)
        if arm is None:
            return {"CANCELLED"}
        prop = "Pose_%d" % self.slot
        if self.action == "SAVE":
            arm[prop] = _capture_pose(cfg, arm)
            self.report({"INFO"}, "已存入槽 %d（%s）" % (self.slot, cfg["label"]))
        else:
            raw = arm.get(prop)
            if not raw:
                self.report({"WARNING"}, "槽 %d 是空的" % self.slot)
                return {"CANCELLED"}
            n = _apply_pose(cfg, arm, raw)
            _upd()
            _redraw()
            self.report({"INFO"}, "已取出槽 %d（%d 根骨骼）" % (self.slot, n))
        return {"FINISHED"}


class EZ_OT_toggle_engine(bpy.types.Operator):
    bl_idname = "ez.toggle_engine"
    bl_label = "切换渲染引擎"
    bl_description = "在 EEVEE（预览快）与 CYCLES（出图质量高）之间切换"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        r = context.scene.render
        try:
            ids = [e.identifier for e in r.bl_rna.properties["engine"].enum_items]
        except Exception:
            ids = []
        eevee = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in ids else "BLENDER_EEVEE"
        r.engine = "CYCLES" if r.engine.startswith("BLENDER_EEVEE") else eevee
        self.report({"INFO"}, "渲染引擎: %s" % r.engine)
        return {"FINISHED"}


class EZ_PT_face(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "EZ V2B"
    bl_label = "表情（形态键）"
    bl_idname = "EZ_PT_face"

    def draw(self, context):
        layout = self.layout
        rigs = _ensure_rigs()
        if not rigs:
            layout.label(text="未发现角色", icon="ERROR")
            return
        for rig_name in sorted(rigs):
            cfg, arm = _rig(rig_name)
            if arm is None:
                continue
            meshes = _face_meshes(arm)
            if not meshes:
                continue
            box = layout.box()
            row = box.row(align=True)
            row.label(text=cfg["label"], icon="ARMATURE_DATA")
            row.operator("ez.select_rig", text="").rig = rig_name

            for group_name, exclusive, items in FACE_GROUPS:
                avail = [(k, lb) for k, lb in items if _face_has(arm, k)]
                if not avail:
                    continue
                row = box.row(align=True)
                row.label(text=group_name + ("（互斥）" if exclusive else ""))
                grid = box.grid_flow(row_major=True, columns=4, even_columns=True, align=True)
                for key_name, label in avail:
                    active = _face_value(arm, key_name) > 0.5
                    # alert 是粘性的：必须每个元素前显式设置，否则会一路继承下去
                    grid.alert = active
                    op = grid.operator("ez.face_apply", text=label, depress=active)
                    op.rig = rig_name
                    op.group = group_name
                    op.key = key_name
                grid.alert = False

            presets = [p for p in FACE_PRESETS
                       if any(_face_has(arm, k) for k, _v in p[1])]
            if presets:
                box.label(text="组合")
                grid = box.grid_flow(row_major=True, columns=4, even_columns=True, align=True)
                for name, _pairs in presets:
                    op = grid.operator("ez.face_preset", text=name)
                    op.rig = rig_name
                    op.preset = name

            row = box.row(align=True)
            o = row.operator("ez.face_clear", text="清空表情", icon="LOOP_BACK")
            o.rig = rig_name
            o = row.operator("ez.face_key", text="K 到当前帧", icon="KEYFRAME")
            o.rig = rig_name


