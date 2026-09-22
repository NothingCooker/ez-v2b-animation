# -*- coding: utf-8 -*-
# ---------------------------------------------------------------------------
# ez-v2b-animation  ——  作者：B站 @高压郭炖大葱
# 使用、修改、二次分发请保留本署名。
# ---------------------------------------------------------------------------

"""ez-v2b-animation :: 面捕依赖部署与定位

【为什么需要这个模块】

面捕依赖（mediapipe + opencv + 模型）约 273 MB / 2526 文件，**绝不能进主 zip**
（主包 94 KB，塞进去会变成上百 MB，每次更新都要重下）。

所以依赖是"分开发布、单独部署"的。但原插件的依赖定位逻辑是给开发环境写的：
它把 `FACECAP_HOME`（一个开发机绝对路径）放在查找顺序第一位，
并且在候选链里硬编码了旧插件目录名 `karin_ik_tools`。
分发给用户后这两条都失效 —— **摄像头因此起不来**。

本模块提供分发版专用的依赖定位，原则：

  1. **零绝对路径**：只用"相对本插件目录"和"Blender 用户目录"推导
  2. **多位置兜底**：插件目录 / 用户 addons / 用户资源目录 / 工程目录
  3. **可手动指定**：面板上留一个路径输入框，找不到时用户自己指
  4. **可自动部署**：内置"安装依赖"操作符，从 zip / 本地目录 / 网络装进来

依赖目录命名统一为 `_blpy_nodeps`，可放在以下任一位置（按优先级）：

    <插件目录>/_blpy_nodeps                        <- 推荐（跟插件走）
    <用户 addons>/ez_v2b_animation/_blpy_nodeps    <- 从磁盘安装后的实际位置
    <用户资源>/ez_v2b_facecap_deps                 <- 独立部署（多个 Blender 版本共享）
    <场景里手动指定的路径>                          <- 兜底
"""

import bpy
import os
import sys
import shutil
import zipfile
import tempfile

DEPS_DIRNAME = "_blpy_nodeps"
MODEL_NAME = "face_landmarker.task"
SCENE_DEPS_KEY = "fc_deps"
SCENE_MODEL_KEY = "fc_model"

# 依赖就绪所需的包（用于校验一个目录是否真的是依赖目录）
REQUIRED = ("cv2", "mediapipe")
# 允许的模型大小下限（防止只放了个空文件）
MODEL_MIN_BYTES = 1_000_000


# ================================================================ 目录定位

def _plugin_dir():
    """本插件所在目录（零绝对路径）。

    Blender 加载插件包时 __file__ 一定存在；被 exec 成文本块时可能没有，
    那时退回用户 addons 目录下的插件名。
    """
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        pass
    try:
        return os.path.join(bpy.utils.user_resource("SCRIPTS", path="addons"),
                            "ez_v2b_animation")
    except Exception:
        return ""


def _user_dirs():
    """Blender 的用户目录候选（不含任何开发机路径）"""
    out = []
    try:
        addons = bpy.utils.user_resource("SCRIPTS", path="addons")
        if addons:
            out.append(addons)
            out.append(os.path.join(addons, "ez_v2b_animation"))
    except Exception:
        pass
    try:
        res = bpy.utils.user_resource("SCRIPTS")
        if res:
            out.append(res)
    except Exception:
        pass
    try:
        cfg = bpy.utils.user_resource("CONFIG")
        if cfg:
            out.append(cfg)
    except Exception:
        pass
    return out


def candidates():
    """依赖目录候选，按优先级排列（**全部相对推导，无绝对路径**）"""
    cands = []

    def add(p):
        if p and p not in cands:
            cands.append(p)

    # 1) 面板里手动指定（用户明确指定时最高优先级）
    try:
        s = getattr(bpy.context.scene, SCENE_DEPS_KEY, "")
        if s:
            add(s)
    except Exception:
        pass

    # 2) 插件目录下（推荐位置）
    pd = _plugin_dir()
    if pd:
        add(os.path.join(pd, DEPS_DIRNAME))
        # 3) 插件目录的兄弟目录（有些用户习惯把依赖放在 addons 下一层）
        add(os.path.join(os.path.dirname(pd), DEPS_DIRNAME))

    # 4) Blender 用户目录
    for d in _user_dirs():
        add(os.path.join(d, DEPS_DIRNAME))
        add(os.path.join(d, "ez_v2b_facecap_deps"))

    # 5) 当前工作目录（用户可能在工程目录里手动放了依赖）
    try:
        cwd = os.getcwd()
        add(os.path.join(cwd, DEPS_DIRNAME))
        add(os.path.join(cwd, "ez_v2b_facecap_deps"))
    except Exception:
        pass

    # 6) **开发期候选**（相对推导，不写死绝对路径）
    #
    # 为什么需要这一条：本插件是在一个工程目录里开发出来的，
    # 依赖最初部署在 <工程>/karin_ik_tools/_blpy_nodeps。
    # 上面 1-5 条都是"零开发路径"的正式位置，开发时依赖不在那里，
    # 于是 ez_deps 报"未就绪"、面捕相关验证全跑不起来（实测踩过）。
    #
    # 做法：从插件目录**往上找 2~3 层**，逐层检查常见的开发期位置。
    # 全部是相对推导 —— 分发给用户后这些目录不存在，自然被跳过，
    # 不会引入任何写死的开发机路径。
    if pd:
        base = pd
        for _ in range(3):
            parent = os.path.dirname(base)
            if not parent or parent == base:
                break
            base = parent
            # <上层>/karin_ik_tools/_blpy_nodeps   （旧插件的依赖位置）
            add(os.path.join(base, "karin_ik_tools", DEPS_DIRNAME))
            # <上层>/_face_work/_blpy_nodeps      （面捕开发目录）
            add(os.path.join(base, "_face_work", DEPS_DIRNAME))
            # <上层>/_blpy_nodeps
            add(os.path.join(base, DEPS_DIRNAME))

    return cands


def looks_like_deps(path):
    """判断目录是否真的像依赖目录：至少有 cv2 或 mediapipe"""
    if not path or not os.path.isdir(path):
        return False
    try:
        names = os.listdir(path)
    except Exception:
        return False
    for n in names:
        if n in REQUIRED or n.startswith("cv2."):
            return True
    return False


def deps_dir():
    """返回第一个**真实存在**的依赖目录；都不存在时返回首选路径（供报错显示）"""
    cands = candidates()
    for p in cands:
        if os.path.isdir(p) and looks_like_deps(p):
            return p
    for p in cands:
        if os.path.isdir(p):
            return p
    return cands[0] if cands else DEPS_DIRNAME


def install_target():
    """自动部署的目标目录：优先插件目录下，其次用户 addons 下"""
    pd = _plugin_dir()
    if pd and os.path.isdir(pd):
        try:
            # 可写才用它
            test = os.path.join(pd, ".ez_write_test")
            with open(test, "w") as f:
                f.write("x")
            os.remove(test)
            return os.path.join(pd, DEPS_DIRNAME)
        except Exception:
            pass
    for d in _user_dirs():
        if os.path.isdir(d):
            return os.path.join(d, DEPS_DIRNAME)
    return os.path.join(tempfile.gettempdir(), DEPS_DIRNAME)


def model_path():
    """模型文件路径"""
    try:
        s = getattr(bpy.context.scene, SCENE_MODEL_KEY, "")
        if s and os.path.isfile(s):
            return s
    except Exception:
        pass
    for d in candidates():
        p = os.path.join(d, MODEL_NAME)
        if os.path.isfile(p):
            return p
    return os.path.join(deps_dir(), MODEL_NAME)


# ================================================================ 状态检查

def status():
    """依赖与模型的就绪状态。返回 dict（**永不抛异常**）"""
    out = {"ok": False, "deps": "", "deps_exists": False, "deps_valid": False,
           "model": "", "model_ok": False, "missing": [], "hint": "",
           "cv2": "", "mediapipe": "", "target": ""}
    try:
        out["target"] = install_target()
        d = deps_dir()
        out["deps"] = d
        out["deps_exists"] = os.path.isdir(d)
        out["deps_valid"] = looks_like_deps(d)

        m = model_path()
        out["model"] = m
        out["model_ok"] = (os.path.isfile(m)
                           and os.path.getsize(m) >= MODEL_MIN_BYTES)

        if not out["deps_valid"]:
            out["missing"].append("依赖包（cv2 + mediapipe）")
        if not out["model_ok"]:
            out["missing"].append("模型文件 %s" % MODEL_NAME)

        # 真正尝试导入（这是唯一可靠的判据）
        if out["deps_valid"]:
            if d not in sys.path:
                sys.path.insert(0, d)
            try:
                import cv2
                out["cv2"] = getattr(cv2, "__version__", "?")
            except Exception as e:
                out["missing"].append("import cv2 失败: %s" % e)
            try:
                import mediapipe
                out["mediapipe"] = getattr(mediapipe, "__version__", "?")
            except Exception as e:
                out["missing"].append("import mediapipe 失败: %s" % e)

        out["ok"] = (out["deps_valid"] and out["model_ok"]
                     and not out["missing"])
        if not out["ok"]:
            out["hint"] = ("依赖未就绪。点「安装面捕依赖」按钮，"
                           "或把 %s 目录放到：%s" % (DEPS_DIRNAME, out["target"]))
    except Exception as e:
        out["missing"].append("检查异常: %s" % e)
        out["hint"] = "依赖检查出错：%s" % e
    return out


def status_text():
    """给面板用的一行状态"""
    s = status()
    if s["ok"]:
        return "依赖就绪  cv2 %s / mediapipe %s" % (s["cv2"], s["mediapipe"]), "CHECKMARK"
    if not s["deps_exists"]:
        return "未找到依赖目录", "ERROR"
    if not s["deps_valid"]:
        return "依赖目录不完整", "ERROR"
    if not s["model_ok"]:
        return "缺少模型文件", "ERROR"
    return "依赖异常", "ERROR"


# ================================================================ 部署

def _copy_tree(src, dst, progress=None):
    """复制依赖树。返回 (文件数, 字节数)"""
    n, total = 0, 0
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        tgt = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(tgt, exist_ok=True)
        for f in files:
            s = os.path.join(root, f)
            d = os.path.join(tgt, f)
            try:
                shutil.copy2(s, d)
                n += 1
                total += os.path.getsize(d)
            except Exception:
                pass
            if progress and n % 200 == 0:
                progress("已复制 %d 个文件…" % n)
    return n, total


def deploy_from_dir(src, dst=None, progress=None):
    """从本地目录部署依赖。返回结果 dict"""
    out = {"ok": False, "deps": "", "notes": []}
    if not os.path.isdir(src):
        out["notes"].append("源目录不存在: %s" % src)
        return out
    if not looks_like_deps(src):
        out["notes"].append("源目录不像依赖目录（缺 cv2 / mediapipe）: %s" % src)
        return out
    dst = dst or install_target()
    try:
        if progress:
            progress("部署到 %s …" % dst)
        os.makedirs(dst, exist_ok=True)
        n, total = _copy_tree(src, dst, progress)
        out.update({"ok": True, "deps": dst})
        out["notes"].append("已部署 %d 个文件（%.1f MB）到 %s" % (
            n, total / 1048576.0, dst))
        # 模型
        ms = os.path.join(src, MODEL_NAME)
        if os.path.isfile(ms):
            shutil.copy2(ms, os.path.join(dst, MODEL_NAME))
            out["notes"].append("模型 %s 已就位" % MODEL_NAME)
        else:
            out["notes"].append("源里没有 %s，请单独放置" % MODEL_NAME)
    except Exception as e:
        out["notes"].append("部署失败: %s" % e)
    return out


def deploy_from_zip(zip_path, dst=None, progress=None):
    """从 zip 部署依赖（zip 内应含 _blpy_nodeps/ 或直接是依赖内容）"""
    out = {"ok": False, "deps": "", "notes": []}
    if not os.path.isfile(zip_path):
        out["notes"].append("zip 不存在: %s" % zip_path)
        return out
    tmp = os.path.join(tempfile.gettempdir(), "ez_deps_unzip")
    try:
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        if progress:
            progress("解压依赖包…")
        with zipfile.ZipFile(zip_path) as z:
            if z.testzip() is not None:
                out["notes"].append("zip 损坏")
                return out
            z.extractall(tmp)
        # 找出真正的依赖根：含 cv2 或 mediapipe 的那一层
        root = None
        for dirpath, dirnames, _ in os.walk(tmp):
            for d in dirnames:
                if d in REQUIRED:
                    root = dirpath
                    break
            if root:
                break
        if root is None:
            out["notes"].append("zip 里找不到 cv2 / mediapipe")
            return out
        return deploy_from_dir(root, dst, progress)
    except Exception as e:
        out["notes"].append("解压失败: %s" % e)
        return out


def uninstall(dst=None):
    """移除已部署的依赖"""
    out = {"ok": False, "notes": []}
    d = dst or deps_dir()
    if not os.path.isdir(d):
        out["notes"].append("目录不存在: %s" % d)
        return out
    try:
        shutil.rmtree(d)
        out["ok"] = True
        out["notes"].append("已移除 %s" % d)
    except Exception as e:
        out["notes"].append("移除失败: %s" % e)
    return out
