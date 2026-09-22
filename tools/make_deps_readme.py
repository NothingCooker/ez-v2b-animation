# -*- coding: utf-8 -*-
"""生成面捕依赖包的说明文档

放在独立脚本里而不是写进 workflow 的 heredoc：
YAML 块标量对缩进敏感，中文内容里的三引号容易把 YAML 解析搞坏
（实测：heredoc 里的 Python 多行字符串导致 workflow 在解析阶段就失败，
 total_count 为 0，0 秒退出）。

用法: python tools/make_deps_readme.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "DEPS_README.md")

TEXT = """# 面捕依赖包

本仓库 Releases 中的依赖包为面捕依赖，与插件本体分开分发。

## 为什么分开

| 项目 | 大小 | 更新频率 |
| --- | --- | --- |
| 插件本体 | 约 110 KB | 频繁 |
| 面捕依赖 | 约 94 MB | 极少 |

依赖打包进主包会让每次插件更新都要重下 94 MB，因此分开分发。

## 内容

    _blpy_nodeps/
      cv2                      opencv 5.0.0，负责抓帧
      mediapipe                mediapipe 1.0.1，负责推理
      face_landmarker.task     FaceLandmarker 模型，3.6 MB
      matplotlib               mediapipe 的硬依赖
      PIL / fontTools          matplotlib 的下游
      sounddevice              提示音

## 注意

依赖包不含 numpy。numpy 复用 Blender 自带的 1.26.4，
若被依赖包里的版本覆盖会导致面捕无法启动。

## 安装

1. 下载依赖包并解压
2. 把 _blpy_nodeps 目录放到插件目录下
3. 或在 Blender 面板中：面部捕捉 > 安装面捕依赖 > 从指定目录或 zip 部署

插件会自动在以下位置查找依赖（按优先级）：

    <插件目录>/_blpy_nodeps
    <插件目录的兄弟目录>/_blpy_nodeps
    <Blender 用户 addons>/_blpy_nodeps
    <Blender 用户目录>/ez_v2b_facecap_deps
    <当前工作目录>/_blpy_nodeps

## 打包

依赖包由 tools/pack_deps.py 生成，脚本内置内容自检：
关键包存在性、模型文件大小、不含 numpy、硬依赖完整。
"""


def main():
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(TEXT)
    print("已生成 %s，%d 字节" % (OUT, os.path.getsize(OUT)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
