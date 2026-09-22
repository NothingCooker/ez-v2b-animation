# -*- coding: utf-8 -*-
# ---------------------------------------------------------------------------
# ez-v2b-animation  ——  作者：B站 @高压郭炖大葱
# 使用、修改、二次分发请保留本署名。
# ---------------------------------------------------------------------------

"""ez-v2b-animation :: 兼容桥接层

移植自 karin_ik_tools 的功能块（ez_port_ik_control / ez_port_face / ez_port_facecap）
引用了原插件的若干共享工具。ez 的对应实现在别的模块里，名字不同。
本模块把它们**统一成原插件期望的名字**，供移植块直接使用。

为什么需要这一层：
  移植块有 51 个类和 87 个函数，逐处改写引用风险极高（且下次重新移植会丢）。
  统一在这里做别名，移植块可以保持原样，只需 `from ez_bridge import *`。

原插件期望的名字 -> ez 的实际实现：
  RIGS / _load_rigs / _ensure_rigs / _rig / _rig_items / _rig_items_all
      -> ez_discovery 的发现结果（但原插件是"骨架自带 profile"，ez 也是，语义一致）
  _upd                 -> ez_builder.upd
  _face_meshes         -> ez_discovery.face_meshes
  _face_has/_face_value/_face_set/_face_clear/... -> ez 侧重新实现（原插件里就有）
  SIDE_ITEMS           -> 常量
"""

import bpy
import json
import os
import sys


def _load_sibling(name):
    """加载同目录子模块（兼容包 / 单文件 / 文本块三种加载方式）"""
    try:
        return __import__(name, globals(), locals(), ["*"], 1)
    except Exception:
        pass
    try:
        return __import__(name)
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
B = _load_sibling("ez_builder")

PROFILE_KEY = "rig_profile"

# ================================================================ 角色表

# 运行时角色表：{label: cfg}。由 _load_rigs() 从场景里的 rig_profile 填充。
RIGS = {}
_LAST_SIG = None

SIDE_ITEMS = [("L", "左", ""), ("R", "右", ""), ("BOTH", "双侧", "")]


def _armatures():
    return [o for o in bpy.data.objects if o.type == "ARMATURE"]


def _load_rigs():
    """扫描场景里的骨架，读 rig_profile 重建角色表。

    与原插件语义一致（角色配置跟着角色走）。**没有 BUILTIN_RIGS 兜底表**——
    开发期那张表里全是具体角色的硬编码，正式版只认骨架上的 profile。
    """
    global RIGS
    found, notes = {}, []
    for arm in _armatures():
        raw = arm.get(PROFILE_KEY)
        if not raw:
            continue
        try:
            cfg = json.loads(raw)
        except Exception as ex:
            notes.append("%s 的 profile 解析失败: %s" % (arm.name, ex))
            continue
        cfg["object"] = arm.name
        label = cfg.get("label") or arm.name
        key = label
        n = 2
        while key in found:
            key = "%s_%d" % (label, n)
            n += 1
        cfg["label"] = key
        cfg["_source"] = "profile"
        found[key] = cfg
    RIGS = found
    return RIGS, notes


def _ensure_rigs():
    """轻量检测：只有骨架集合变了才重建，避免每帧重扫"""
    global _LAST_SIG
    sig = tuple(sorted(o.name for o in _armatures()))
    if sig != _LAST_SIG:
        _LAST_SIG = sig
        _load_rigs()
    return RIGS


def _rig_items(self, context):
    rigs = _ensure_rigs()
    if not rigs:
        return [("NONE", "（未发现角色）", "请先用「一键处理」建立 IK 并写入 profile")]
    return [(k, rigs[k]["label"], rigs[k].get("_source", "")) for k in sorted(rigs)]


def _rig_items_all(self, context):
    return list(_rig_items(self, context)) + [("NONE", "全部隐藏", "")]


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
    """该角色是否已经装好 IKFK 切换器"""
    if arm is None:
        return False
    for side in ("L", "R"):
        e = (cfg.get("arms") or {}).get(side)
        if not e:
            return False
        if arm.pose.bones.get(e.get("sw", "")) is None:
            return False
    return True


def _redraw():
    try:
        for w in bpy.context.window_manager.windows:
            for a in w.screen.areas:
                if a.type in ("VIEW_3D", "PROPERTIES"):
                    a.tag_redraw()
    except Exception:
        pass


# ================================================================ 求值

def _upd():
    """刷求值（移植块大量使用这个名字）"""
    return B.upd(3)


# ================================================================ 表情（移植块用）

def _face_meshes(arm):
    return D.face_meshes(arm)


def _face_find_key(arm, key_name):
    """在角色的面部网格里找形态键对象"""
    for o in _face_meshes(arm):
        sk = o.data.shape_keys
        if sk is None:
            continue
        kb = sk.key_blocks.get(key_name)
        if kb is not None:
            return kb
    return None


def _face_has(arm, key_name):
    return _face_find_key(arm, key_name) is not None


def _face_value(arm, key_name):
    kb = _face_find_key(arm, key_name)
    return float(kb.value) if kb is not None else 0.0


def _face_set(arm, key_name, value):
    kb = _face_find_key(arm, key_name)
    if kb is None:
        return False
    kb.value = max(0.0, min(1.0, float(value)))
    return True


def _face_clear(arm):
    for o in _face_meshes(arm):
        sk = o.data.shape_keys
        if sk is None:
            continue
        for kb in sk.key_blocks:
            if kb.name != sk.key_blocks[0].name:
                kb.value = 0.0


# ================================================================ 导出

__all__ = [
    "D", "M", "B",
    "RIGS", "SIDE_ITEMS", "PROFILE_KEY",
    "_armatures", "_load_rigs", "_ensure_rigs", "_rig_items", "_rig_items_all",
    "_rig", "_has_switch", "_redraw", "_upd",
    "_face_meshes", "_face_has", "_face_value", "_face_set", "_face_clear",
    "_face_find_key",
]
