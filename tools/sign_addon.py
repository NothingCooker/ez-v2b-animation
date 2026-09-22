# -*- coding: utf-8 -*-
"""给 ez-v2b-animation 的每个模块统一加署名

幂等：已署名的文件会先移除旧署名块再加新的，重复跑不会累积。

用法: python _autorig_work/sign_addon.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ASS = os.path.dirname(HERE)
ADDON = os.path.join(ASS, "ez_v2b_animation")

AUTHOR = "B站 @高压郭炖大葱"

# 署名块（插在编码声明之后）
SIGN_BLOCK = """# ---------------------------------------------------------------------------
# ez-v2b-animation  ——  作者：%s
# 使用、修改、二次分发请保留本署名。
# ---------------------------------------------------------------------------
""" % AUTHOR

# 移植块（自动生成，勿手改）——这些文件的署名写在生成器里，这里也补一份
GENERATED = {"ez_port_ik_control.py", "ez_port_face.py", "ez_port_facecap.py"}

SKIP = {"__init__.py"}          # __init__ 的署名在 docstring 与 bl_info 里，单独处理

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


def strip_old_sign(src):
    """移除旧的署名块（幂等）"""
    # 我们自己的分隔线块
    src = re.sub(
        r"# -{70,}\n# ez-v2b-animation.*?\n# 使用、修改、二次分发请保留本署名。\n"
        r"# -{70,}\n", "", src, flags=re.S)
    return src


def add_sign(path):
    fn = os.path.basename(path)
    if fn in SKIP:
        return None
    src = open(path, encoding="utf-8").read()
    if AUTHOR in src and "使用、修改、二次分发请保留本署名" in src:
        return "already"

    src = strip_old_sign(src)

    lines = src.split("\n")
    # 找到编码声明行（若有）
    idx = 0
    if lines and lines[0].startswith("# -*- coding"):
        idx = 1
    # 在编码声明后插入署名
    out = lines[:idx] + SIGN_BLOCK.rstrip("\n").split("\n") + [""] + lines[idx:]
    new = "\n".join(out)
    open(path, "w", encoding="utf-8", newline="\n").write(new)
    return "added"


P("=" * 74)
P("为 ez-v2b-animation 添加署名：%s" % AUTHOR)
P("=" * 74)

for fn in sorted(os.listdir(ADDON)):
    if not fn.endswith(".py"):
        continue
    p = os.path.join(ADDON, fn)
    r = add_sign(p)
    if r is None:
        P("  跳过 %-24s（署名在 docstring 里）" % fn)
    elif r == "already":
        P("  已有 %-24s" % fn)
    else:
        P("  已加 %-24s" % fn)

# ---------------------------------------------------------------- 校验
P("")
P("--- 校验：每个模块都必须含署名 ---")
for fn in sorted(os.listdir(ADDON)):
    if not fn.endswith(".py"):
        continue
    src = open(os.path.join(ADDON, fn), encoding="utf-8").read()
    chk(AUTHOR in src, "%s 含署名" % fn)

# ---------------------------------------------------------------- 语法
P("")
P("--- 语法检查 ---")
import ast
for fn in sorted(os.listdir(ADDON)):
    if not fn.endswith(".py"):
        continue
    p = os.path.join(ADDON, fn)
    raw = open(p, "rb").read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
        open(p, "wb").write(raw)
        P("  已剥离 BOM: %s" % fn)
    try:
        ast.parse(raw.decode("utf-8"))
        P("  语法 OK  %s" % fn)
    except SyntaxError as e:
        chk(False, "%s 语法错误 line %s: %s" % (fn, e.lineno, e.msg))

# ---------------------------------------------------------------- 幂等
P("")
P("--- 幂等检查（再跑一次不应变化）---")
before = {}
for fn in sorted(os.listdir(ADDON)):
    if fn.endswith(".py"):
        before[fn] = open(os.path.join(ADDON, fn), encoding="utf-8").read()
for fn in sorted(os.listdir(ADDON)):
    if fn.endswith(".py"):
        add_sign(os.path.join(ADDON, fn))
changed = []
for fn in sorted(os.listdir(ADDON)):
    if fn.endswith(".py"):
        now = open(os.path.join(ADDON, fn), encoding="utf-8").read()
        if now != before[fn]:
            changed.append(fn)
chk(not changed, "重复运行不改变文件（%s）" % (changed or "无变化"))

P("")
P("=" * 74)
if fails:
    P("失败 %d 项:" % len(fails))
    for f in fails:
        P("  - %s" % f)
else:
    P("全部通过")
P("=" * 74)

text = "\n".join(log)
open(os.path.join(HERE, "sign_addon.txt"), "w", encoding="utf-8").write(text)
print(text)
sys.stdout.flush()
if fails:
    sys.exit(1)
