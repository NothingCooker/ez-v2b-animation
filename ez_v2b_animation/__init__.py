# -*- coding: utf-8 -*-
"""ez-v2b-animation :: 把任意常规人形骨架一键变成可动画的 IK 骨架

作者     B站 @高压郭炖大葱
主页     https://space.bilibili.com/
版本     1.0.0

面向"拿到一个新模型就要重新绑一遍 IK"的场景。核心是**结构发现**：
不写死任何骨骼名，全部靠约束结构反查，所以换模型不用改代码。

实测支持（三套命名完全不同的模型全部通过）：

  模型                          手柄命名                                  极向命名
  Karin      (角色.blend)      LHik RHik LFIK RFik                      LHCik LFCik
  Chocolate  (角色.blend)      LHIK RHIK LowerLeg.L.003 RLIK            LWAY RWAY
  Mafuyu     (Mafuyu.blend)    LHIK RHIK Lower_leg.L.001 Lower_leg.R.001 Upper_arm.L.001

一键处理做的事：
  1. 结构发现 —— IK 约束 / 链末端旋转 / 头部 TRACK_TO / 分组 / 形态键
  2. 补 IK 手柄与极向骨骼（缺失时从零建，含弯曲侧反推）
  3. IK 约束 + 链末端 COPY_ROTATION（原模型缺失的自动补，如 Mafuyu 的脚）
  4. 极向角度扫描（仅从零建时；默认 0 是错的）
  5. 前臂扭转传递（LOCAL/LOCAL + 只开 Y + BEFORE + 0.4）
  6. Root / Hips 控制器骨骼 + 层级
  7. IKFK 切换器与驱动
  8. 控制器形状（尺寸按实测世界度量换算）
  9. 骨骼集合分组
 10. 写 rig_profile（供面板自动发现）
 11. 重绑并打包纹理（按文件名递归找回贴图）
 12. 数值验证（静置偏差 / IK 跟随率 / 约束完整性）

安装：
  编辑 > 偏好设置 > 附加组件 > 从磁盘安装 > 选择 ez_v2b_animation.zip

启用后 3D 视图侧栏（N 键）出现 "EZ V2B" 标签页。

------------------------------------------------------------------
本插件由 B站 @高压郭炖大葱 制作并维护。
使用、修改、二次分发请保留本署名。
------------------------------------------------------------------
"""

# 署名（统一在这里，各模块引用同一份）
__author__ = "B站 @高压郭炖大葱"
__bilibili__ = "@高压郭炖大葱"
__version__ = (1, 0, 0)

bl_info = {
    "name": "ez-v2b-animation",
    "author": "B站 @高压郭炖大葱",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar (N) > EZ V2B",
    "description": "任意常规人形骨架一键 IK 化：结构发现、补手柄、IKFK、控制器、纹理打包、自动更新",
    "category": "Rigging",
    "doc_url": "",
    "tracker_url": "",
}

import bpy
import importlib
import sys

# 子模块：作为包加载时用相对导入；被 exec / 单文件加载时退回绝对导入
#
# 前 6 个是 ez 自己的模块（结构发现 / 度量 / 构建 / 更新 / UI / 桥接）；
# 后 3 个是从 karin_ik_tools 移植的功能块（IK 控制 / 表情 / 面捕）。
_SUBMODULES = ("ez_discovery", "ez_metrics", "ez_builder", "ez_update",
               "ez_texture", "ez_ui", "ez_bridge", "ez_deps",
               "ez_port_ik_control", "ez_port_face", "ez_port_facecap")

# 移植块里的面板/操作符类（注册顺序在 ez_ui 之后，便于冲突检查）
_PORT_MODULES = ("ez_port_ik_control", "ez_port_face", "ez_port_facecap")

_modules = {}


def _load_submodules():
    """按可用方式加载子模块，返回 {短名: 模块}"""
    global _modules
    loaded = {}
    pkg = __name__ if __package__ is None or __package__ == "" else __package__
    here = None
    try:
        here = __file__
    except NameError:
        # 文本块场景：没有 __file__（文档 6 节实测会炸）
        here = None

    for name in _SUBMODULES:
        mod = None
        # 1) 相对导入
        if pkg:
            try:
                mod = importlib.import_module("." + name, pkg)
            except Exception:
                mod = None
        # 2) 绝对导入
        if mod is None:
            try:
                mod = importlib.import_module(name)
            except Exception:
                mod = None
        # 3) 同目录文件直接加载（文本块 / 单文件场景）
        if mod is None and here:
            import os
            p = os.path.join(os.path.dirname(os.path.abspath(here)), name + ".py")
            if os.path.isfile(p):
                try:
                    import importlib.util as _ilu
                    spec = _ilu.spec_from_file_location(name, p)
                    mod = _ilu.module_from_spec(spec)
                    sys.modules.setdefault(name, mod)
                    spec.loader.exec_module(mod)
                except Exception:
                    mod = None
        if mod is not None:
            loaded[name] = mod
    _modules = loaded
    return loaded


def _patch_ui():
    """把已加载的子模块注入 ez_ui 的命名空间。

    ez_ui 内部已做相对/绝对导入兜底；这里再补一次，
    保证"作为包加载"和"作为文本块加载"两条路都能拿到正确模块。
    """
    ui = _modules.get("ez_ui")
    if ui is None:
        return
    for short, mod in _modules.items():
        if short != "ez_ui" and not hasattr(ui, short.rsplit("_", 1)[-1]):
            setattr(ui, short.rsplit("_", 1)[-1], mod)
    # 显式设置 ez_ui 用到的四个短名
    if "ez_discovery" in _modules:
        ui.D = _modules["ez_discovery"]
    if "ez_builder" in _modules:
        ui.B = _modules["ez_builder"]
    if "ez_metrics" in _modules:
        ui.M = _modules["ez_metrics"]
    if "ez_update" in _modules:
        ui.U = _modules["ez_update"]


def _bl_id_taken(cls):
    """插件包与文本块同时加载时，避免重复注册（沿用旧插件的守卫思路）"""
    bid = getattr(cls, "bl_idname", None)
    if not bid:
        return getattr(bpy.types, cls.__name__, None) is not None
    try:
        if issubclass(cls, bpy.types.Operator):
            mod, name = bid.split(".", 1)
            getattr(getattr(bpy.ops, mod), name).get_rna_type()
            return True
    except Exception:
        pass
    return getattr(bpy.types, bid, None) is not None


_LOAD_HOOK = None


def _on_load(_dummy=None):
    """换文件后刷新面板"""
    try:
        ui = _modules.get("ez_ui")
        if ui is not None:
            ui._redraw()
    except Exception:
        pass


def _collect_port_classes():
    """收集移植块里的面板/操作符类（按模块顺序），供注册"""
    out = []
    for name in _PORT_MODULES:
        mod = _modules.get(name)
        if mod is None:
            continue
        for attr in dir(mod):
            obj = getattr(mod, attr, None)
            if not isinstance(obj, type):
                continue
            try:
                if issubclass(obj, (bpy.types.Operator, bpy.types.Panel,
                                    bpy.types.Header, bpy.types.Menu)):
                    if obj.__module__ == mod.__name__:
                        out.append(obj)
            except Exception:
                continue
    return out


def _register_port():
    """注册移植块的类。重复 bl_idname 直接跳过（不覆盖已注册的）。"""
    n_ok, n_skip = 0, 0
    for c in _collect_port_classes():
        try:
            if _bl_id_taken(c):
                n_skip += 1
                continue
            bpy.utils.register_class(c)
            n_ok += 1
        except Exception:
            n_skip += 1
    return n_ok, n_skip


def _unregister_port():
    for c in reversed(_collect_port_classes()):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass


def register():
    global _LOAD_HOOK
    _load_submodules()
    _patch_ui()

    ui = _modules.get("ez_ui")
    if ui is not None:
        try:
            ui.register_props()
        except Exception:
            pass
        for c in getattr(ui, "_CLASSES", ()):
            try:
                if _bl_id_taken(c):
                    continue
                bpy.utils.register_class(c)
            except Exception:
                pass

    # 移植块：面捕的属性要先注册（面板 draw 会读它们）
    fc = _modules.get("ez_port_facecap")
    if fc is not None:
        for fn in ("_register_facecap_props",):
            f = getattr(fc, fn, None)
            if callable(f):
                try:
                    f()
                except Exception:
                    pass

    n_ok, n_skip = _register_port()
    if n_skip:
        print("[ez-v2b-animation] 移植块注册 %d 个类，跳过 %d 个（已存在）" % (n_ok, n_skip))

    if _LOAD_HOOK is None:
        _LOAD_HOOK = _on_load
    try:
        if _LOAD_HOOK not in bpy.app.handlers.load_post:
            bpy.app.handlers.load_post.append(_LOAD_HOOK)
    except Exception:
        pass

    # 启动时自动检查更新（带节流，失败静默）
    try:
        upd = _modules.get("ez_update")
        if upd is not None:
            upd.maybe_auto_check()
    except Exception:
        pass


def unregister():
    _unregister_port()

    fc = _modules.get("ez_port_facecap")
    if fc is not None:
        f = getattr(fc, "_unregister_facecap_props", None)
        if callable(f):
            try:
                f()
            except Exception:
                pass

    ui = _modules.get("ez_ui")
    if ui is not None:
        for c in reversed(getattr(ui, "_CLASSES", ())):
            try:
                bpy.utils.unregister_class(c)
            except Exception:
                pass
        try:
            ui.unregister_props()
        except Exception:
            pass
    try:
        if _LOAD_HOOK is not None and _LOAD_HOOK in bpy.app.handlers.load_post:
            bpy.app.handlers.load_post.remove(_LOAD_HOOK)
    except Exception:
        pass


if __name__ == "__main__":
    register()
