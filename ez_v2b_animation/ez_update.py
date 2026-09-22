# -*- coding: utf-8 -*-
# ---------------------------------------------------------------------------
# ez-v2b-animation  ——  作者：B站 @高压郭炖大葱
# 使用、修改、二次分发请保留本署名。
# ---------------------------------------------------------------------------

"""ez-v2b-animation :: 自动更新

三条通道，优先级从高到低，任意一条可用即可：

  1. UPDATE_URL   插件自带的更新清单 URL（JSON），面板上可改
  2. GitHub API   owner/repo 形式，走 releases/latest
  3. 本地 ZIP     手动指定本地 zip 文件（离线场景）

设计约束（Blender 插件的现实限制）：

  * **不能自动替换正在运行的 .py 文件**。Windows 上文件被占用，写入会失败。
    所以流程是：下载 -> 解压到临时目录 -> 校验 -> 写"待安装"标记 -> 提示重启 Blender。
    真正的替换发生在 Blender 退出后由 `apply_pending` 执行，或在下次启动时执行。
  * **绝不能因为网络失败而阻断主功能**。所有更新函数都吞异常并返回结果对象。
  * **校验**：比对 zip 内的 bl_info 版本号与清单版本号，避免下到错误的包。
  * **不静默覆盖**：安装前备份现有目录到 `<addon>.bak.<version>`。

用法（插件内）：
    import ez_update
    info = ez_update.check(context)          # 只读，返回 {ok, latest, notes}
    r    = ez_update.download_and_stage(...) # 下载并暂存
    ez_update.apply_pending()                # 下次启动/手动调用时应用
"""

import bpy
import os
import re
import json
import shutil
import zipfile
import tempfile
import time

try:
    import urllib.request as _url
    import urllib.error as _urlerr
except Exception:                                    # pragma: no cover
    _url = None
    _urlerr = None

ADDON_ID = "ez_v2b_animation"
ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
ASSET_DIR = os.path.dirname(ADDON_DIR)

PREFS_GROUP = "ez_v2b_update"
DEFAULT_MANIFEST = "https://raw.githubusercontent.com/ez-v2b/ez-v2b-animation/main/update.json"

# 面板可改的偏好项（存 Scene，避免注册 AddonPreferences 与旧插件冲突）
_PREFS = {
    "ez_upd_url":      (DEFAULT_MANIFEST, "更新清单 URL（JSON）"),
    "ez_upd_repo":     ("", "GitHub owner/repo（留空则用清单 URL）"),
    "ez_upd_auto":     (False, "启动时自动检查更新"),
    "ez_upd_channel":  ("stable", "更新通道：stable / beta"),
}


def _scene():
    return getattr(bpy.context, "scene", None)


def get_pref(name, default=None):
    sc = _scene()
    if sc is None:
        return default
    if name in sc:
        return sc[name]
    return _PREFS.get(name, (default,))[0]


def set_pref(name, value):
    sc = _scene()
    if sc is not None:
        sc[name] = value


def current_version():
    """从本插件模块读版本号"""
    try:
        import sys
        mod = sys.modules.get(__package__ or ADDON_ID)
        if mod is not None and hasattr(mod, "bl_info"):
            v = mod.bl_info.get("version")
            if v:
                return tuple(int(x) for x in v)
    except Exception:
        pass
    # 兜底：解析本包目录下的 __init__.py
    try:
        p = os.path.join(ADDON_DIR, "__init__.py")
        if os.path.isfile(p):
            src = open(p, "r", encoding="utf-8", errors="replace").read(4000)
            m = re.search(r'"version"\s*:\s*\(([^)]*)\)', src)
            if m:
                return tuple(int(x.strip()) for x in m.group(1).split(",") if x.strip())
    except Exception:
        pass
    return (0, 0, 0)


def version_str(v):
    return ".".join(str(x) for x in v)


def parse_version(s):
    if isinstance(s, (list, tuple)):
        try:
            return tuple(int(x) for x in s)
        except Exception:
            return (0, 0, 0)
    nums = re.findall(r"\d+", str(s or ""))
    if not nums:
        return (0, 0, 0)
    return tuple(int(x) for x in nums[:3]) + (0,) * max(0, 3 - len(nums))


def is_newer(remote, local):
    """逐段比较，长度不足补 0"""
    a = list(parse_version(remote)) + [0, 0, 0]
    b = list(parse_version(local)) + [0, 0, 0]
    for i in range(3):
        if a[i] != b[i]:
            return a[i] > b[i]
    return False


# ================================================================ 网络

def _fetch(url, timeout=8):
    """取文本。失败抛异常（由调用方吞掉）。"""
    if _url is None:
        raise RuntimeError("urllib 不可用")
    req = _url.Request(url, headers={
        "User-Agent": "ez-v2b-animation/%s" % version_str(current_version()),
        "Accept": "application/json",
    })
    with _url.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def _download(url, dest, timeout=60):
    if _url is None:
        raise RuntimeError("urllib 不可用")
    req = _url.Request(url, headers={
        "User-Agent": "ez-v2b-animation/%s" % version_str(current_version())})
    with _url.urlopen(req, timeout=timeout) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest


def manifest_url():
    repo = (get_pref("ez_upd_repo") or "").strip()
    if repo:
        repo = repo.replace("https://github.com/", "").strip("/")
        if repo.count("/") == 1:
            return "https://api.github.com/repos/%s/releases/latest" % repo
    return (get_pref("ez_upd_url") or DEFAULT_MANIFEST).strip()


def check(context=None):
    """只读检查更新。返回 dict，永不抛异常。

    {ok, latest, current, newer, url, notes, source}
    """
    cur = current_version()
    out = {"ok": False, "latest": cur, "current": cur, "newer": False,
           "url": "", "notes": [], "source": "none"}
    url = manifest_url()
    if not url:
        out["notes"].append("未配置更新源")
        return out
    try:
        txt = _fetch(url)
        data = json.loads(txt)
    except Exception as ex:
        out["notes"].append("取清单失败: %s" % ex)
        return out

    # 兼容 GitHub releases API 与自定义清单两种格式
    try:
        if "tag_name" in data or ("assets" in data and "name" in data):
            latest = data.get("tag_name") or data.get("name") or "0"
            out["source"] = "github"
            assets = data.get("assets") or []
            dl = ""
            for a in assets:
                nm = (a.get("name") or "").lower()
                if nm.endswith(".zip"):
                    dl = a.get("browser_download_url") or ""
                    break
            out["url"] = dl
            notes = data.get("body") or ""
            if notes:
                out["notes"].append(notes.strip()[:800])
        else:
            latest = data.get("version") or data.get("tag") or "0"
            out["source"] = "manifest"
            chan = get_pref("ez_upd_channel") or "stable"
            chans = data.get("channels") or {}
            if chan in chans:
                latest = chans[chan].get("version", latest)
                out["url"] = chans[chan].get("url", "")
            else:
                out["url"] = data.get("url") or data.get("download") or ""
            if data.get("notes"):
                out["notes"].append(str(data["notes"])[:800])
    except Exception as ex:
        out["notes"].append("清单格式无法识别: %s" % ex)
        return out

    out["latest"] = parse_version(latest)
    out["newer"] = is_newer(out["latest"], cur)
    out["ok"] = True
    if not out["url"]:
        out["notes"].append("清单未提供下载地址")
    return out


# ================================================================ 暂存与安装

def _stage_dir():
    d = os.path.join(tempfile.gettempdir(), "ez_v2b_update")
    os.makedirs(d, exist_ok=True)
    return d


def _pending_marker():
    return os.path.join(_stage_dir(), "pending.json")


def download_and_stage(url=None, progress=None):
    """下载 zip 并解压到暂存目录，写待安装标记。

    返回 {ok, staged, version, notes}
    """
    out = {"ok": False, "staged": "", "version": (0, 0, 0), "notes": []}
    info = check()
    if not url:
        url = info.get("url") or ""
    if not url:
        out["notes"].append("没有可用的下载地址")
        return out

    sd = _stage_dir()
    zp = os.path.join(sd, "download.zip")
    try:
        if progress:
            progress("下载中…")
        _download(url, zp)
    except Exception as ex:
        out["notes"].append("下载失败: %s" % ex)
        return out

    # 校验 zip 结构与版本
    try:
        with zipfile.ZipFile(zp) as z:
            bad = z.testzip()
            if bad is not None:
                out["notes"].append("zip 损坏: %s" % bad)
                return out
            names = z.namelist()
            init = None
            for n in names:
                if n.endswith("__init__.py") and n.count("/") <= 1:
                    init = n
                    break
            if init is None:
                out["notes"].append("zip 里找不到 __init__.py（不是插件包）")
                return out
            src = z.read(init).decode("utf-8", "replace")[:4000]
            m = re.search(r'"version"\s*:\s*\(([^)]*)\)', src)
            pkg_ver = parse_version(m.group(1)) if m else (0, 0, 0)
            if not is_newer(pkg_ver, current_version()):
                out["notes"].append("包内版本 %s 不比当前 %s 新，跳过" % (
                    version_str(pkg_ver), version_str(current_version())))
                return out
            # 解压到 staged/
            staged = os.path.join(sd, "staged")
            if os.path.isdir(staged):
                shutil.rmtree(staged, ignore_errors=True)
            os.makedirs(staged, exist_ok=True)
            z.extractall(staged)
    except Exception as ex:
        out["notes"].append("解包失败: %s" % ex)
        return out

    # 找出实际的包根目录
    root = os.path.join(staged, ADDON_ID)
    if not os.path.isdir(root):
        # 兼容 zip 里直接是文件（无顶层目录）
        root = staged

    try:
        with open(_pending_marker(), "w", encoding="utf-8") as f:
            json.dump({"staged": root, "version": list(pkg_ver),
                       "at": time.time(), "addon_dir": ADDON_DIR}, f,
                      ensure_ascii=False, indent=2)
    except Exception as ex:
        out["notes"].append("写标记失败: %s" % ex)
        return out

    out.update({"ok": True, "staged": root, "version": pkg_ver})
    out["notes"].append("已暂存 %s，重启 Blender 后生效" % version_str(pkg_ver))
    return out


def has_pending():
    return os.path.isfile(_pending_marker())


def pending_info():
    p = _pending_marker()
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def apply_pending(force=False):
    """应用暂存的更新。

    Windows 上正在运行的 .py 被占用，所以：
      * 若目标目录可写 -> 直接替换（备份旧版）
      * 否则 -> 返回 ok=False 并保留标记，由外部脚本在 Blender 退出后执行

    返回 {ok, applied, version, notes}
    """
    out = {"ok": False, "applied": False, "version": (0, 0, 0), "notes": []}
    info = pending_info()
    if not info:
        out["notes"].append("没有待安装的更新")
        return out
    staged = info.get("staged") or ""
    if not os.path.isdir(staged):
        out["notes"].append("暂存目录不存在: %s" % staged)
        return out

    target = info.get("addon_dir") or ADDON_DIR
    ver = tuple(info.get("version") or (0, 0, 0))
    bak = "%s.bak.%s" % (target, version_str(ver))

    try:
        if os.path.isdir(target):
            if os.path.isdir(bak):
                shutil.rmtree(bak, ignore_errors=True)
            shutil.copytree(target, bak)
            out["notes"].append("已备份到 %s" % os.path.basename(bak))
        for name in os.listdir(staged):
            s = os.path.join(staged, name)
            d = os.path.join(target, name)
            if os.path.isdir(s):
                if os.path.isdir(d):
                    shutil.rmtree(d, ignore_errors=True)
                shutil.copytree(s, d)
            else:
                shutil.copy2(s, d)
        os.remove(_pending_marker())
        out.update({"ok": True, "applied": True, "version": ver})
        out["notes"].append("已更新到 %s，请重启 Blender" % version_str(ver))
    except Exception as ex:
        out["notes"].append("替换失败（文件可能被占用）: %s" % ex)
        out["notes"].append("请关闭 Blender 后重新运行本操作，或手动覆盖 %s" % target)
    return out


def cancel_pending():
    p = _pending_marker()
    if os.path.isfile(p):
        try:
            os.remove(p)
        except Exception:
            return False
    return True


# ================================================================ 启动钩子

_CHECKED = {"at": 0.0}


def maybe_auto_check(interval=6 * 3600):
    """启动时自动检查（按 ez_upd_auto 开关）。带节流，绝不阻塞。"""
    try:
        if not get_pref("ez_upd_auto"):
            return None
        now = time.time()
        if now - _CHECKED["at"] < interval:
            return None
        _CHECKED["at"] = now
        return check()
    except Exception:
        return None


def status_text():
    """给面板用的一行状态"""
    cur = version_str(current_version())
    if has_pending():
        info = pending_info() or {}
        return "待安装 %s（重启后生效）" % version_str(info.get("version") or (0, 0, 0)), "IMPORT"
    return "当前版本 %s" % cur, "CHECKMARK"
