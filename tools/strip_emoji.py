# -*- coding: utf-8 -*-
"""清除待发布文件里的 emoji（保留箭头等排版符号）

区分两类字符：
  真正的 emoji   -> 必须清除
      U+2705 勾   U+274C 叉   U+26A0 警告   U+FE0F 变体选择符
      U+200D 零宽连接符   U+2665 心   U+1F300-1FAFF emoji 主区
  排版符号       -> 保留（不是 emoji，是数学/排版字符）
      U+2192 右箭头   U+2190 左箭头   U+2194 双向箭头
      U+21D2 双线右箭头   U+2014 破折号   U+2026 省略号

说明：本文件自身也含 U+2713 / U+2717 等码点，那是"要清除的字符"的定义，
不是 emoji 的使用。为保持仓库零 emoji，下面统一用码点转义书写。

用法: python tools/strip_emoji.py [--check]
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ASS = os.path.dirname(HERE)

# 待发布文件
TARGETS = [
    os.path.join(ASS, "ez_v2b_animation"),
    os.path.join(ASS, "EZ_V2B_README.md"),
    os.path.join(ASS, "update.json"),
    os.path.join(HERE, "pack_ez.py"),
    os.path.join(HERE, "port_karin.py"),
    os.path.join(HERE, "pack_deps.py"),
    os.path.join(HERE, "sign_addon.py"),
]
EXT = (".py", ".md", ".json", ".txt", ".yml", ".yaml", ".cfg", ".toml")

# 要清除的 emoji（正则字符类）
EMOJI_PAT = re.compile(
    "["
    "\U0001F000-\U0001FAFF"      # emoji 主区与补充
    "\u2600-\u26FF"              # 杂项符号（含 2600 太阳、26A0 警告、2665 心）
    "\u2700-\u27BF"              # 装饰符号（含 2705 勾、274C 叉、2728 星）
    "\u2B00-\u2BFF"              # 杂项符号与箭头（含 2B50 星）
    "\uFE0F"                     # 变体选择符
    "\u200D"                     # 零宽连接符
    "\u20E3"                     # 组合包围键帽
    "\u2190-\u21FF"              # 箭头：**保留**，见下方白名单
    "\u2708\u2709\u270A-\u270D"  # 少量装饰
    "]+")

# 白名单：这些虽然在上面范围内，但要保留（排版符号，不是 emoji）
KEEP_CHARS = set(
    "\u2190"   # ← 左箭头
    "\u2192"   # → 右箭头
    "\u2194"   # ↔ 双向箭头
    "\u21D2"   # ⇒ 双线右箭头
    "\u21D0"   # ⇐
    "\u21D4"   # ⇔
    "\u2191"   # ↑ 上箭头
    "\u2193"   # ↓ 下箭头
    "\u2212"   # − 减号
)


def clean(s):
    """清除 emoji，保留白名单排版符号。返回 (新文本, 清除数, 清除明细)"""
    removed = {}

    def repl(m):
        keep = []
        drop = []
        for ch in m.group(0):
            if ch in KEEP_CHARS:
                keep.append(ch)
            else:
                drop.append(ch)
                removed[ch] = removed.get(ch, 0) + 1
        return "".join(keep)

    out = EMOJI_PAT.sub(repl, s)
    # 清理因删除 emoji 产生的多余空格（如 "xxx U+2713" -> "xxx"）
    out = re.sub(r"[ \t]+(?=\n)", "", out)
    out = re.sub(r"[ \t]{2,}(?=[\u4e00-\u9fff])", " ", out)
    return out, sum(removed.values()), removed


def iter_files():
    for t in TARGETS:
        if os.path.isfile(t):
            yield t
        elif os.path.isdir(t):
            for root, dirs, files in os.walk(t):
                dirs[:] = [d for d in dirs if d not in ("__pycache__",)]
                for f in files:
                    if f.endswith(EXT):
                        yield os.path.join(root, f)


CHECK_ONLY = "--check" in sys.argv

log = []
def P(s=""):
    log.append(str(s))

P("=" * 74)
P("清除待发布文件里的 emoji" + ("（只检查，不修改）" if CHECK_ONLY else ""))
P("=" * 74)
P("保留的排版符号: %s" % " ".join(sorted(KEEP_CHARS)))
P("")

total_files = 0
total_removed = 0
changed_files = []
all_removed = {}

for p in iter_files():
    try:
        src = open(p, encoding="utf-8").read()
    except Exception as e:
        P("  读取失败 %s: %s" % (p, e))
        continue
    total_files += 1
    new, n, removed = clean(src)
    if n == 0:
        continue
    rel = os.path.relpath(p, ASS)
    changed_files.append((rel, n))
    for ch, c in removed.items():
        all_removed[ch] = all_removed.get(ch, 0) + c
    total_removed += n
    if not CHECK_ONLY:
        open(p, "w", encoding="utf-8", newline="\n").write(new)

P("扫描 %d 个文件" % total_files)
P("")
if changed_files:
    P("含 emoji 的文件 %d 个:" % len(changed_files))
    for rel, n in sorted(changed_files, key=lambda x: -x[1]):
        P("  %-46s %d 处" % (rel, n))
    P("")
    P("清除的字符统计:")
    for ch, c in sorted(all_removed.items(), key=lambda x: -x[1]):
        # 用码点输出，避免本工具自身引入 emoji
        P("  U+%04X  %d 次" % (ord(ch), c))
    P("")
    P("合计清除 %d 处" % total_removed)
else:
    P("未发现 emoji（干净）")

P("")
P("=" * 74)

text = "\n".join(log)
print(text)
sys.stdout.flush()
