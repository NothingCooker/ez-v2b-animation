# -*- coding: utf-8 -*-
"""打包面捕依赖为可分发 zip

面捕依赖约 250 MB / 1557 文件（已剔除 __pycache__），与主插件（98 KB）分开分发：
主插件保持小体积便于频繁更新，依赖只在首次安装时下一次。

用法:
    python _autorig_work/pack_deps.py
"""
import os
import sys
import shutil
import zipfile
import hashlib
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ASS = os.path.dirname(HERE)

DEPS_DIRNAME = "_blpy_nodeps"
MODEL_NAME = "face_landmarker.task"
ZIP = os.path.join(ASS, "ez_v2b_facecap_deps.zip")

# 依赖源候选（按优先级：先选"最干净"的那个）
# _blpy_nodeps 是当初用 --no-deps 装出来的最小集，复用 Blender 自带 numpy。
# _blpy_deps 是早期全依赖版（324.9 MB），会把 numpy 顶成 2.x，**不能分发**。
SRC_CANDIDATES = [
    os.path.join(ASS, "karin_ik_tools", DEPS_DIRNAME),
    os.path.join(ASS, "_face_work", DEPS_DIRNAME),
]

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


P("=" * 74)
P("打包面捕依赖")
P("=" * 74)

# ---------------------------------------------------------------- 1. 选源
P("")
P("--- 选择依赖源 ---")
src = None
for c in SRC_CANDIDATES:
    if os.path.isdir(c) and os.path.isdir(os.path.join(c, "cv2")):
        src = c
        break
chk(src is not None, "找到可用的依赖源")
if src is None:
    P("候选: %s" % SRC_CANDIDATES)
    sys.exit(1)

P("  源目录: %s" % src)

# 统计
n_files = 0
n_bytes = 0
for root, dirs, files in os.walk(src):
    # 跳过缓存
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for f in files:
        if f.endswith(".pyc"):
            continue
        try:
            n_bytes += os.path.getsize(os.path.join(root, f))
            n_files += 1
        except Exception:
            pass
P("  内容: %d 文件, %.1f MB" % (n_files, n_bytes / 1048576.0))

# ---------------------------------------------------------------- 2. 关键内容自检
P("")
P("--- 关键内容自检 ---")
must_have = [
    ("cv2", "opencv（抓帧）"),
    ("mediapipe", "mediapipe（推理）"),
    (MODEL_NAME, "FaceLandmarker 模型"),
]
for name, label in must_have:
    p = os.path.join(src, name)
    ok = os.path.exists(p)
    chk(ok, "包含 %s（%s）" % (name, label))
    if ok and os.path.isfile(p):
        sz = os.path.getsize(p)
        chk(sz > 1_000_000, "%s 大小合理（%.2f MB）" % (name, sz / 1048576.0))

# 危险内容自检：绝不能把 numpy 打进去（会顶掉 Blender 自带的 1.26.4）
P("")
P("--- 危险内容自检（必须不含）---")
danger = {
    "numpy": "会顶掉 Blender 自带 numpy 1.26.4（实测把 1.26.4 顶成 2.4.6，面捕直接崩）",
}
for name, why in danger.items():
    p = os.path.join(src, name)
    present = os.path.isdir(p)
    chk(not present, "不含 %s  —— %s" % (name, why if present else "已确认"))

# matplotlib 是 mediapipe 的硬依赖，必须留
P("")
P("--- 硬依赖自检（必须包含）---")
hard = {
    "matplotlib": "mediapipe 的硬依赖，删了会 ModuleNotFoundError",
    "PIL": "matplotlib 的下游",
    "fontTools": "matplotlib 的下游",
}
for name, why in hard.items():
    p = os.path.join(src, name)
    present = os.path.isdir(p)
    chk(present, "包含 %s —— %s" % (name, why))

# ---------------------------------------------------------------- 3. 打 zip
P("")
P("--- 打包 ---")
if os.path.exists(ZIP):
    os.remove(ZIP)

t0 = time.time()
count = 0
with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".pyc"):
                continue
            full = os.path.join(root, f)
            rel = os.path.relpath(full, src)
            # zip 内顶层目录固定为 _blpy_nodeps，方便用户直接解压到插件目录
            arc = "%s/%s" % (DEPS_DIRNAME, rel.replace("\\", "/"))
            try:
                z.write(full, arc)
                count += 1
            except Exception as e:
                P("  跳过 %s: %s" % (rel, e))
dt = time.time() - t0

zip_size = os.path.getsize(ZIP)
P("  已写 %s" % ZIP)
P("  %d 个条目  压缩后 %.1f MB（原始 %.1f MB，压缩率 %.0f%%）  耗时 %.1fs" % (
    count, zip_size / 1048576.0, n_bytes / 1048576.0,
    100.0 * zip_size / max(1, n_bytes), dt))

# ---------------------------------------------------------------- 4. 校验
P("")
P("--- zip 校验 ---")
with zipfile.ZipFile(ZIP) as z:
    bad = z.testzip()
    chk(bad is None, "完整性 OK")
    names = z.namelist()
    P("  条目数 %d" % len(names))
    chk(len(names) == count, "条目数与写入一致")

    # 关键条目在 zip 内
    top = set()
    for n in names:
        parts = n.split("/")
        if len(parts) > 1:
            top.add(parts[1])
    for name, label in must_have:
        chk(name in top, "zip 内含 %s（%s）" % (name, label))
    chk("numpy" not in top, "zip 内不含 numpy")
    chk(all(n.startswith(DEPS_DIRNAME + "/") for n in names),
        "全部条目在 %s/ 下（解压即用）" % DEPS_DIRNAME)

# ---------------------------------------------------------------- 5. 摘要
sha = hashlib.sha256(open(ZIP, "rb").read()).hexdigest()
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
    P("产物: %s" % os.path.basename(ZIP))
    P("  大小   %.1f MB" % (zip_size / 1048576.0))
    P("  sha256 %s" % sha)
    P("  用法   解压后得到 %s/ 目录，放到插件目录下即可" % DEPS_DIRNAME)
    P("         或用面板的「安装面捕依赖」-> 「从指定目录/zip 部署」" % ())
    P("=" * 74)

text = "\n".join(log)
open(os.path.join(HERE, "pack_deps.txt"), "w", encoding="utf-8").write(text)
print(text)
sys.stdout.flush()
if fails:
    sys.exit(1)
