# -*- coding: utf-8 -*-
"""生成 SHA256SUMS.txt，覆盖 Release 的全部产物

为什么需要这个脚本：
  发布到 Release 的 zip 是 CI 重新构建的，哈希与本地开发目录的产物**不同**。
  所以校验和必须在发布之后、从 Release 实际下载的产物上计算，
  不能拿本地文件凑数（否则用户核对会失败）。

用法:
    python tools/make_sha256sums.py <目录>       # 从目录里的文件生成
    python tools/make_sha256sums.py --from-release v1.0.1   # 从 Release 下载后生成
"""

import hashlib
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Release 的产物清单（顺序即输出顺序）
ASSETS = (
    "ez_v2b_animation.zip",
    "ez_v2b_animation_single.py",
    "ez_v2b_facecap_deps.zip",
)


def sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def from_dir(d):
    lines = []
    for name in ASSETS:
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            print("  跳过（不存在）: %s" % name)
            continue
        h = sha256(p)
        lines.append("%s  %s" % (h, name))
        print("  %-34s %s" % (name, h))
    return lines


def from_release(tag):
    """从 GitHub Release 下载全部产物，再算哈希。

    这是唯一可靠的来源：Release 里的 zip 由 CI 构建，
    与本地开发目录的产物不是同一份文件。
    """
    tmp = tempfile.mkdtemp(prefix="ez_sha_")
    print("下载 %s 的产物到 %s" % (tag, tmp))
    r = subprocess.run(
        ["gh", "release", "download", tag, "--dir", tmp, "--clobber"],
        capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        print("下载失败: %s" % (r.stderr or "")[:300])
        return None
    return from_dir(tmp)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1

    if args[0] == "--from-release":
        tag = args[1] if len(args) > 1 else "v1.0.1"
        lines = from_release(tag)
        if lines is None:
            return 1
    else:
        lines = from_dir(args[0])

    if not lines:
        print("没有任何产物，未生成校验和")
        return 1

    out = os.path.join(ROOT, "SHA256SUMS.txt")
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print("")
    print("已写 %s，%d 条" % (out, len(lines)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
