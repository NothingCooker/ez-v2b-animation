# -*- coding: utf-8 -*-
# ---------------------------------------------------------------------------
# ez-v2b-animation  ——  作者：B站 @高压郭炖大葱
# 使用、修改、二次分发请保留本署名。
# ---------------------------------------------------------------------------

"""ez-v2b-animation :: UI 面板与操作符

核心交互（对应需求"选择一个骨骼，然后一键进行处理"）：

  1. 面板列出场景里所有骨架，每个一行
  2. 点"选择" -> 选中该骨架；点"处理" -> 对该骨架一键建 IK
  3. 处理前显示**只读预检**（结构发现结果），处理后才显示结果与指标
  4. 更新区：检查 / 下载 / 安装，全部不阻断主功能

所有写操作都走操作符（Blender 禁止在 draw() 里改数据）。
"""

import bpy
import json
import os
import sys

from bpy.props import (StringProperty, BoolProperty, FloatProperty,
                       EnumProperty, IntProperty)

# 子模块导入：包内相对导入优先，失败时按"同目录文件"直接加载
# （三种加载方式：插件包 / 单文件 exec / blend 文本块，只有第一种有正常包上下文）
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
B = _load_sibling("ez_builder")
U = _load_sibling("ez_update")
TEX = _load_sibling("ez_texture")


PANEL_CAT_MAIN = "EZ V2B"
PANEL_CAT_UPDATE = "EZ V2B"

# 最近一次处理的报告（存在 Scene 上，随文件走）
REPORT_KEY = "ez_last_report"


def _scene():
    return getattr(bpy.context, "scene", None)


def _armatures():
    return [o for o in bpy.data.objects if o.type == "ARMATURE"]


def _active_arm(context):
    ob = context.object
    if ob is not None and ob.type == "ARMATURE":
        return ob
    for o in context.selected_objects:
        if o.type == "ARMATURE":
            return o
    return None


def _redraw():
    try:
        for w in bpy.context.window_manager.windows:
            for a in w.screen.areas:
                if a.type in ("VIEW_3D", "PROPERTIES"):
                    a.tag_redraw()
    except Exception:
        pass


def _is_done(arm):
    """该骨架是否已经处理过（有本插件的 profile 标记）"""
    raw = arm.get(B.PROFILE_KEY)
    if not raw:
        return False
    try:
        d = json.loads(raw)
    except Exception:
        return False
    return bool((d.get("ez") or {}).get("mark") == B.MARK)


# ================================================================ 操作符：一键处理

class EZ_OT_process(bpy.types.Operator):
    bl_idname = "ez.process"
    bl_label = "一键处理：补齐 IK"
    bl_description = ("对该骨架执行完整的人形 IK 化：\n"
                      "结构发现 -> 补 IK 手柄/极向 -> IK 与链末端旋转 -> "
                      "IKFK 切换器与驱动 -> 前臂扭转传递 -> Root/Hips 控制器 -> "
                      "控制器形状 -> 骨骼集合分组 -> 写 rig_profile -> 数值验证")
    bl_options = {"REGISTER", "UNDO"}

    armature: StringProperty(name="骨架", default="")
    do_twist: BoolProperty(name="前臂扭转传递", default=True)
    do_switches: BoolProperty(name="IKFK 切换器与驱动", default=True)
    do_controls: BoolProperty(name="Root / Hips 控制器", default=True)
    do_shapes: BoolProperty(name="控制器形状", default=True)
    do_groups: BoolProperty(name="骨骼集合分组", default=True)
    pole_scan: BoolProperty(name="扫描极向角度（从零建时）", default=True)
    shape_scale: FloatProperty(name="形状尺寸倍率", default=1.0, min=0.1, max=5.0)
    do_textures: BoolProperty(name="重绑并打包纹理", default=True,
                              description="按文件名递归查找贴图并重新指向路径，"
                                          "然后打包进 .blend（换机器不丢贴图）")

    @classmethod
    def poll(cls, context):
        return _armatures() != []

    def invoke(self, context, event):
        if not self.armature:
            arm = _active_arm(context)
            if arm is not None:
                self.armature = arm.name
        return context.window_manager.invoke_props_dialog(self, width=380)

    def draw(self, context):
        layout = self.layout
        arm = bpy.data.objects.get(self.armature)
        if arm is None:
            layout.label(text="未选择骨架", icon="ERROR")
            return
        layout.label(text="骨架: %s" % arm.name, icon="ARMATURE_DATA")
        box = layout.box()
        box.label(text="要构建的内容", icon="TOOL_SETTINGS")
        box.prop(self, "do_switches")
        box.prop(self, "do_twist")
        box.prop(self, "do_controls")
        box.prop(self, "do_shapes")
        box.prop(self, "do_groups")
        box.prop(self, "do_textures")
        box.prop(self, "pole_scan")
        if self.do_shapes:
            box.prop(self, "shape_scale")
        if _is_done(arm):
            layout.label(text="该骨架已处理过，重复运行是幂等的", icon="INFO")

    def execute(self, context):
        arm = bpy.data.objects.get(self.armature)
        if arm is None or arm.type != "ARMATURE":
            self.report({"ERROR"}, "找不到骨架 %s" % self.armature)
            return {"CANCELLED"}

        opt = {
            "twist": self.do_twist,
            "switches": self.do_switches,
            "controls": self.do_controls,
            "shapes": self.do_shapes,
            "groups": self.do_groups,
            "pole_scan": self.pole_scan,
            "shape_scale": self.shape_scale,
            "textures": self.do_textures,
            "profile": True,
        }
        try:
            rep = B.build(arm, opt)
        except Exception as ex:
            import traceback
            traceback.print_exc()
            self.report({"ERROR"}, "处理失败: %s" % ex)
            return {"CANCELLED"}

        # 报告存到场景，随文件保存
        sc = _scene()
        if sc is not None:
            try:
                sc[REPORT_KEY] = json.dumps(_rep_to_json(rep), ensure_ascii=False)
            except Exception:
                pass

        m = rep["metrics"]
        self.report({"INFO"}, "完成: 静置偏差 %.6f，IK 跟随率 %.4f，新增骨骼 %d" % (
            m.get("rest_dev_max", -1), m.get("follow_min", -1),
            len(rep["created"]["bones"])))
        for w in rep["warnings"][:3]:
            self.report({"WARNING"}, w)
        _redraw()
        return {"FINISHED"}


def _rep_to_json(rep):
    """报告转可序列化结构（去掉不可 JSON 化的部分）"""
    out = {
        "armature": rep.get("armature"),
        "steps": rep.get("steps", []),
        "warnings": rep.get("warnings", []),
        "created": rep.get("created", {}),
        "metrics": {},
        "profile": rep.get("profile"),
    }
    m = rep.get("metrics", {})
    for k, v in m.items():
        if k in ("rest_dev", "follow", "bone_collections", "shape_world_size",
                 "shape_local_size", "local_to_world", "rest_dev_max", "follow_min",
                 "constraints", "missing_limbs", "drivers",
                 "controllers_with_shape"):
            try:
                json.dumps(v)
                out["metrics"][k] = v
            except Exception:
                out["metrics"][k] = str(v)
    return out


# ================================================================ 操作符：辅助

class EZ_OT_select_armature(bpy.types.Operator):
    bl_idname = "ez.select_armature"
    bl_label = "选中该骨架"
    bl_options = {"REGISTER", "UNDO", "INTERNAL"}

    armature: StringProperty(default="")

    def execute(self, context):
        arm = bpy.data.objects.get(self.armature)
        if arm is None:
            return {"CANCELLED"}
        for o in context.view_layer.objects:
            o.select_set(False)
        arm.select_set(True)
        context.view_layer.objects.active = arm
        return {"FINISHED"}


class EZ_OT_precheck(bpy.types.Operator):
    bl_idname = "ez.precheck"
    bl_label = "只读预检"
    bl_description = "只读地跑一遍结构发现，不修改任何数据"
    bl_options = {"REGISTER", "INTERNAL"}

    armature: StringProperty(default="")

    def execute(self, context):
        arm = bpy.data.objects.get(self.armature)
        if arm is None:
            return {"CANCELLED"}
        try:
            info = D.discover(arm)
            met = M.measure(arm, info.get("human"))
        except Exception as ex:
            self.report({"ERROR"}, "预检失败: %s" % ex)
            return {"CANCELLED"}
        sc = _scene()
        if sc is not None:
            sc["ez_precheck"] = D.describe(info) + "\n" + M.describe(arm, info.get("human"))
        self.report({"INFO"}, "预检完成，结果见面板")
        _redraw()
        return {"FINISHED"}


class EZ_OT_rescan(bpy.types.Operator):
    bl_idname = "ez.rescan"
    bl_label = "重新扫描骨架"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        n = len(_armatures())
        self.report({"INFO"}, "场景里有 %d 个骨架" % n)
        _redraw()
        return {"FINISHED"}


class EZ_OT_show_report(bpy.types.Operator):
    bl_idname = "ez.show_report"
    bl_label = "查看最近一次报告"
    bl_description = "把最近一次处理报告写入 Blender 文本编辑器，便于复制"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        sc = _scene()
        raw = sc.get(REPORT_KEY) if sc is not None else None
        if not raw:
            self.report({"WARNING"}, "还没有处理记录")
            return {"CANCELLED"}
        try:
            d = json.loads(raw)
        except Exception:
            self.report({"ERROR"}, "报告损坏")
            return {"CANCELLED"}
        lines = []
        lines.append("ez-v2b-animation 处理报告")
        lines.append("骨架: %s" % d.get("armature"))
        lines.append("")
        lines.append("--- 步骤 ---")
        lines += ["  " + s for s in d.get("steps", [])]
        if d.get("warnings"):
            lines.append("")
            lines.append("--- 警告 ---")
            lines += ["  " + s for s in d["warnings"]]
        lines.append("")
        lines.append("--- 指标 ---")
        for k, v in (d.get("metrics") or {}).items():
            lines.append("  %s = %s" % (k, json.dumps(v, ensure_ascii=False)))
        lines.append("")
        lines.append("--- profile ---")
        lines.append(json.dumps(d.get("profile"), ensure_ascii=False, indent=2))
        txt = "\n".join(lines)

        name = "ez_report.txt"
        t = bpy.data.texts.get(name)
        if t is None:
            t = bpy.data.texts.new(name)
        t.clear()
        t.write(txt)
        self.report({"INFO"}, "报告已写入文本块 %s" % name)
        return {"FINISHED"}


# ================================================================ 操作符：纹理

class EZ_OT_texture_process(bpy.types.Operator):
    bl_idname = "ez.texture_process"
    bl_label = "重绑并打包纹理"
    bl_description = ("按文件名递归查找贴图 -> 重新指向路径 -> 打包进 .blend。\n"
                      "适合 FBX 导入后材质变紫、或换机器丢贴图的情况")
    bl_options = {"REGISTER", "UNDO"}

    armature: StringProperty(name="骨架", default="")
    root: StringProperty(name="纹理搜索目录", subtype="DIR_PATH", default="",
                         description="留空则自动用 .blend 所在目录及其 Textures 子目录")
    do_rebind: BoolProperty(name="重绑路径", default=True)
    do_pack: BoolProperty(name="打包进文件", default=True)
    all_images: BoolProperty(name="处理全部贴图", default=False,
                             description="关闭时只处理该骨架引用的材质用到的贴图，"
                                         "避免误伤场景里其他角色")

    @classmethod
    def poll(cls, context):
        return True

    def invoke(self, context, event):
        if not self.armature:
            arm = _active_arm(context)
            if arm is not None:
                self.armature = arm.name
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "armature")
        layout.prop(self, "root")
        box = layout.box()
        box.prop(self, "do_rebind")
        box.prop(self, "do_pack")
        box.prop(self, "all_images")
        arm = bpy.data.objects.get(self.armature)
        if arm is not None:
            st = TEX.stats(TEX.collect_images(arm, self.all_images))
            box.label(text=TEX.stats_text(st))

    def execute(self, context):
        arm = bpy.data.objects.get(self.armature) if self.armature else None
        roots = [self.root] if self.root else None
        try:
            r = TEX.process(arm, roots, do_rebind=self.do_rebind,
                            do_pack=self.do_pack, all_images=self.all_images)
        except Exception as ex:
            import traceback
            traceback.print_exc()
            self.report({"ERROR"}, "处理失败: %s" % ex)
            return {"CANCELLED"}
        for n in r.get("notes", []):
            self.report({"INFO"}, n)
        if not r.get("ok"):
            self.report({"WARNING"}, "未完成：%s" % "；".join(r.get("notes", ["未知"])))
            return {"CANCELLED"}
        st = r.get("stats") or {}
        self.report({"INFO"}, TEX.stats_text(st))
        _redraw()
        return {"FINISHED"}


class EZ_OT_texture_unpack(bpy.types.Operator):
    bl_idname = "ez.texture_unpack"
    bl_label = "取消打包（写回磁盘）"
    bl_options = {"REGISTER", "UNDO"}

    armature: StringProperty(name="骨架", default="")

    def execute(self, context):
        arm = bpy.data.objects.get(self.armature) if self.armature else None
        imgs = TEX.collect_images(arm, True)
        n = TEX.unpack_all(imgs)
        self.report({"INFO"}, "已取消打包 %d 张" % n)
        return {"FINISHED"}


# ================================================================ 操作符：更新

class EZ_OT_update_check(bpy.types.Operator):
    bl_idname = "ez.update_check"
    bl_label = "检查更新"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        r = U.check(context)
        sc = _scene()
        if sc is not None:
            sc["ez_upd_status"] = ("；".join(r.get("notes") or []))[:900]
            sc["ez_upd_latest"] = U.version_str(r.get("latest") or (0, 0, 0))
            sc["ez_upd_newer"] = bool(r.get("newer"))
        if not r.get("ok"):
            self.report({"WARNING"}, "检查失败: %s" % "；".join(r.get("notes") or ["未知"]))
            return {"CANCELLED"}
        if r.get("newer"):
            self.report({"INFO"}, "发现新版本 %s（当前 %s）" % (
                U.version_str(r["latest"]), U.version_str(r["current"])))
        else:
            self.report({"INFO"}, "已是最新版本 %s" % U.version_str(r["current"]))
        _redraw()
        return {"FINISHED"}


class EZ_OT_update_install(bpy.types.Operator):
    bl_idname = "ez.update_install"
    bl_label = "下载并安装更新"
    bl_description = ("下载更新包并暂存。Windows 上正在运行的 .py 无法就地替换，"
                      "所以安装会在重启 Blender 后生效")
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        r = U.download_and_stage()
        sc = _scene()
        if sc is not None:
            sc["ez_upd_status"] = ("；".join(r.get("notes") or []))[:900]
        if not r.get("ok"):
            self.report({"WARNING"}, "更新未完成: %s" % "；".join(r.get("notes") or ["未知"]))
            return {"CANCELLED"}
        self.report({"INFO"}, "已暂存 %s，重启 Blender 后生效" % U.version_str(r["version"]))
        _redraw()
        return {"FINISHED"}


class EZ_OT_update_apply(bpy.types.Operator):
    bl_idname = "ez.update_apply"
    bl_label = "立即应用已暂存的更新"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        r = U.apply_pending()
        sc = _scene()
        if sc is not None:
            sc["ez_upd_status"] = ("；".join(r.get("notes") or []))[:900]
        if not r.get("ok"):
            self.report({"WARNING"}, "应用失败: %s" % "；".join(r.get("notes") or ["未知"]))
            return {"CANCELLED"}
        self.report({"INFO"}, "已更新到 %s，请重启 Blender" % U.version_str(r["version"]))
        _redraw()
        return {"FINISHED"}


class EZ_OT_update_cancel(bpy.types.Operator):
    bl_idname = "ez.update_cancel"
    bl_label = "取消待安装的更新"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        U.cancel_pending()
        sc = _scene()
        if sc is not None:
            sc["ez_upd_status"] = "已取消待安装的更新"
        self.report({"INFO"}, "已取消")
        _redraw()
        return {"FINISHED"}


# ================================================================ 面板

class EZ_PT_main(bpy.types.Panel):
    bl_label = "ez-v2b-animation"
    bl_idname = "EZ_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = PANEL_CAT_MAIN

    def draw(self, context):
        layout = self.layout

        # ---- 署名 ----
        _sb = layout.row(align=True)
        _sb.alignment = "CENTER"
        _sb.label(text="作者  B站 @高压郭炖大葱", icon="INFO")

        arms = _armatures()
        row = layout.row(align=True)
        row.label(text="场景骨架 %d 个" % len(arms), icon="ARMATURE_DATA")
        row.operator("ez.rescan", text="", icon="FILE_REFRESH")

        if not arms:
            box = layout.box()
            box.label(text="未发现骨架", icon="ERROR")
            box.label(text="请先导入模型或打开文件", icon="INFO")
            return

        for arm in arms:
            box = layout.box()
            head = box.row(align=True)
            done = _is_done(arm)
            head.label(text=arm.name,
                       icon="CHECKMARK" if done else "ARMATURE_DATA")
            o = head.operator("ez.select_armature", text="", icon="RESTRICT_SELECT_OFF")
            o.armature = arm.name
            head.operator("ez.precheck", text="", icon="VIEWZOOM").armature = arm.name

            sub = box.row(align=True)
            op = sub.operator("ez.process", text="一键处理", icon="PLAY")
            op.armature = arm.name

            tex = box.row(align=True)
            top = tex.operator("ez.texture_process", text="重绑并打包纹理",
                               icon="TEXTURE")
            top.armature = arm.name
            tst = TEX.stats(TEX.collect_images(arm, False))
            box.label(text=TEX.stats_text(tst),
                      icon="CHECKMARK" if tst["missing"] == 0 and tst["empty"] == 0
                      else "ERROR")

            if done:
                box.label(text="已具备 IK（重复处理是幂等的）", icon="CHECKMARK")

        # 预检结果
        sc = _scene()
        pc = sc.get("ez_precheck") if sc is not None else None
        if pc:
            box = layout.box()
            box.label(text="预检结果（只读）", icon="INFO")
            col = box.column(align=True)
            for line in str(pc).split("\n")[:24]:
                col.label(text=line[:110])

        # 最近报告
        if sc is not None and sc.get(REPORT_KEY):
            layout.operator("ez.show_report", icon="TEXT")


class EZ_PT_options(bpy.types.Panel):
    bl_label = "处理选项"
    bl_idname = "EZ_PT_options"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = PANEL_CAT_MAIN
    bl_parent_id = "EZ_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        box = layout.box()
        box.label(text="默认值 = 保持原有行为", icon="INFO")
        box.label(text="极向角度只在「从零建 IK」时才扫描")
        box.label(text="已存在的 pole_angle / chain_count 一律保留")


class EZ_PT_update(bpy.types.Panel):
    bl_label = "更新"
    bl_idname = "EZ_PT_update"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = PANEL_CAT_UPDATE
    bl_parent_id = "EZ_PT_main"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        sc = _scene()

        txt, icon = U.status_text()
        row = layout.row(align=True)
        row.label(text=txt, icon=icon)

        col = layout.column(align=True)
        col.prop(sc, "ez_upd_url", text="清单")
        col.prop(sc, "ez_upd_repo", text="仓库")
        col.prop(sc, "ez_upd_channel", text="通道")
        col.prop(sc, "ez_upd_auto", text="启动时自动检查")

        r = layout.row(align=True)
        r.operator("ez.update_check", icon="URL")
        if U.has_pending():
            r.operator("ez.update_install", text="重新下载", icon="IMPORT")
        else:
            r.operator("ez.update_install", icon="IMPORT")

        r2 = layout.row(align=True)
        if U.has_pending():
            r2.operator("ez.update_apply", icon="CHECKMARK")
            r2.operator("ez.update_cancel", icon="X")

        st = sc.get("ez_upd_status") if sc is not None else None
        if st:
            box = layout.box()
            for line in str(st).split("\n")[:8]:
                box.label(text=line[:110])

        layout.label(text="当前 %s  最新 %s" % (
            U.version_str(U.current_version()),
            (sc.get("ez_upd_latest") if sc is not None else "-") or "-"))
        layout.separator()
        _ab = layout.row(align=True)
        _ab.alignment = "CENTER"
        _ab.label(text="B站 @高压郭炖大葱")


# ================================================================ 属性与注册

_PROPS = (
    ("ez_upd_url", StringProperty(
        name="清单 URL",
        description="返回 JSON 的更新清单地址。支持自定义格式或 GitHub releases API",
        default=U.DEFAULT_MANIFEST)),
    ("ez_upd_repo", StringProperty(
        name="GitHub 仓库",
        description="owner/repo 形式。填了就用 GitHub releases/latest 作为更新源",
        default="")),
    ("ez_upd_channel", EnumProperty(
        name="通道",
        items=[("stable", "稳定版", ""), ("beta", "测试版", "")],
        default="stable")),
    ("ez_upd_auto", BoolProperty(
        name="启动时自动检查",
        description="打开 Blender 时在后台检查更新（不阻塞界面）",
        default=False)),
    ("ez_upd_status", StringProperty(default="")),
    ("ez_upd_latest", StringProperty(default="")),
    ("ez_upd_newer", BoolProperty(default=False)),
    ("ez_precheck", StringProperty(default="")),
)

_CLASSES = (
    EZ_OT_process,
    EZ_OT_select_armature,
    EZ_OT_precheck,
    EZ_OT_rescan,
    EZ_OT_show_report,
    EZ_OT_texture_process,
    EZ_OT_texture_unpack,
    EZ_OT_update_check,
    EZ_OT_update_install,
    EZ_OT_update_apply,
    EZ_OT_update_cancel,
    EZ_PT_main,
    EZ_PT_options,
    EZ_PT_update,
)


def register_props():
    for name, prop in _PROPS:
        if not hasattr(bpy.types.Scene, name):
            setattr(bpy.types.Scene, name, prop)


def unregister_props():
    for name, _ in _PROPS:
        if hasattr(bpy.types.Scene, name):
            try:
                delattr(bpy.types.Scene, name)
            except Exception:
                pass


def register():
    register_props()
    for c in _CLASSES:
        try:
            bpy.utils.register_class(c)
        except Exception:
            pass


def unregister():
    for c in reversed(_CLASSES):
        try:
            bpy.utils.unregister_class(c)
        except Exception:
            pass
    unregister_props()
