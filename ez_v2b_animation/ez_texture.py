# -*- coding: utf-8 -*-
# ---------------------------------------------------------------------------
# ez-v2b-animation  ——  作者：B站 @高压郭炖大葱
# 使用、修改、二次分发请保留本署名。
# ---------------------------------------------------------------------------

"""ez-v2b-animation :: 纹理重绑与打包

解决两个真实痛点（FBX 导入的模型尤其常见）：

  1. **纹理路径失效**：FBX 里只存文件名，实际贴图在解压目录的 Textures/ 下。
     导入后 Blender 找不到，材质全紫。
  2. **换机器丢贴图**：外部引用的贴图不会跟着 .blend 走。17 张贴图打包后
     文件从 11 MB 涨到 56 MB，但换来的是传文件不丢（文档第九节实测）。

做法：
  1. 按 basename 递归扫描指定目录，建立 {文件名: 真实路径} 索引
  2. 逐张重指路径 -> reload -> pack()
  3. 返回详细报告（重绑几张 / 打包几张 / 哪些找不到）

实测依据（Rusk_优化说明.md 二）：
  FBX 里存 `Rusk_Face.png`，实际在 `Textures/Rusk_Face.png`，
  按 basename 递归查找后重指路径，5 张全部命中，打包 5/5。
"""

import bpy
import os
import sys


# ================================================================ 扫描

IMAGE_EXT = (".png", ".jpg", ".jpeg", ".tga", ".bmp", ".tif", ".tiff",
             ".exr", ".hdr", ".webp", ".dds", ".psd")


def build_index(roots, progress=None):
    """递归扫描目录，建立 {小写文件名: 真实路径} 索引。

    多目录时后者覆盖前者（按传入顺序，用户指定优先）。
    """
    idx = {}
    if isinstance(roots, str):
        roots = [roots]
    n = 0
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirs, files in os.walk(root):
            # 跳过常见的无关目录
            dirs[:] = [d for d in dirs
                       if d.lower() not in ("__pycache__", ".git", ".svn")]
            for f in files:
                if not f.lower().endswith(IMAGE_EXT):
                    continue
                idx[f.lower()] = os.path.join(dirpath, f)
                n += 1
                if progress and n % 200 == 0:
                    progress("已索引 %d 张…" % n)
    return idx


def collect_images(arm=None, all_images=False):
    """收集要处理的贴图。

    arm 非 None 时只处理**该骨架实际引用的材质**用到的图
    （文档 5.7：全表遍历会误伤其他角色的材质）。
    """
    out = []
    if all_images or arm is None:
        for img in bpy.data.images:
            if img.name in ("Render Result", "Viewer Node", "Dirty"):
                continue
            out.append(img)
        return out

    # 只取该骨架关联网格引用的材质
    mats = set()
    for o in bpy.data.objects:
        if o.type != "MESH":
            continue
        if not any(m.type == "ARMATURE" and m.object == arm for m in o.modifiers):
            continue
        for slot in o.material_slots:
            if slot.material is not None:
                mats.add(slot.material)
    seen = set()
    for m in mats:
        if not m.use_nodes or m.node_tree is None:
            continue
        for node in m.node_tree.nodes:
            if node.type != "TEX_IMAGE" or node.image is None:
                continue
            img = node.image
            if img.name in seen or img.name in ("Render Result", "Viewer Node"):
                continue
            seen.add(img.name)
            out.append(img)
    return out


# ================================================================ 重绑

def rebind(images, index, progress=None):
    """按 basename 重指路径。返回报告 dict"""
    rep = {"rebound": [], "missing": [], "skipped": []}
    for i, img in enumerate(images):
        fp = img.filepath or ""
        base = os.path.basename(fp)
        if not base:
            # 没有 filepath：可能是已打包的图或生成的图
            if img.packed_file is not None:
                rep["skipped"].append((img.name, "已打包"))
            else:
                rep["skipped"].append((img.name, "无路径"))
            continue
        real = index.get(base.lower())
        if real is None:
            rep["missing"].append((img.name, base))
            continue
        if os.path.normpath(fp) == os.path.normpath(real):
            rep["skipped"].append((img.name, "路径已正确"))
            continue
        try:
            img.filepath = real
            img.filepath_raw = real
            try:
                img.reload()
            except Exception as e:
                rep["skipped"].append((img.name, "reload 失败: %s" % e))
            rep["rebound"].append((img.name, base, tuple(img.size)))
        except Exception as e:
            rep["missing"].append((img.name, "%s（设置失败: %s）" % (base, e)))
        if progress and i % 20 == 0:
            progress("已处理 %d/%d 张…" % (i + 1, len(images)))
    return rep


# ================================================================ 打包

def pack(images, progress=None):
    """打包贴图。返回报告 dict"""
    rep = {"packed": [], "failed": [], "already": [], "empty": []}
    for i, img in enumerate(images):
        try:
            if img.packed_file is not None:
                rep["already"].append(img.name)
                continue
            # 空图（没加载成功）打包没意义，且会报错
            if img.size[0] == 0 or img.size[1] == 0:
                rep["empty"].append(img.name)
                continue
            img.pack()
            if img.packed_file is not None:
                rep["packed"].append((img.name, tuple(img.size)))
            else:
                rep["failed"].append((img.name, "pack() 后仍无 packed_file"))
        except Exception as e:
            rep["failed"].append((img.name, str(e)))
        if progress and i % 20 == 0:
            progress("已打包 %d/%d 张…" % (i + 1, len(images)))
    return rep


def unpack_all(images):
    """取消打包（写回磁盘）。谨慎使用。"""
    n = 0
    for img in images:
        try:
            if img.packed_file is not None:
                img.unpack(method="USE_ORIGINAL")
                n += 1
        except Exception:
            pass
    return n


# ================================================================ 统计

def stats(images):
    """贴图统计：总张数 / 已打包 / 缺文件 / 磁盘占用"""
    out = {"total": 0, "packed": 0, "missing": 0, "empty": 0,
           "bytes": 0, "items": []}
    for img in images:
        out["total"] += 1
        pk = img.packed_file is not None
        empty = (img.size[0] == 0 or img.size[1] == 0)
        exists = bool(img.filepath) and os.path.isfile(bpy.path.abspath(img.filepath))
        if pk:
            out["packed"] += 1
            try:
                out["bytes"] += img.packed_file.size
            except Exception:
                pass
        elif empty:
            out["empty"] += 1
        elif not exists:
            out["missing"] += 1
        out["items"].append({
            "name": img.name,
            "size": tuple(img.size),
            "packed": pk,
            "exists": exists,
            "path": img.filepath or "",
        })
    return out


def stats_text(st):
    return ("贴图 %d 张：已打包 %d / 缺文件 %d / 空图 %d，打包体积 %.1f MB" % (
        st["total"], st["packed"], st["missing"], st["empty"],
        st["bytes"] / 1048576.0))


# ================================================================ 一步到位

def process(arm=None, roots=None, do_rebind=True, do_pack=True,
            all_images=False, progress=None):
    """一站式：扫描 -> 重绑 -> 打包。

    roots: 纹理搜索目录（可多个）。为 None 时自动推断：
           .blend 所在目录 + 其 Textures 子目录
    """
    out = {"ok": False, "notes": [], "index_size": 0,
           "rebind": None, "pack": None, "stats": None}

    # 推断搜索目录
    if not roots:
        roots = []
        try:
            bf = bpy.data.filepath
            if bf:
                d = os.path.dirname(bf)
                roots.append(d)
                for sub in ("Textures", "textures", "Texture", "tex", "maps"):
                    p = os.path.join(d, sub)
                    if os.path.isdir(p):
                        roots.append(p)
        except Exception:
            pass
        if not roots:
            out["notes"].append("未能推断纹理目录，请手动指定")
            return out

    roots = [r for r in roots if os.path.isdir(r)]
    if not roots:
        out["notes"].append("指定的目录都不存在")
        return out

    images = collect_images(arm, all_images)
    if not images:
        out["notes"].append("没有找到可处理的贴图")
        return out

    if do_rebind:
        if progress:
            progress("扫描纹理目录…")
        idx = build_index(roots, progress)
        out["index_size"] = len(idx)
        out["notes"].append("扫描 %d 个目录，索引 %d 张图" % (len(roots), len(idx)))
        rb = rebind(images, idx, progress)
        out["rebind"] = rb
        out["notes"].append("重绑 %d 张，未找到 %d 张，跳过 %d 张" % (
            len(rb["rebound"]), len(rb["missing"]), len(rb["skipped"])))

    if do_pack:
        pk = pack(images, progress)
        out["pack"] = pk
        out["notes"].append("打包 %d 张（已打包 %d，失败 %d，空图 %d）" % (
            len(pk["packed"]), len(pk["already"]),
            len(pk["failed"]), len(pk["empty"])))

    out["stats"] = stats(images)
    out["ok"] = True
    return out
