# -*- coding: utf-8 -*-
"""把版本号改成指定值（同时改 docstring、__version__、bl_info）

用法: python tools/bump_version.py 1.0.1
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
INIT = os.path.join(ROOT, "ez_v2b_animation", "__init__.py")


def main():
    if len(sys.argv) < 2:
        print("用法: python tools/bump_version.py <版本号，如 1.0.1>")
        return 1
    ver = sys.argv[1].strip()
    parts = ver.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        print("版本号必须是三段数字，如 1.0.1")
        return 1

    s = open(INIT, encoding="utf-8").read()
    old_doc = re.search(r"^版本\s+(\S+)", s, flags=re.M)
    old_ver = re.search(r"^__version__ = \(([^)]*)\)", s, flags=re.M)
    old_bl = re.search(r'"version": \(([^)]*)\),', s)

    s = re.sub(r"^版本\s+\S+", "版本     " + ver, s, count=1, flags=re.M)
    s = re.sub(r"^__version__ = \([^)]*\)",
               "__version__ = (%s)" % ", ".join(parts), s, count=1, flags=re.M)
    s = s.replace('"version": (1, 0, 0),',
                  '"version": (%s),' % ", ".join(parts))

    open(INIT, "w", encoding="utf-8", newline="\n").write(s)

    print("版本号: %s -> %s" % (
        old_doc.group(1) if old_doc else "?", ver))
    print("  __version__ : %s -> (%s)" % (
        old_ver.group(1) if old_ver else "?", ", ".join(parts)))
    print("  bl_info     : %s -> (%s)" % (
        old_bl.group(1) if old_bl else "?", ", ".join(parts)))

    # 自检：三处必须一致
    s2 = open(INIT, encoding="utf-8").read()
    ok = (("版本     " + ver) in s2
          and ("__version__ = (%s)" % ", ".join(parts)) in s2
          and ('"version": (%s),' % ", ".join(parts)) in s2)
    print("  三处一致: %s" % ("是" if ok else "否"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
