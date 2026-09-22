# -*- coding: utf-8 -*-
"""打包 ez-v2b-animation 为可分发 zip，并做结构自检

用法:
    python _autorig_work/pack_ez.py
    # 或（需要 Blender 环境时）
    blender -b --factory-startup -P _autorig_work/pack_ez.py
"""
import os
import sys
import re
import json
import zipfile
import hashlib
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ASS = os.path.dirname(HERE)
ADDON = os.path.join(ASS, "ez_v2b_animation")
ZIP = os.path.join(ASS, "ez_v2b_animation.zip")
SINGLE = os.path.join(ASS, "ez_v2b_animation_single.py")

ADDON_ID = "ez_v2b_animation"
FILES = ("__init__.py", "ez_discovery.py", "ez_metrics.py",
         "ez_builder.py", "ez_texture.py", "ez_update.py", "ez_ui.py",
         "ez_bridge.py", "ez_deps.py",
         "ez_port_ik_control.py", "ez_port_face.py", "ez_port_facecap.py")

log = []
def P(s=""):
    log.append(str(s))

fails = []


def chk(cond, msg):
    if not cond:
        fails.append(msg)
        P("  !! %s" % msg)
    else:
        P("  OK %s" % msg)


# ---------------------------------------------------------------- 1. 清理缓存
P("=" * 74)
P("打包 %s" % ADDON_ID)
P("=" * 74)

pyc = os.path.join(ADDON, "__pycache__")
if os.path.isdir(pyc):
    shutil.rmtree(pyc, ignore_errors=True)
    P("已清理 __pycache__")

# ---------------------------------------------------------------- 2. 语法与结构检查
P("")
P("--- 源文件检查 ---")
bodies = {}
for fn in FILES:
    p = os.path.join(ADDON, fn)
    chk(os.path.isfile(p), "%s 存在" % fn)
    if not os.path.isfile(p):
        continue
    raw = open(p, "rb").read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
        open(p, "wb").write(raw)
        P("  已剥离 BOM: %s" % fn)
    src = raw.decode("utf-8")
    bodies[fn] = src
    try:
        compile(src, fn, "exec")
        P("  语法 OK  %-18s %6d 字节" % (fn, len(raw)))
    except SyntaxError as e:
        chk(False, "%s 语法错误: line %s %s" % (fn, e.lineno, e.msg))

# ---------------------------------------------------------------- 3. 关键结构自检
P("")
P("--- 关键结构自检 ---")
checks = [
    ("bl_info 完整", '"name": "ez-v2b-animation"' in bodies.get("__init__.py", "")),
    ("版本号存在", re.search(r'"version":\s*\(', bodies.get("__init__.py", "")) is not None),
    ("子模块加载器", "_load_submodules" in bodies.get("__init__.py", "")),
    ("结构发现入口", "def discover(" in bodies.get("ez_discovery.py", "")),
    ("IK 约束反查", 'if c.type != "IK"' in bodies.get("ez_discovery.py", "")),
    ("TRACK_TO 反查", 'c.type == "TRACK_TO"' in bodies.get("ez_discovery.py", "")),
    ("形态键发现", "def discover_face(" in bodies.get("ez_discovery.py", "")),
    ("世界度量", "def measure(" in bodies.get("ez_metrics.py", "")),
    ("本地->世界换算", "def local_to_world(" in bodies.get("ez_metrics.py", "")),
    ("一键构建入口", "def build(" in bodies.get("ez_builder.py", "")),
    ("极向扫描", "def scan_pole_angle(" in bodies.get("ez_builder.py", "")),
    ("弯曲侧反推", "def bend_axis(" in bodies.get("ez_builder.py", "")),
    ("扭转传递参数", 'con.mix_mode = "BEFORE"' in bodies.get("ez_builder.py", "")),
    ("驱动绑定", "def _ensure_switches(" in bodies.get("ez_builder.py", "")),
    ("幂等清理", "PROFILE_KEY" in bodies.get("ez_builder.py", "")),
    ("更新检查", "def check(" in bodies.get("ez_update.py", "")),
    ("更新暂存", "def download_and_stage(" in bodies.get("ez_update.py", "")),
    ("更新应用", "def apply_pending(" in bodies.get("ez_update.py", "")),
    ("面板主入口", "class EZ_PT_main" in bodies.get("ez_ui.py", "")),
    ("一键处理操作符", 'bl_idname = "ez.process"' in bodies.get("ez_ui.py", "")),
    # --- 移植块（原 karin_ik_tools 的功能）---
    ("移植·桥接层", "def _load_rigs(" in bodies.get("ez_bridge.py", "")),
    ("移植·Snap 切换", "snap_fk_to_ik" in bodies.get("ez_port_ik_control.py", "")),
    ("移植·FK 真值烘焙", "p_ensure_fk_truth" in bodies.get("ez_port_ik_control.py", "")),
    ("移植·控制器定位", "p_set_ctrl_to_world" in bodies.get("ez_port_ik_control.py", "")),
    ("移植·扭转传递", "twist_enable" in bodies.get("ez_port_ik_control.py", "")),
    ("移植·装饰显隐", "toggle_decoration" in bodies.get("ez_port_ik_control.py", "")),
    ("移植·表情面板", "class EZ_PT_face" in bodies.get("ez_port_face.py", "")),
    ("移植·姿势槽", "_capture_pose" in bodies.get("ez_port_face.py", "")),
    ("移植·面捕引擎", "_facecap_discover_head" in bodies.get("ez_port_facecap.py", "")),
    ("移植·头部标定", "_facecap_calib_head" in bodies.get("ez_port_facecap.py", "")),
    ("移植·面捕烘焙", "_facecap_bake" in bodies.get("ez_port_facecap.py", "")),
    ("移植·两阶段校准", "_fc_calib_begin" in bodies.get("ez_port_facecap.py", "")),
    ("移植·One Euro 滤波", "class _OneEuro" in bodies.get("ez_port_facecap.py", "")),
    # --- 分发版依赖（面捕能否跑起来的关键）---
    ("依赖·零绝对路径定位", "def _plugin_dir(" in bodies.get("ez_deps.py", "")),
    ("依赖·多位置兜底", "def candidates(" in bodies.get("ez_deps.py", "")),
    ("依赖·状态检查", "def status(" in bodies.get("ez_deps.py", "")),
    ("依赖·部署", "def deploy_from_dir(" in bodies.get("ez_deps.py", "")),
    ("依赖·重定向到 ez_deps", "_EZD.status()" in bodies.get("ez_port_facecap.py", "")),
    ("依赖·安装操作符", "ez.facecap_deps_install" in bodies.get("ez_port_facecap.py", "")),
    ("依赖·面板状态区", "面捕依赖" in bodies.get("ez_port_facecap.py", "")),
    # --- 纹理（FBX 导入后材质变紫 / 换机器丢贴图）---
    ("纹理·扫描索引", "def build_index(" in bodies.get("ez_texture.py", "")),
    ("纹理·重绑", "def rebind(" in bodies.get("ez_texture.py", "")),
    ("纹理·打包", "def pack(" in bodies.get("ez_texture.py", "")),
    ("纹理·按骨架过滤", "def collect_images(" in bodies.get("ez_texture.py", "")),
    ("纹理·一站式", "def process(" in bodies.get("ez_texture.py", "")),
    ("纹理·接入一键流程", "TEX.process(" in bodies.get("ez_builder.py", "")),
    ("纹理·UI 开关", "do_textures" in bodies.get("ez_ui.py", "")),
    ("纹理·独立操作符", "ez.texture_process" in bodies.get("ez_ui.py", "")),
    # --- 从零建 IK（纯 FK 骨架）---
    ("从零建·手柄规划", "def _plan_limb_from_scratch(" in bodies.get("ez_builder.py", "")),
    ("从零建·头部手柄", "def _create_head_handle(" in bodies.get("ez_builder.py", "")),
    ("从零建·头部追踪", "def _constrain_head(" in bodies.get("ez_builder.py", "")),
    ("从零建·轴对齐弯曲", "抗浅弯曲噪声" in bodies.get("ez_builder.py", "")),
    # --- 摄像头打开（用户报"面捕无响应，卡在启动摄像头"）---
    ("摄像头·超时常量", "FC_OPEN_TIMEOUT = 25.0" in bodies.get("ez_port_facecap.py", "")),
    ("摄像头·实际读帧判定", "必须实际读到一帧才算可用" in bodies.get("ez_port_facecap.py", "")),
    ("摄像头·逐 tick 推进", '_FC["open_try"]' in bodies.get("ez_port_facecap.py", "")),
    ("摄像头·面板进度", "正在打开摄像头" in bodies.get("ez_port_facecap.py", "")),
    ("摄像头·取消入口", "取消打开" in bodies.get("ez_port_facecap.py", "")),
    ("摄像头·立即定案", "一旦找到可用后端就立即定案" in bodies.get("ez_port_facecap.py", "")),
    ("摄像头·断流重连", "FC_RECONNECT_MAX = 3" in bodies.get("ez_port_facecap.py", "")),
    # --- 通道解析（VRM 1.0 命名）---
    ("通道·归一化匹配", "def _norm_key(" in bodies.get("ez_discovery.py", "")),
    ("通道·VRM1.0 命名", "vrc.Looking Up" in bodies.get("ez_discovery.py", "")),
    ("通道·去重", "已被别的通道占用就跳过" in bodies.get("ez_discovery.py", "")),
    # --- 前臂定位（chain_count=2 时 chain[2] 会越界）---
    ("前臂·不用下标", "def _forearm_of(" in bodies.get("ez_builder.py", "")),
    ("前臂·沿父级查找", "沿父级向上找带 IK 约束的骨骼" in bodies.get("ez_builder.py", "")),
    ("前臂·chain[2] 已移除", "chain[2] if len(chain) > 2 else None" not in bodies.get("ez_builder.py", "")),
    ("手柄·对齐前臂骨长轴", "骨长轴要对齐前臂的骨长轴" in bodies.get("ez_builder.py", "")),
    # --- 面板前臂定位（用户报"依旧显示无前臂骨"）---
    ("前臂·面板辅助函数", "def _ez_forearm(" in bodies.get("ez_port_ik_control.py", "")),
    ("前臂·常量随切片进来", 'TWIST_CNAME = "扭转传递"' in bodies.get("ez_port_ik_control.py", "")),
    ("profile·ik_bone 字段", '"ik_bone": e["ik"]' in bodies.get("ez_builder.py", "")),
    ("profile·handle 字段", '"handle": e["handle"]' in bodies.get("ez_builder.py", "")),
]
for name, ok in checks:
    chk(ok, name)

# ---------------------------------------------------------------- 4. 反向自检：不该残留的硬编码
P("")
P("--- 反向自检（不得残留角色专属硬编码）---")
# 必须**剥离注释与文档字符串**再查：注释里的实测记录（如"Chocolate 的手柄叫
# LowerLeg.L.003"）是设计依据，不是硬编码。只有可执行代码里的名字才算违规。
import io
import tokenize


def strip_comments_and_strings(src):
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    except Exception:
        return src
    return " ".join(out)


code_only = "\n".join(strip_comments_and_strings(v) for v in bodies.values())
forbidden = {
    "Karin 手柄名": r"\bLHik\b",
    "Chocolate 手柄名": r"\bLowerLeg\.L\.003\b",
    "Mafuyu 手柄名": r"Lower_leg\.L\.001",
    "Karin 极向名": r"\bLHCik\b",
    "Chocolate 极向名": r"\bLWAY\b",
    "旧插件 ID": r"karin_ik_tools",
    "旧操作符前缀": r'"karin\.',
    "内置角色兜底表": r"BUILTIN_RIGS",
}
for label, pat in forbidden.items():
    hits = re.findall(pat, code_only)
    chk(not hits, "%s 无残留（代码层命中 %d）" % (label, len(hits)))

# 正向确认：注释里应当**保留**实测记录（这是设计依据，不能被误删）
doc_all = "\n".join(bodies.values())
chk("LowerLeg.L.003" in doc_all, "实测记录（Chocolate 手柄名）保留在注释中")
chk("Lower_leg.L.001" in doc_all, "实测记录（Mafuyu 手柄名）保留在注释中")

# 面板前臂定位：代码层必须用 _ez_forearm（不能再用 chain[2]）
_panel_code = strip_comments_and_strings(bodies.get("ez_port_ik_control.py", ""))
chk("_ez_forearm" in _panel_code, "面板代码层已改用 _ez_forearm")
chk("chain[2]" not in _panel_code, "面板代码层无 chain[2] 残留")

# ---------------------------------------------------------------- 4c. 署名红线
P("")
P("--- 署名检查（每个模块都必须带署名，否则拒绝出包）---")
AUTHOR = "B站 @高压郭炖大葱"
_missing_sign = []
for fn in FILES:
    src = bodies.get(fn, "")
    if AUTHOR not in src:
        _missing_sign.append(fn)
chk(not _missing_sign, "全部 %d 个模块含署名「%s」（缺: %s）" % (
    len(FILES), AUTHOR, _missing_sign or "无"))
chk('"author": "%s"' % AUTHOR in bodies.get("__init__.py", ""),
    "bl_info 的 author 字段已署名")
chk("__author__" in bodies.get("__init__.py", ""),
    "模块级 __author__ 已定义")

# ---------------------------------------------------------------- 4b. 分发安全红线
P("")
P("--- 分发安全红线（分发包绝不能带的东西）---")
# 这条是踩过坑才加的：原插件的 merge_facecap.py 会往代码里注入一行
#     FACECAP_HOME = r"D:\...\karin_ik_tools"
# 那是给 blend 内嵌文本块用的（exec 时没有 __file__）。但它进了分发包后，
# 用户机器上该路径不存在 -> 依赖定位退化 -> **摄像头起不来**。
# 所以：分发包里出现任何开发机绝对路径，一律视为致命缺陷。
dev_path_hits = []
for fn, src in bodies.items():
    for i, line in enumerate(src.split("\n"), 1):
        if "AA-工程文件" in line or "nsfwpro" in line:
            dev_path_hits.append("%s:%d" % (fn, i))
        if re.search(r'FACECAP_HOME\s*=\s*r?["\']', line):
            dev_path_hits.append("%s:%d FACECAP_HOME 注入" % (fn, i))
chk(not dev_path_hits, "零开发机绝对路径（命中 %s）" % (
    dev_path_hits[:4] if dev_path_hits else "0"))

# 旧插件目录名不得出现在代码层（注释里说明来源可以）
_old_hits = []
for fn, src in bodies.items():
    code = strip_comments_and_strings(src)
    if "karin_ik_tools" in code or "karin_ikfk_tools" in code:
        _old_hits.append(fn)
chk(not _old_hits, "代码层无旧插件目录引用（命中 %s）" % (_old_hits or "0"))

# ---------------------------------------------------------------- 5. 打 zip
P("")
P("--- 打包 ---")
if os.path.exists(ZIP):
    os.remove(ZIP)
with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
    for fn in FILES:
        z.write(os.path.join(ADDON, fn), "%s/%s" % (ADDON_ID, fn))
P("已写 %s  %d 字节" % (ZIP, os.path.getsize(ZIP)))

with zipfile.ZipFile(ZIP) as z:
    bad = z.testzip()
    chk(bad is None, "zip 完整性 OK")
    names = z.namelist()
    P("  条目数 %d（应为 %d）" % (len(names), len(FILES)))
    chk(len(names) == len(FILES), "条目数正确（无 __pycache__ 混入）")
    for n in names:
        P("    %-40s %7d 字节" % (n, z.getinfo(n).file_size))
    chk(not any("__pycache__" in n or n.endswith(".pyc") for n in names),
        "zip 内无缓存文件")

# ---------------------------------------------------------------- 6. 单文件版（便于文本块/快速分发）
P("")
P("--- 单文件合并版 ---")
# 按依赖顺序拼接，去掉子模块之间的 import 语句，改用命名空间注入
ORDER = ("ez_discovery.py", "ez_metrics.py", "ez_builder.py", "ez_update.py", "ez_ui.py")
parts = []
for fn in ORDER:
    src = bodies.get(fn, "")
    # 去掉子模块互引（单文件版里已经平铺）
    src = re.sub(r"^D = _load_sibling\(.*?\)\s*$", "", src, flags=re.M)
    src = re.sub(r"^M = _load_sibling\(.*?\)\s*$", "", src, flags=re.M)
    src = re.sub(r"^B = _load_sibling\(.*?\)\s*$", "", src, flags=re.M)
    src = re.sub(r"^U = _load_sibling\(.*?\)\s*$", "", src, flags=re.M)
    parts.append("# " + "=" * 70 + "\n# 来自 %s\n# %s\n" % (fn, "=" * 70) + src)

single = (
    "# -*- coding: utf-8 -*-\n"
    "# ez-v2b-animation 单文件合并版（由 pack_ez.py 自动生成，勿手改）\n"
    "# 用法：Blender 文本编辑器 -> 打开 -> 勾选 Register；或 Run Script\n"
    "# 需要同目录下的子模块时请用插件包版本（ez_v2b_animation.zip）\n\n"
    "import bpy\nimport bmesh\nimport math\nimport json\nimport os\nimport re\n"
    "import sys\nimport time\nimport shutil\nimport zipfile\nimport tempfile\n"
    "import threading\nfrom mathutils import Vector, Matrix\n\n"
) + "\n\n".join(parts)

open(SINGLE, "w", encoding="utf-8", newline="\n").write(single)
P("已写 %s  %d 字节" % (SINGLE, os.path.getsize(SINGLE)))
try:
    compile(single, "single", "exec")
    chk(True, "单文件版语法 OK")
except SyntaxError as e:
    chk(False, "单文件版语法错误 line %s: %s" % (e.lineno, e.msg))

# ---------------------------------------------------------------- 7. 清单模板
P("")
P("--- 更新清单模板 ---")
ver = (0, 0, 0)
m = re.search(r'"version":\s*\(([^)]*)\)', bodies.get("__init__.py", ""))
if m:
    ver = tuple(int(x.strip()) for x in m.group(1).split(",") if x.strip())
sha = hashlib.sha256(open(ZIP, "rb").read()).hexdigest()
manifest = {
    "version": ".".join(str(x) for x in ver),
    "url": "https://example.com/ez_v2b_animation.zip",
    "notes": "更新说明",
    "channels": {
        "stable": {"version": ".".join(str(x) for x in ver),
                   "url": "https://example.com/ez_v2b_animation.zip"},
        "beta": {"version": ".".join(str(x) for x in ver),
                 "url": "https://example.com/ez_v2b_animation_beta.zip"},
    },
    "sha256": sha,
}
mp = os.path.join(ASS, "update.json")
open(mp, "w", encoding="utf-8", newline="\n").write(
    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
P("已写清单模板 %s" % mp)
P("  zip sha256 = %s" % sha)
P("  版本 = %s" % manifest["version"])

# ---------------------------------------------------------------- 8. 汇总
P("")
P("=" * 74)
if fails:
    P("自检失败 %d 项:" % len(fails))
    for f in fails:
        P("  - %s" % f)
    P("=" * 74)
else:
    P("全部通过")
    P("")
    P("产物:")
    P("  %s  (%d 字节)   <- 分发给用户，从磁盘安装" % (
        os.path.basename(ZIP), os.path.getsize(ZIP)))
    P("  %s  (%d 字节)   <- 单文件版" % (
        os.path.basename(SINGLE), os.path.getsize(SINGLE)))
    P("  %s           <- 更新清单模板（改 URL 后上传）" % os.path.basename(mp))
    P("=" * 74)

# 先落盘再打印再退出：否则 sys.exit 会丢掉缓冲输出（踩过）
text = "\n".join(log)
out = os.path.join(HERE, "pack_ez.txt")
try:
    open(out, "w", encoding="utf-8").write(text)
except Exception as ex:
    print("写日志失败: %s" % ex)
print(text)
sys.stdout.flush()

if fails:
    sys.exit(1)
