# -*- coding: utf-8 -*-
# ---------------------------------------------------------------------------
# ez-v2b-animation  ——  作者：B站 @高压郭炖大葱
# 使用、修改、二次分发请保留本署名。
# ---------------------------------------------------------------------------
# 由 port_karin.py 从 karin_ik_tools 移植并适配（勿手改，改 port_karin.py 重跑）
# 命名空间: karin.* -> ez.*  /  KARIN_OT_ -> EZ_OT_  /  面板分类 -> EZ V2B
# 共享工具由 ez_bridge 注入（_rig / _ensure_rigs / _upd / _face_meshes 等）

import bpy
import math
import json
import os
import re
import sys
import time
import wave
import queue
import threading
import subprocess
import numpy as np
from mathutils import Matrix, Vector, Quaternion, Euler

try:
    from .ez_bridge import *
    from .ez_bridge import D, M, B, RIGS, SIDE_ITEMS
except Exception:
    from ez_bridge import *
    from ez_bridge import D, M, B, RIGS, SIDE_ITEMS

# ==================== FACECAP BLOCK BEGIN（自动生成，勿手改）====================

# -*- coding: utf-8 -*-
# ============================================================================
# 面部捕捉（Webcam Face Capture）—— 并入 karin_ik_tools 的独立区块
#
# 【零硬编码原则】与原插件的角色发现一脉相承：
#   - 角色：靠骨架上的 rig_profile(JSON) 自动发现，不写死角色名
#   - 头部骨骼与手柄：靠 TRACK_TO 约束结构反查，不写死 "Head" / "Hik" / "Headik"
#   - 形态键：优先按语义候选名匹配，全部未命中时退化为模糊发现，不写死键名
#   - 标定矩阵 M 与距离 D：每次运行时实测，不缓存、不硬编码
#
# 依赖：mediapipe + opencv，用 --no-deps 装进独立目录，复用 Blender 自带
#       numpy 1.26.4（绝不覆盖，实测见 _face_work/variant_b.txt）。
#
# 头部机制实测（_face_work/calib_matrix.txt、head_discover.txt）：
#   Head 骨骼挂 TRACK_TO(track=TRACK_Z, up=UP_Y)，目标是头部手柄
#   Karin      M=diag(1.0000,-1.0000,-1.0000)  D=0.3757
#   Chocolate  M=diag(0.0100,-0.0100,-0.0100)  D=0.2946
#   Rusk       M 含旋转耦合，必须用完整矩阵         D=0.3001
#   M 与「骨架世界旋转 × 缩放」无关（实测差 2.0，根因是骨骼矩阵轴翻转约定），
#   所以只能运行时标定 —— 这条是踩过坑才写下的。
#   手柄沿世界 X 移动 -> 偏航；沿世界 Z 移动 -> 俯仰。
#   TRACK_TO 只有 2 个自由度，无法表达绕视线轴的 roll（歪头），这是机制限制。
# ============================================================================

FACECAP_DEPS_DEFAULT = "_blpy_nodeps"
FACECAP_MODEL_NAME = "face_landmarker.task"

# 摄像头打开的总超时（秒）。实测 MSMF 构造约 8s、ANY 约 7s，
# 三个后端串行最坏 15s+。给 25s 余量，超时明确报错而不是无限等。
FC_OPEN_TIMEOUT = 25.0

# 运行时断流自动重连。实测 0xC00D36C3 是设备被判失效，
# 释放句柄重新打开即可恢复；给 3 次机会，避免设备真拔掉时无限重试。
FC_READFAIL_RECONNECT = 30     # 连续失败多少帧后触发重连
FC_RECONNECT_MAX = 3           # 最多重连几次

_FACECAP_MOD = {}


# ---------------------------------------------------------------- 依赖路径
def _facecap_here():
    """本文件所在目录。

    注意：Register 文本块是被 Blender 用 exec 执行的，此时没有 __file__，
    不能直接 os.path.dirname(__file__)。
    查找顺序：
      1) FACECAP_HOME（合并脚本注入的绝对路径）—— **优先**。
         它是打包时确定的正确位置，比 __file__ 可靠：插件从 addons 目录加载时
         __file__ 指向 addons，而依赖可能部署在工程目录，两者不一定同处。
      2) __file__（没有注入常量时的回退）
      3) 用户 addons 下的插件目录
      4) 当前工作目录
    """
    home = globals().get("FACECAP_HOME", "")
    if home and os.path.isdir(home):
        return home
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        pass
    try:
        addon = os.path.join(bpy.utils.user_resource("SCRIPTS"),
                             "addons", "ez_v2b_animation")
        if os.path.isdir(addon):
            return addon
    except Exception:
        pass
    try:
        return os.getcwd()
    except Exception:
        return "."


def _facecap_deps_candidates():
    """依赖目录的全部候选，按优先级排列。
    逐个探测哪个真实存在（含模型），而不是猜一个就返回。"""
    cands = []

    def add(p):
        if p and p not in cands:
            cands.append(p)

    # 1) 面板里手动指定
    try:
        s = getattr(bpy.context.scene, "fc_deps", "")
        if s:
            add(s)
    except Exception:
        pass
    # 2) 各 home 候选下的 _blpy_nodeps
    homes = []
    h = globals().get("FACECAP_HOME", "")
    if h:
        homes.append(h)
    try:
        homes.append(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        pass
    try:
        homes.append(os.path.join(bpy.utils.user_resource("SCRIPTS"),
                                  "addons", "ez_v2b_animation"))
    except Exception:
        pass
    try:
        homes.append(os.getcwd())
    except Exception:
        pass
    for home in homes:
        add(os.path.join(home, FACECAP_DEPS_DEFAULT))
        # 工程开发目录：<工程>/_face_work/_blpy_nodeps
        add(os.path.join(os.path.dirname(home), "_face_work",
                         FACECAP_DEPS_DEFAULT))
    # 3) 本文件所在目录本身（万一依赖直接摊在同一层）
    try:
        add(os.path.dirname(os.path.abspath(__file__)))
    except NameError:
        pass
    return cands


def _facecap_deps_dir():
    """依赖目录：返回第一个**真实存在**的候选；都不存在时返回首选路径（供报错显示）。"""
    cands = _facecap_deps_candidates()
    for p in cands:
        if os.path.isdir(p) and _facecap_looks_like_deps(p):
            return p
    for p in cands:
        if os.path.isdir(p):
            return p
    return cands[0] if cands else FACECAP_DEPS_DEFAULT


def _facecap_looks_like_deps(path):
    """判断一个目录是否真的像依赖目录：至少有 cv2 或 mediapipe。"""
    try:
        for n in os.listdir(path):
            if n in ("cv2", "mediapipe") or n.startswith("cv2."):
                return True
    except Exception:
        pass
    return False


def _facecap_model_path():
    """模型文件路径。按候选顺序找第一个真实存在的；都没有时返回首选（供报错显示）。"""
    try:
        s = getattr(bpy.context.scene, "fc_model", "")
        if s and os.path.isfile(s):
            return s
    except Exception:
        pass
    first = ""
    for d in _facecap_deps_candidates():
        p = os.path.join(d, FACECAP_MODEL_NAME)
        if not first:
            first = p
        if os.path.isfile(p):
            return p
    return first or os.path.join(_facecap_deps_dir(), FACECAP_MODEL_NAME)


def _facecap_import():
    """惰性加载 cv2 / mediapipe。返回 (cv2, vision, err)。"""
    if _FACECAP_MOD.get("cv2") is not None:
        return _FACECAP_MOD["cv2"], _FACECAP_MOD["vision"], ""
    deps = _facecap_deps_dir()
    if deps and deps not in sys.path:
        sys.path.insert(0, deps)
    try:
        import cv2
    except Exception as e:
        return None, None, "缺少 opencv（%s）：%s" % (deps, e)
    try:
        import mediapipe as mp
        from mediapipe.tasks import python as _mpp
        from mediapipe.tasks.python import vision
    except Exception as e:
        return None, None, "缺少 mediapipe（%s）：%s" % (deps, e)
    _FACECAP_MOD.update({"cv2": cv2, "vision": vision, "mpp": _mpp, "mp": mp})
    return cv2, vision, ""


# ---------------------------------------------------------------- ARKit 52 键
# 实测顺序（_face_work/arkit_keys.txt）。这是 mediapipe 的输出契约，非配置。
ARKIT_KEYS = [
    "_neutral",
    "browDownLeft", "browDownRight", "browInnerUp",
    "browOuterUpLeft", "browOuterUpRight",
    "cheekPuff", "cheekSquintLeft", "cheekSquintRight",
    "eyeBlinkLeft", "eyeBlinkRight",
    "eyeLookDownLeft", "eyeLookDownRight",
    "eyeLookInLeft", "eyeLookInRight",
    "eyeLookOutLeft", "eyeLookOutRight",
    "eyeLookUpLeft", "eyeLookUpRight",
    "eyeSquintLeft", "eyeSquintRight",
    "eyeWideLeft", "eyeWideRight",
    "jawForward", "jawLeft", "jawOpen", "jawRight",
    "mouthClose",
    "mouthDimpleLeft", "mouthDimpleRight",
    "mouthFrownLeft", "mouthFrownRight",
    "mouthFunnel", "mouthLeft",
    "mouthLowerDownLeft", "mouthLowerDownRight",
    "mouthPressLeft", "mouthPressRight",
    "mouthPucker", "mouthRight",
    "mouthRollLower", "mouthRollUpper",
    "mouthShrugLower", "mouthShrugUpper",
    "mouthSmileLeft", "mouthSmileRight",
    "mouthStretchLeft", "mouthStretchRight",
    "mouthUpperUpLeft", "mouthUpperUpRight",
    "noseSneerLeft", "noseSneerRight",
]

# ---------------------------------------------------------------- ARKit -> 逻辑通道
# (通道名, [(arkit键, 权重)...], 模式)   add=加权求和后clamp  avg=加权平均  max=取最大  min=取最小
#
# 【模式怎么选】看该通道驱动的形态键语义：
#   avg  两侧独立的对称表情（微笑、皱眉）—— 单侧做一半，取平均合理
#   min  "两侧都满足才成立"（闭双眼）—— 必须取最小，否则单侧动作会污染
#   add  多项叠加成一个形变（viseme 合成）
#   max  取最强的一项
FACE_MAP = [
    ("blink_left",   [("eyeBlinkLeft", 1.0)], "add"),
    ("blink_right",  [("eyeBlinkRight", 1.0)], "add"),
    # 【踩过的坑】blink 曾用 avg，导致眨单眼时 blink=0.5 把"闭双眼"形态键
    # 设成半闭，两只眼一起半闭，单眼眨的效果被吃掉（用户报告"不能眨一只眼"）。
    # blink 的语义是"两眼都闭"，必须用 min：
    #   只闭左 → left=1 right=0  blink=0   单眼眨干净
    #   闭双眼 → left=1 right=1  blink=1   正常
    ("blink",        [("eyeBlinkLeft", 1.0), ("eyeBlinkRight", 1.0)], "min"),
    ("lowerlid_left",  [("eyeSquintLeft", 1.0)], "add"),
    ("lowerlid_right", [("eyeSquintRight", 1.0)], "add"),
    ("look_up",    [("eyeLookUpLeft", 0.5), ("eyeLookUpRight", 0.5)], "avg"),
    ("look_down",  [("eyeLookDownLeft", 0.5), ("eyeLookDownRight", 0.5)], "avg"),
    ("look_left",  [("eyeLookOutLeft", 0.5), ("eyeLookInRight", 0.5)], "avg"),
    ("look_right", [("eyeLookInLeft", 0.5), ("eyeLookOutRight", 0.5)], "avg"),
    ("brow_inner_up", [("browInnerUp", 1.0)], "add"),
    ("brow_down",     [("browDownLeft", 0.5), ("browDownRight", 0.5)], "avg"),
    ("brow_outer_up", [("browOuterUpLeft", 0.5), ("browOuterUpRight", 0.5)], "avg"),
    ("cheek_puff",   [("cheekPuff", 1.0)], "add"),
    ("cheek_squint", [("cheekSquintLeft", 0.5), ("cheekSquintRight", 0.5)], "avg"),
    ("mouth_smile",  [("mouthSmileLeft", 0.5), ("mouthSmileRight", 0.5)], "avg"),
    ("mouth_frown",  [("mouthFrownLeft", 0.5), ("mouthFrownRight", 0.5)], "avg"),
    ("mouth_pucker", [("mouthPucker", 1.0)], "add"),
    ("mouth_funnel", [("mouthFunnel", 1.0)], "add"),
    ("mouth_press",  [("mouthPressLeft", 0.5), ("mouthPressRight", 0.5)], "avg"),
    ("mouth_upper_up", [("mouthUpperUpLeft", 0.5), ("mouthUpperUpRight", 0.5)], "avg"),
    ("mouth_lower_down", [("mouthLowerDownLeft", 0.5), ("mouthLowerDownRight", 0.5)], "avg"),
    ("mouth_stretch", [("mouthStretchLeft", 0.5), ("mouthStretchRight", 0.5)], "avg"),
    ("nose_sneer",   [("noseSneerLeft", 0.5), ("noseSneerRight", 0.5)], "avg"),
    ("jaw_open",    [("jawOpen", 1.0)], "add"),
    ("jaw_left",    [("jawLeft", 1.0)], "add"),
    ("jaw_right",   [("jawRight", 1.0)], "add"),
    ("jaw_forward", [("jawForward", 1.0)], "add"),
    ("viseme_aa", [("jawOpen", 0.75), ("mouthLowerDownLeft", 0.15),
                   ("mouthLowerDownRight", 0.15)], "add"),
    ("viseme_ih", [("mouthSmileLeft", 0.35), ("mouthSmileRight", 0.35),
                   ("jawOpen", 0.20), ("mouthStretchLeft", 0.15),
                   ("mouthStretchRight", 0.15)], "add"),
    ("viseme_ou", [("mouthPucker", 0.60), ("mouthFunnel", 0.30),
                   ("jawOpen", 0.10)], "add"),
    ("viseme_e",  [("mouthSmileLeft", 0.30), ("mouthSmileRight", 0.30),
                   ("jawOpen", 0.35), ("mouthStretchLeft", 0.20),
                   ("mouthStretchRight", 0.20)], "add"),
    ("viseme_oh", [("mouthFunnel", 0.55), ("jawOpen", 0.30),
                   ("mouthPucker", 0.15)], "add"),
    ("viseme_nn", [("mouthPressLeft", 0.4), ("mouthPressRight", 0.4),
                   ("mouthClose", 0.3)], "add"),
]

# ---------------------------------------------------------------- 形态键语义候选
# 每个逻辑通道给一组候选键名，按顺序尝试。这是"已知约定"而非"写死"——
# 全部未命中时，_facecap_resolve 会退化为模糊发现（见该函数），不会直接判死。
FACECAP_CANDIDATES = {
    "blink_left":   ["blink_L", "ウィンク", "blink_left",
                     "vrc.blink_left", "eye_blink_1_L", "eye_blink_2_L"],
    "blink_right":  ["blink_R", "ウィンク右", "blink_right",
                     "vrc.blink_right", "eye_blink_1_R", "eye_blink_2_R"],
    "blink":        ["blink", "まばたき", "vrc.blink", "eye_blink_1",
                     "eye_blink_2"],
    "lowerlid_left":  ["vrc.lowerlid_left", "LowerEyelid_up_L", "LowerEyelid_up"],
    "lowerlid_right": ["vrc.lowerlid_right", "LowerEyelid_up_R", "LowerEyelid_up"],
    "look_up":    ["vrc.looking_up", "LookUp", "eye_look_up_1", "上"],
    "look_down":  ["vrc.looking_down", "LookDown", "eye_look_down", "下"],
    "look_left":  ["LookLeft", "eye_look_L", "eye_look_inside"],
    "look_right": ["LookRight", "eye_look_R", "eye_look_outside"],
    "viseme_aa": ["VRC.v_aa", "vrc.v_aa", "あ", "kuchi_A", "mouth_A_1"],
    "viseme_ih": ["VRC.v_ih", "vrc.v_ih", "い", "kuchi_I", "mouth_I_1"],
    "viseme_ou": ["VRC.v_ou", "vrc.v_ou", "う", "kuchi_U", "mouth_U_1"],
    "viseme_e":  ["VRC.v_E", "vrc.v_e", "え", "kuchi_E", "mouth_E_1"],
    "viseme_oh": ["VRC.v_oh", "vrc.v_oh", "お", "kuchi_O", "mouth_O_1"],
    "viseme_nn": ["VRC.v_nn", "vrc.v_nn", "ん", "mouth_n"],
    "mouth_smile": ["口角上げ", "mouth_smile_1", "kuchi_smile_1", "にっこり"],
    "mouth_frown": ["口角下げ", "mouth_angry_1", "kuchi_angry_1"],
    "cheek_squint": ["頬染め", "cheek", "hoppe"],
    "brow_down":   ["eyebrow_angry_1", "mayu_angry_1", "怒り"],
    "brow_inner_up": ["eyebrow_trouble", "mayu_sad_1", "困る"],
    "brow_outer_up": ["eyebrow_up", "mayu_up"],
    "jaw_open":    ["jaw_open", "mouth_△_1", "mouth_o_big"],
    "cheek_puff":  ["cheekPuff", "Option_cheek_puku-", "mouth_puku-"],
    "mouth_pucker": ["mouth_pucker", "kuchi_Λ_1", "mouth_ω"],
    "mouth_funnel": ["mouth_funnel", "kuchi_△_1"],
    "nose_sneer":  ["noseSneer", "nose_up"],
}

# 明确不参与面捕的键前缀（换装开关等），避免误伤
FACECAP_DENY_PREFIX = ("kisekae_",)

# 模糊发现的禁止模式：这些键名出现说明是分区标记或非表情用途
FACECAP_DENY_SUBSTR = ("------", "-----", "====")


# ---------------------------------------------------------------- One Euro 滤波
class _OneEuro:
    """低频平滑、高频跟随。面捕抖动抑制的标准做法。"""

    __slots__ = ("min_cutoff", "beta", "d_cutoff", "x_prev", "dx_prev", "t_prev")

    def __init__(self, min_cutoff=1.0, beta=0.007, d_cutoff=1.0):
        self.min_cutoff = float(min_cutoff)
        self.beta = float(beta)
        self.d_cutoff = float(d_cutoff)
        self.x_prev = None
        self.dx_prev = 0.0
        self.t_prev = None

    @staticmethod
    def _alpha(cutoff, dt):
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def reset(self):
        self.x_prev = None
        self.dx_prev = 0.0
        self.t_prev = None

    def __call__(self, x, t):
        x = float(x)
        if self.x_prev is None or self.t_prev is None:
            self.x_prev = x
            self.dx_prev = 0.0
            self.t_prev = t
            return x
        dt = t - self.t_prev
        if dt <= 0.0:
            dt = 1.0 / 60.0
        dx = (x - self.x_prev) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        dx_hat = a_d * dx + (1.0 - a_d) * self.dx_prev
        cutoff = self.min_cutoff + self.beta * abs(dx_hat)
        a = self._alpha(cutoff, dt)
        x_hat = a * x + (1.0 - a) * self.x_prev
        self.x_prev = x_hat
        self.dx_prev = dx_hat
        self.t_prev = t
        return x_hat


class _FilterBank:
    def __init__(self, min_cutoff=1.0, beta=0.007):
        self.min_cutoff = float(min_cutoff)
        self.beta = float(beta)
        self._f = {}

    def reset(self):
        self._f.clear()

    def apply(self, name, value, t):
        f = self._f.get(name)
        if f is None:
            f = _OneEuro(self.min_cutoff, self.beta)
            self._f[name] = f
        return f(value, t)

    def rebuild(self, min_cutoff, beta):
        if (abs(min_cutoff - self.min_cutoff) > 1e-9
                or abs(beta - self.beta) > 1e-9):
            self.min_cutoff = float(min_cutoff)
            self.beta = float(beta)
            self.reset()


def _facecap_smooth_series(vals, min_cutoff, beta, fps):
    if not vals:
        return []
    f = _OneEuro(min_cutoff, beta)
    dt = 1.0 / max(1e-6, float(fps))
    out = []
    t = 0.0
    for v in vals:
        out.append(f(v, t))
        t += dt
    return out


# ---------------------------------------------------------------- 形态键解析（不写死）
def _facecap_key_pool(arm):
    """该角色全部可用的形态键名（去重，排除换装/分区标记/空键）

    【为什么排除空键】实测（_facework/wink_geom.txt）：Karin/Rusk 的
    `vrc.blink_left` / `vrc.blink_right` 是**空键** —— VRChat 标准命名占了位，
    但位移顶点数为 0，设成 1.0 也不产生任何形变。候选键列表里若它们排在前面，
    单眼眨就会静默失效（用户报告"眨单眼两只眼都不动"）。
    所以这里把空键直接剔出键池，让解析落到真正有形变的键上。"""
    pool = []
    seen = set()
    for o in _face_meshes(arm):
        sk = o.data.shape_keys
        if sk is None:
            continue
        basis = sk.key_blocks.get("Basis")
        basis_co = None
        if basis is not None:
            try:
                import numpy as _np
                n = len(o.data.vertices)
                arr = _np.empty(n * 3, dtype=_np.float32)
                basis.data.foreach_get("co", arr)
                basis_co = arr.reshape(n, 3)
            except Exception:
                basis_co = None
        for kb in sk.key_blocks:
            n = kb.name
            if n == "Basis" or n in seen:
                continue
            if any(n.startswith(p) for p in FACECAP_DENY_PREFIX):
                continue
            if any(s in n for s in FACECAP_DENY_SUBSTR):
                continue
            # 空键检测：位移顶点数为 0 就跳过
            if basis_co is not None and _facecap_key_is_empty(kb, basis_co):
                continue
            seen.add(n)
            pool.append(n)
    return pool


def _facecap_key_is_empty(kb, basis_co, ref_scale=None):
    """形态键是否「实质无效」。

    【判据不能只看绝对位移】实测（_facework/empty_check.txt）：
      Karin  vrc.blink_left   最大位移 0.000075   ← 非零，但比有效键小 380 倍
      Rusk   vrc.blink_left   最大位移 0.007805   ← 比有效键小 257 倍
    这两个键在渲染上等于不可见，却是 VRChat 标准命名，候选列表里排前面
    就会把单眼眨吃掉（用户报告"眨单眼两只眼都不动"）。
    所以用**相对量级**判定：位移小于参考尺度的 1e-3 即视为无效。
    参考尺度取模型自身包围盒对角线，这样与角色大小无关
    （Karin 顶点 X 范围 0.08，Rusk 是 8.04，差 100 倍）。
    """
    try:
        import numpy as _np
        n = basis_co.shape[0]
        arr = _np.empty(n * 3, dtype=_np.float32)
        kb.data.foreach_get("co", arr)
        arr = arr.reshape(n, 3)
        d = float(_np.abs(arr - basis_co).max())
        if ref_scale is None:
            ext = basis_co.max(axis=0) - basis_co.min(axis=0)
            ref_scale = float(_np.linalg.norm(ext))
        if ref_scale <= 0.0:
            return d < 1e-6
        return bool(d < ref_scale * 1e-3)
    except Exception:
        # 读不出来就当非空（宁可留着也不要误删有效键）
        return False


def _facecap_fuzzy(pool, channel):
    """模糊发现：候选名全未命中时的兜底。
    按通道关键词在键池里找包含关系，取最短的（最短 = 最通用，避免命中 _L/_R 细分）。"""
    kw = {
        "blink": ("blink", "eye_blink", "まばたき"),
        "blink_left": ("blink", "ウィンク"),
        "blink_right": ("blink", "ウィンク"),
        "look_up": ("look_up", "looking_up", "LookUp"),
        "look_down": ("look_down", "looking_down", "LookDown"),
        "viseme_aa": ("v_aa", "kuchi_a", "mouth_a", "あ"),
        "viseme_ih": ("v_ih", "kuchi_i", "mouth_i", "い"),
        "viseme_ou": ("v_ou", "kuchi_u", "mouth_u", "う"),
        "viseme_e": ("v_e", "kuchi_e", "mouth_e", "え"),
        "viseme_oh": ("v_oh", "kuchi_o", "mouth_o", "お"),
        "viseme_nn": ("v_nn", "mouth_n", "ん"),
    }.get(channel)
    if not kw:
        return None
    hits = []
    for n in pool:
        low = n.lower()
        for k in kw:
            if k.lower() in low:
                hits.append(n)
                break
    if not hits:
        return None
    hits.sort(key=lambda s: (len(s), s))
    return hits[0]


def _facecap_resolve(arm, channel, pool=None):
    """逻辑通道 -> 实际键名。候选名优先，未命中走模糊发现。"""
    for n in FACECAP_CANDIDATES.get(channel, []):
        if _face_has(arm, n):
            return n
    if pool is None:
        pool = _facecap_key_pool(arm)
    return _facecap_fuzzy(pool, channel)


def _facecap_channels(arm):
    """该角色可用的全部逻辑通道 -> 实际键名"""
    pool = _facecap_key_pool(arm)
    out = {}
    for ch, _terms, _mode in FACE_MAP:
        k = _facecap_resolve(arm, ch, pool)
        if k:
            out[ch] = k
    return out


def _facecap_compute(bs):
    """ARKit blendshape dict -> 逻辑通道值 dict"""
    out = {}
    for ch, terms, mode in FACE_MAP:
        vals = []
        for ak, w in terms:
            v = bs.get(ak)
            if v is None:
                continue
            vals.append((float(v), float(w)))
        if not vals:
            continue
        if mode == "add":
            s = sum(v * w for v, w in vals)
            s = 0.0 if s < 0.0 else (1.0 if s > 1.0 else s)
        elif mode == "avg":
            tw = sum(w for _v, w in vals)
            s = (sum(v * w for v, w in vals) / tw) if tw > 0.0 else 0.0
        elif mode == "max":
            s = max(v for v, _w in vals)
        elif mode == "min":
            # "两侧都满足才成立"（如闭双眼）：取最小，避免单侧动作污染
            s = min(v for v, _w in vals)
        else:
            s = 0.0
        out[ch] = s

    # ---- 眨眼去叠加 ----
    # 【踩过的坑】blink（闭双眼键，本身已含两只眼的形变）与 blink_left/right
    # （单眼键）同时驱动时，同一只眼被形变两次 —— Blender 形态键是叠加的，
    # 闭双眼时眼皮过度形变错位（用户实测截图：双眼闭合出现难看褶皱）。
    # 标准解法是「差值驱动」：
    #   both = min(左, 右)      → 归 blink（双眼键）
    #   单眼 = 自身 - both      → 只驱动"单眼独有"的差值
    # 验证（每只眼总驱动量 = blink + 单眼键）：
    #   只闭左眼  both=0  blink_L=1  blink_R=0  → 左眼总量 1
    #   闭双眼    both=1  blink_L=0  blink_R=0  → 每只眼只剩 blink 一次形变
    #   左0.5右0  both=0  blink_L=0.5           → 左眼 0.5
    if "blink" in out and ("blink_left" in out or "blink_right" in out):
        both = out["blink"]
        if "blink_left" in out:
            out["blink_left"] = max(0.0, out["blink_left"] - both)
        if "blink_right" in out:
            out["blink_right"] = max(0.0, out["blink_right"] - both)
    return out


# ---------------------------------------------------------------- 头部结构发现（不写死）
def _facecap_bone_depth(arm, name):
    b = arm.data.bones.get(name)
    d = 0
    while b is not None and b.parent is not None:
        d += 1
        b = b.parent
    return d


def _facecap_discover_head(arm):
    """靠结构关系找出「头部被驱动骨骼」与「头部手柄」，不写死任何骨骼名。
    规则 1：扫全部骨骼的 TRACK_TO 约束，被挂约束的 = 被驱动骨骼，
            constraint.subtarget = 手柄。多条候选时用手柄深度浅 + 无约束消歧。
    规则 2：无 TRACK_TO 时，退化为 rig_profile["handles"] 里名字含 head/hik 的。
    规则 3：再退化为离被驱动骨骼最近的无约束骨骼。
    返回 {"driven":名, "handle":名, "method":说明} 或 None。"""
    cands = []
    for b in arm.data.bones:
        pb = arm.pose.bones.get(b.name)
        if pb is None:
            continue
        for c in pb.constraints:
            if c.type != "TRACK_TO":
                continue
            if c.target != arm or not c.subtarget:
                continue
            if arm.data.bones.get(c.subtarget) is None:
                continue
            cands.append({
                "driven": b.name,
                "handle": c.subtarget,
                "influence": c.influence,
                "mute": c.mute,
                "hdepth": _facecap_bone_depth(arm, c.subtarget),
                "hcons": len(arm.pose.bones[c.subtarget].constraints),
            })
    if cands:
        def score(c):
            s = 0.0
            if c["hcons"] == 0:
                s -= 100.0          # 无约束 = 真控制器
            s += c["hdepth"] * 10.0
            if c["mute"]:
                s += 1000.0
            s += (1.0 - c["influence"]) * 100.0
            return s
        best = sorted(cands, key=score)[0]
        return {"driven": best["driven"], "handle": best["handle"],
                "method": "TRACK_TO 结构发现"}

    # 规则 2：profile 里的 handles
    handles = []
    raw = arm.get("rig_profile")
    if raw:
        try:
            handles = list(json.loads(raw).get("handles") or [])
        except Exception:
            handles = []
    driven = None
    for n in ("Head", "head", "HEAD"):
        if arm.data.bones.get(n):
            driven = n
            break
    if handles and driven:
        for key in ("head", "hik"):
            for h in handles:
                if key in h.lower():
                    return {"driven": driven, "handle": h,
                            "method": "profile.handles(%s)" % key}

    # 规则 3：几何最近的无约束骨骼
    if driven:
        hp = (arm.matrix_world @ arm.pose.bones[driven].matrix).to_translation()
        best = None
        for b in arm.data.bones:
            pb = arm.pose.bones.get(b.name)
            if pb is None or pb.constraints or b.name == driven:
                continue
            p = (arm.matrix_world @ pb.matrix).to_translation()
            d = (p - hp).length
            if d < 1e-6:
                continue
            if best is None or d < best[0]:
                best = (d, b.name)
        if best:
            return {"driven": driven, "handle": best[1],
                    "method": "几何最近(兜底)"}
    return None


def _facecap_calib_head(arm):
    """运行时标定：映射矩阵 M（basis->world）与 D（被驱动骨骼到手柄距离）。
    绝不硬编码 —— 实测三台 M 与 D 都不同，且改了 rig 就会变。"""
    info = _facecap_discover_head(arm)
    if info is None:
        return None
    hpb = arm.pose.bones.get(info["handle"])
    driven = arm.pose.bones.get(info["driven"])
    if hpb is None or driven is None:
        return None

    _upd()
    orig = hpb.matrix_basis.copy()

    def hpos():
        _upd()
        return (arm.matrix_world @ hpb.matrix).to_translation()

    eps = 0.01
    cols = []
    ok = True
    try:
        for i in range(3):
            # 每轮必须先复位再读基准 —— 否则 p0 会读到上一轮的残留状态，
            # 使 M 出现非对角污染（实测：位移被放大 sqrt(3)，头部转角超标 3 倍）
            hpb.matrix_basis = orig.copy()
            _upd()
            p0 = hpos()
            v = Vector((0.0, 0.0, 0.0))
            v[i] = eps
            hpb.matrix_basis = Matrix.Translation(v) @ orig
            p1 = hpos()
            d = (p1 - p0) / eps
            if d.length < 1e-9:
                ok = False
                break
            cols.append(d)
    finally:
        hpb.matrix_basis = orig
        _upd()

    if not ok or len(cols) != 3:
        return None

    M = Matrix((
        (cols[0].x, cols[1].x, cols[2].x),
        (cols[0].y, cols[1].y, cols[2].y),
        (cols[0].z, cols[1].z, cols[2].z),
    ))
    if abs(M.determinant()) < 1e-12:
        return None

    # 健全性检查：M 的列应近似正交（每列是单位 basis 位移产生的世界位移）。
    # 若出现显著非对角项，说明标定被污染 —— 宁可失败也不要给出错误映射。
    for a in range(3):
        for b in range(a + 1, 3):
            na = cols[a].length
            nb = cols[b].length
            if na < 1e-9 or nb < 1e-9:
                return None
            cosab = cols[a].dot(cols[b]) / (na * nb)
            if abs(cosab) > 0.05:
                return None

    _upd()
    dr_w = (arm.matrix_world @ driven.matrix).to_translation()
    hk_w = (arm.matrix_world @ hpb.matrix).to_translation()
    D = (hk_w - dr_w).length
    if D < 1e-9:
        return None

    return {"M": M, "Minv": M.inverted(), "D": D,
            "handle": info["handle"], "driven": info["driven"],
            "method": info["method"], "base": orig.copy()}


def _facecap_head_apply(arm, calib, yaw_deg, pitch_deg, strength, max_deg,
                        roll_deg=0.0, roll_strength=1.0, roll_max=45.0):
    """把手柄移到「头部朝向目标角」的位置，并把 roll 写到手柄的局部 Z 旋转上。

    【pitch/yaw】手柄是同一个点，沿 X 移和沿 Z 移会合成成单一夹角，
    而 TRACK_TO 量的是总夹角。所以必须先合成锥角，再沿方向移动：

        total = acos(cos(yaw) * cos(pitch))        # 球面合成角
        dir   = normalize(tan(yaw), 0, tan(pitch)) # 世界方向
        offset = D * tan(total) * dir

    实测证据（verify_facecap.txt 首轮）：若把 yaw/pitch 当独立位移直接相加，
    偏航单轴精确（dz=0 时总角即偏航角），但俯仰严重超标
    —— Karin 请求俯仰 30° 实测 65.52°，偏差 +35.52°。
    改成球面合成后误差回到 1e-4 量级。

    【roll】TRACK_TO 只读位置，表达不了绕视线轴的旋转。
    靠 Head 上那条 COPY_ROTATION(mix_mode=ADD, 只开 Z) 约束从手柄自身
    旋转里取 —— 所以这里把手柄绕局部 Z 转 roll 度即可。
    实测（_facework/roll_probe.txt）：手柄转 +30° → Head roll +30.000°，
    且 pitch/yaw 精度不受影响（误差 1e-3 量级）。
    约束未安装时（_facecap_ensure_roll 未调用）roll 会被忽略，pitch/yaw 照常。
    """
    if calib is None:
        return False
    hpb = arm.pose.bones.get(calib["handle"])
    if hpb is None:
        return False

    yaw = yaw_deg * strength
    pitch = pitch_deg * strength
    lim = abs(max_deg)
    if lim > 0.0:
        yaw = -lim if yaw < -lim else (lim if yaw > lim else yaw)
        pitch = -lim if pitch < -lim else (lim if pitch > lim else pitch)

    # tan 在 90° 发散，限幅 85°
    yaw = -85.0 if yaw < -85.0 else (85.0 if yaw > 85.0 else yaw)
    pitch = -85.0 if pitch < -85.0 else (85.0 if pitch > 85.0 else pitch)

    # roll 限幅
    roll = roll_deg * roll_strength
    rl = abs(roll_max)
    if rl > 0.0:
        roll = -rl if roll < -rl else (rl if roll > rl else roll)

    D = calib["D"]
    ry = math.radians(yaw)
    rp = math.radians(pitch)

    # 球面合成：两个正交旋转的合角
    cos_total = math.cos(ry) * math.cos(rp)
    cos_total = -1.0 if cos_total < -1.0 else (1.0 if cos_total > 1.0 else cos_total)
    total = math.acos(cos_total)
    # 合角限幅到 85°，避免 tan 发散
    cap = math.radians(85.0)
    if total > cap:
        total = cap

    ty = math.tan(ry)
    tp = math.tan(rp)
    mag = math.sqrt(ty * ty + tp * tp)
    if mag < 1e-12:
        off = Vector((0.0, 0.0, 0.0))
    else:
        scale = D * math.tan(total) / mag
        off = Vector((ty * scale, 0.0, tp * scale))

    bl = calib["Minv"] @ off
    m = Matrix.Translation(bl) @ calib["base"]
    # 局部 Z 旋转 → 经 COPY_ROTATION(ADD, 只开Z) 传给 Head 成为歪头
    if abs(roll) > 1e-9:
        m = m @ Matrix.Rotation(math.radians(roll), 4, 'Z')
    hpb.matrix_basis = m
    return True


def _facecap_head_release(arm, calib):
    """松开面捕控制，恢复手柄原状"""
    if calib is None:
        return
    hpb = arm.pose.bones.get(calib["handle"])
    if hpb is not None:
        hpb.matrix_basis = calib["base"].copy()


# ---------------------------------------------------------------- 歪头（roll）约束
# TRACK_TO 只读目标「位置」，所以原生只能表达 pitch/yaw 两个自由度。
# 加 roll 的四条路线实测（_facework/roll_probe.txt）：
#   A 直接写 Head.matrix_basis          → 被 TRACK_TO 完全覆盖，roll 0.000° 无效
#   B use_target_z=True                → 破坏整个约束，基线变 180°，不可用
#   C Head 上加 COPY_ROTATION(ADD,只开Z) ← 三台全部有效，且不破坏 pitch/yaw
#   D TRANSFORM                        → 需额外属性+驱动，链路过长
# 采用 C。静置零变化（roll 0.000°）；手柄绕局部 Z 转多少，头就歪多少。
FC_ROLL_CNAME = "面捕歪头"


def _facecap_discover_head_roll(arm):
    """找头部被驱动骨骼与手柄（复用结构发现）"""
    info = _facecap_discover_head(arm)
    if info is None:
        return None, None
    return info["driven"], info["handle"]


def _facecap_roll_ready(arm):
    """该骨架是否已装好歪头约束"""
    driven, _h = _facecap_discover_head_roll(arm)
    if driven is None:
        return False
    pb = arm.pose.bones.get(driven)
    if pb is None:
        return False
    for c in pb.constraints:
        if c.name == FC_ROLL_CNAME and c.type == "COPY_ROTATION":
            return True
    return False


def _facecap_ensure_roll(arm, install=True):
    """幂等安装/移除歪头约束。返回 (ok, msg)。

    约束参数（实测有效的那一组，改动前先看 roll_probe.txt）：
        target      = 骨架本体
        subtarget   = 头部手柄（结构发现得到，不写死）
        target_space / owner_space = LOCAL
        mix_mode    = ADD      ← 叠加在 TRACK_TO 之后，不覆盖 pitch/yaw
        use_z       = True     ← 只取绕手柄长轴的分量
        influence   = 1.0
    """
    driven, handle = _facecap_discover_head_roll(arm)
    if driven is None or handle is None:
        return False, "未发现头部结构"
    pb = arm.pose.bones.get(driven)
    if pb is None:
        return False, "未找到被驱动骨骼"

    existing = None
    for c in pb.constraints:
        if c.name == FC_ROLL_CNAME:
            existing = c
            break

    if not install:
        if existing is not None:
            try:
                pb.constraints.remove(existing)
                _upd()
                return True, "已移除歪头约束"
            except Exception as e:
                return False, "移除失败: %s" % e
        return True, "本来就没有歪头约束"

    if existing is not None:
        # 已存在：校正参数（防止旧版本参数不对）
        changed = False
        want = {"target_space": "LOCAL", "owner_space": "LOCAL",
                "mix_mode": "ADD", "use_x": False, "use_y": False,
                "use_z": True}
        for k, v in want.items():
            try:
                if getattr(existing, k) != v:
                    setattr(existing, k, v)
                    changed = True
            except Exception:
                pass
        try:
            existing.target = arm
            existing.subtarget = handle
        except Exception:
            pass
        _upd()
        return True, ("已校正歪头约束参数" if changed else "歪头约束已存在")

    try:
        con = pb.constraints.new("COPY_ROTATION")
        con.name = FC_ROLL_CNAME
        con.target = arm
        con.subtarget = handle
        con.target_space = "LOCAL"
        con.owner_space = "LOCAL"
        con.mix_mode = "ADD"
        con.use_x = False
        con.use_y = False
        con.use_z = True
        con.influence = 1.0
        _upd()
        return True, "已安装歪头约束（%s ← %s）" % (driven, handle)
    except Exception as e:
        return False, "安装失败: %s" % e


def _facecap_roll_state(arm):
    """返回 (是否已装, 说明)"""
    driven, _h = _facecap_discover_head_roll(arm)
    if driven is None:
        return False, "未发现头部结构"
    pb = arm.pose.bones.get(driven)
    if pb is None:
        return False, "未找到被驱动骨骼"
    for c in pb.constraints:
        if c.name == FC_ROLL_CNAME:
            return True, "influence %.2f  mix %s  仅Z轴" % (
                c.influence, c.mix_mode)
    return False, "未安装"


def _facecap_matrix_rot(m4):
    """mediapipe 4x4 面部矩阵 -> (pitch, yaw, roll) 度。

    【坐标系】mediapipe 面部矩阵：X 右、Y 上、Z 朝观察者（右手系）。
    pitch 取 euler X，yaw 取 euler Y，roll 取 euler Z。

    【实测符号约定】_facework/pitch_dir.txt 验证过驱动侧：
      pitch > 0 → 头部 Z 轴朝上 → 抬头（正确）
    而 mediapipe 的 euler X 在"抬头"时给出的是**负值**，
    所以调用方要用 fc_head_pitch_sign = -1 抵消。
    这个默认值写在 Scene 属性里，面板上有开关可随时反向。"""
    try:
        m3 = Matrix(((float(m4[0][0]), float(m4[0][1]), float(m4[0][2])),
                     (float(m4[1][0]), float(m4[1][1]), float(m4[1][2])),
                     (float(m4[2][0]), float(m4[2][1]), float(m4[2][2]))))
        e = m3.normalized().to_euler("XYZ")
        return (math.degrees(e.x), math.degrees(e.y), math.degrees(e.z))
    except Exception:
        return (0.0, 0.0, 0.0)


# ---------------------------------------------------------------- 追踪线程
class _FaceTracker(threading.Thread):
    """后台追踪线程：抓帧 -> 推理 -> 投递结果。线程内绝不触碰 bpy。"""

    def __init__(self, cam_index, model_path, out_queue, stop_event, want_head):
        super().__init__(daemon=True)
        self.cam_index = int(cam_index)
        self.model_path = model_path
        self.q = out_queue
        self.stop_ev = stop_event
        self.want_head = bool(want_head)
        self.error = ""
        self.fps = 0.0

    def run(self):
        cv2, vision, err = _facecap_import()
        if err:
            self.error = err
            return
        if not os.path.isfile(self.model_path):
            self.error = "缺少模型文件 face_landmarker.task：%s" % self.model_path
            return
        cap = None
        try:
            # MSMF 是实测唯一可用后端（DSHOW 报 backend can't be used by index）
            cap = cv2.VideoCapture(self.cam_index, cv2.CAP_MSMF)
            if not cap.isOpened():
                cap.release()
                cap = cv2.VideoCapture(self.cam_index)
            if not cap.isOpened():
                self.error = "无法打开摄像头 %d" % self.cam_index
                return
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

            opts = vision.FaceLandmarkerOptions(
                base_options=_FACECAP_MOD["mpp"].BaseOptions(
                    model_asset_path=self.model_path),
                running_mode=vision.RunningMode.VIDEO,
                num_faces=1,
                output_face_blendshapes=True,
                output_facial_transformation_matrixes=self.want_head,
            )
            lm = vision.FaceLandmarker.create_from_options(opts)

            t0 = time.monotonic()
            last = t0
            prev_ts = 0
            n = 0
            while not self.stop_ev.is_set():
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.005)
                    continue
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mpimg = _FACECAP_MOD["mp"].Image(
                    image_format=_FACECAP_MOD["mp"].ImageFormat.SRGB, data=rgb)
                ts = int((time.monotonic() - t0) * 1000.0)
                # mediapipe VIDEO 模式硬要求时间戳严格递增（实测：相同或
                # 倒退都抛 ValueError: Input timestamp must be monotonically
                # increasing）。int(ms) 截断在抖动帧下会产生相同值，原实现
                # 一抛就 break 退出线程 → 表现为"数帧后卡住"。
                if ts <= prev_ts:
                    ts = prev_ts + 1
                prev_ts = ts
                try:
                    res = lm.detect_for_video(mpimg, ts)
                except ValueError:
                    continue          # 时间戳类问题跳过该帧，不杀线程
                except Exception as e:
                    self.error = "推理失败：%s: %s" % (type(e).__name__, e)
                    break

                if res.face_blendshapes:
                    bs = {}
                    for c in res.face_blendshapes[0]:
                        bs[c.category_name] = float(c.score)
                    rot = None
                    if self.want_head and res.facial_transformation_matrixes:
                        rot = _facecap_matrix_rot(
                            res.facial_transformation_matrixes[0])
                    payload = {"bs": bs, "rot": rot,
                               "t": time.monotonic() - t0}
                    try:
                        while self.q.qsize() > 2:
                            self.q.get_nowait()
                    except Exception:
                        pass
                    try:
                        self.q.put_nowait(payload)
                    except Exception:
                        pass
                    n += 1

                now = time.monotonic()
                if now - last >= 1.0:
                    self.fps = n / (now - last)
                    n = 0
                    last = now
        except Exception as e:
            self.error = "追踪线程异常：%s" % e
        finally:
            try:
                if cap is not None:
                    cap.release()
            except Exception:
                pass


# ---------------------------------------------------------------- 运行状态
_FC = {
    "thread": None, "stop": None, "queue": None, "filters": None,
    "calib": {}, "running": False, "recording": False, "rig": "",
    "buffer": [], "last": {}, "err": "", "fps": 0.0, "rot": (0.0, 0.0, 0.0),
    "timer": None, "frames_in": 0, "channels": {},
    "preview_on": False, "preview_timer": None,
    "preview_win": None, "preview_embedded_area": None,
    "last_frame": None, "last_lms": None,
    "cap": None, "cap_state": "idle", "cap_backend": None,
    "cam_index": 0, "cap_frames": 0, "read_fail": 0,
    # 表情校准
    "calib_state": "idle", "calib_buf": [], "calib_t0": 0.0,
    "calib_msg": "", "calib_expr": {}, "calib_stage_no": 0,
}


def _fc_scene():
    try:
        return bpy.context.scene
    except Exception:
        return None


def _facecap_running():
    return bool(_FC["running"])


def _fc_wink_boost(raw, gain):
    """单眼眨增益：放大"单眼独有"分量，不动双眼闭。

    mediapipe 对「单侧眼睑闭合」的输出幅度天然低于双眼闭（实测常见 0.6~0.8，
    而双眼闭能给 0.95），加上校准归一化再压缩一次，表现为"单眼眨闭不紧"。

    【公式要点·踩过两次】
    1) 传入的 raw[side] **已经是差值** —— _facecap_compute 末尾的差值驱动
       已算过 max(0, 自身 - both)。这里不能再减一次 both，
       否则差值被减两遍、增益形同无效（实测：输入 0.6 增益 1.5 后仍 0.6）。
    2) 结果钳到 [0,1]。差值本身非负，不会产生负值
       （曾写成 both + (v - both) * gain，浮点误差被放大成 -0.425）。
    语义：每只眼总驱动量 = blink + 单眼键 = both + 差值 × 增益。

    单独成函数是为了能被验证脚本直接调用 —— 原先是内联在 tick 里的，
    公式写错时无法单独测出。"""
    if gain <= 1.0 or "blink" not in raw:
        return raw
    out = dict(raw)
    for side in ("blink_left", "blink_right"):
        if side in out:
            d = out[side]              # 已是"单眼独有"分量
            out[side] = min(1.0, d * gain) if d > 0.0 else 0.0
    return out


def _facecap_start(rig_name, cam_index, want_head):
    """启动追踪（幂等）。返回 (ok, msg)。
    摄像头 ~8s 枚举在后台线程进行，本函数立即返回。"""
    if _FC["running"]:
        return True, "已在运行"
    cfg, arm = _rig(rig_name)
    if arm is None:
        return False, "未找到角色骨架"
    if not _face_meshes(arm):
        return False, "该角色没有形态键网格"
    channels = _facecap_channels(arm)
    if not channels:
        return False, "该角色没有可映射的表情键"

    cv2, vision, err = _facecap_import()
    if err:
        return False, err
    model = _facecap_model_path()
    if not os.path.isfile(model):
        return False, "缺少模型文件：%s" % model

    calib = None
    if want_head:
        calib = _facecap_calib_head(arm)
        if calib is None:
            want_head = False

    q = queue.Queue()
    stop_ev = threading.Event()
    # 推理线程（不含摄像头 I/O —— 摄像头在主线程 _fc_capture_tick，
    # 因为 MSMF 在 worker 线程里读不出帧，实测）
    th = _FaceTrackerPreview(cam_index, model, q, stop_ev, want_head)
    th.start()

    _FC.update({
        "thread": th, "stop": stop_ev, "queue": q,
        "filters": _FilterBank(), "running": True, "rig": rig_name,
        "err": "", "frames_in": 0, "rot": (0.0, 0.0, 0.0),
        "channels": channels,
        "cam_index": int(cam_index), "cap_state": "idle",
        "cap_frames": 0, "read_fail": 0, "last_lms": None,
    })
    _FC["calib"][rig_name] = calib
    _facecap_ensure_timer()
    _facecap_preview_ensure_timer()
    _fc_capture_ensure_timer()          # 主线程抓帧
    hm = calib["method"] if calib else "关"
    return True, "已启动（摄像头准备中，约 8 秒；头部：%s）" % (
        "开 / " + hm if want_head else "关")


def _facecap_stop():
    """停止追踪并释放头部控制"""
    _FC["running"] = False
    _FC["recording"] = False
    ev = _FC.get("stop")
    if ev is not None:
        ev.set()
    th = _FC.get("thread")
    if th is not None:
        try:
            th.join(timeout=2.0)
        except Exception:
            pass
    _FC["thread"] = None
    _FC["queue"] = None
    _FC["stop"] = None

    for rig_name, calib in list(_FC["calib"].items()):
        if calib is None:
            continue
        cfg, arm = _rig(rig_name)
        if arm is not None:
            try:
                _facecap_head_release(arm, calib)
            except Exception:
                pass
    _FC["calib"] = {}
    _FC["channels"] = {}
    _facecap_kill_timer()
    _facecap_preview_kill_timer()
    _fc_capture_kill_timer()
    _fc_release_camera()


def _facecap_ensure_timer():
    # bpy.app.timers.register 返回 None，用 is_registered 按函数本体查询
    if not bpy.app.timers.is_registered(_facecap_tick):
        bpy.app.timers.register(_facecap_tick,
                                first_interval=0.0, persistent=True)
    _FC["timer"] = True


def _facecap_kill_timer():
    if bpy.app.timers.is_registered(_facecap_tick):
        try:
            bpy.app.timers.unregister(_facecap_tick)
        except Exception:
            pass
    _FC["timer"] = None


# ---------------------------------------------------------------- 主线程 tick
def _facecap_tick():
    """主线程：取队列 -> 滤波 -> 写形态键 / 写缓冲。返回下次间隔（秒）。"""
    if not _FC["running"]:
        _FC["timer"] = None
        return None

    th = _FC.get("thread")
    if th is not None and th.error and not _FC["err"]:
        _FC["err"] = th.error

    sc = _fc_scene()
    if sc is None:
        return 1.0 / 60.0

    q = _FC.get("queue")
    payload = None
    if q is not None:
        try:
            while True:
                payload = q.get_nowait()
        except Exception:
            pass

    # 预览快照：帧由主线程抓帧 timer 提供，这里补上 landmarks
    # （两个 timer 各司其职，不再争抢同一个队列）
    if payload is not None:
        _FC["last_lms"] = payload.get("lms")
        snap = _FC.get("last_frame")
        if snap is not None:
            snap["lms"] = payload.get("lms")

    if payload is None:
        if th is not None and not th.is_alive() and not _FC["err"]:
            _FC["err"] = "推理线程已退出"
        return 1.0 / 60.0

    _FC["frames_in"] += 1
    if th is not None:
        _FC["fps"] = th.fps

    rig_name = _FC["rig"]
    cfg, arm = _rig(rig_name)
    if arm is None:
        return 1.0 / 60.0

    fb = _FC["filters"]
    if fb is None:
        fb = _FilterBank()
        _FC["filters"] = fb
    fb.rebuild(getattr(sc, "fc_smooth", 1.0), getattr(sc, "fc_beta", 0.007))

    t = float(payload.get("t", 0.0))
    raw = _facecap_compute(payload.get("bs") or {})

    # 校准采样（用未去偏置的原始值）
    rot_raw = payload.get("rot")
    if _FC.get("calib_state", "idle") != "idle":
        _fc_calib_tick(raw, rot_raw)

    # 去偏置 + 眼睛幅度归一化
    raw = _fc_calib_apply(raw)

    # 单眼眨增益（放大单眼独有分量，不动双眼闭）
    raw = _fc_wink_boost(raw, float(getattr(sc, "fc_wink_gain", 1.5)))

    strength = float(getattr(sc, "fc_strength", 1.0))
    vals = {}
    for ch, v in raw.items():
        v = v * strength
        v = 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)
        vals[ch] = fb.apply(ch, v, t)
    _FC["last"] = vals

    keys = _FC["channels"] or _facecap_channels(arm)

    if getattr(sc, "fc_live", True):
        for ch, v in vals.items():
            k = keys.get(ch)
            if k:
                _face_set(arm, k, v)

    rot = payload.get("rot")
    if rot is not None:
        _FC["rot"] = rot
        if getattr(sc, "fc_head", True):
            calib = _FC["calib"].get(rig_name)
            if calib is not None:
                # 实测：手柄沿世界 X -> 偏航；沿世界 Z -> 俯仰
                hz = _fc_calib_head_zero()
                if hz is not None:
                    pitch_r, yaw_r, roll_r = (rot[0] - hz[0],
                                              rot[1] - hz[1],
                                              rot[2] - hz[2])
                else:
                    pitch_r, yaw_r, roll_r = rot[0], rot[1], rot[2]
                yaw = yaw_r * float(getattr(sc, "fc_head_yaw_sign", 1.0))
                pitch = pitch_r * float(getattr(sc, "fc_head_pitch_sign", -1.0))
                # roll 仅在装了歪头约束时生效（未装则内部自动忽略）
                roll = 0.0
                if bool(getattr(sc, "fc_head_roll", True)) and \
                        _facecap_roll_ready(arm):
                    roll = roll_r * float(
                        getattr(sc, "fc_head_roll_sign", -1.0))
                try:
                    _facecap_head_apply(
                        arm, calib, yaw, pitch,
                        float(getattr(sc, "fc_head_strength", 1.0)),
                        float(getattr(sc, "fc_head_max", 45.0)),
                        roll_deg=roll,
                        roll_strength=float(
                            getattr(sc, "fc_head_roll_strength", 1.0)),
                        roll_max=float(getattr(sc, "fc_head_roll_max", 30.0)))
                except Exception:
                    pass

    if _FC["recording"]:
        _FC["buffer"].append((int(sc.frame_current), dict(vals)))

    if getattr(sc, "fc_live", True) or _FC["recording"]:
        _redraw()

    return 1.0 / 60.0


# ---------------------------------------------------------------- 录制与烘焙
def _facecap_rec_start(rig_name):
    if not _FC["running"]:
        return False, "请先开启面捕"
    _FC["buffer"] = []
    _FC["recording"] = True
    return True, "开始录制"


def _facecap_rec_stop():
    n = len(_FC["buffer"])
    _FC["recording"] = False
    return n


def _facecap_autokey(samples, fps, min_gap, thresh, smooth, beta, zero_eps=0.01):
    """自适应抽帧：变化超 thresh 立刻落帧，否则至少间隔 min_gap。
    首末帧必定保留。返回 [(frame, value)]。

    两个实测教训（见 verify_facecap.txt 首轮）：
    1) 录制缓冲里的数据已经被实时 One Euro 滤过一遍。烘焙时若再做强平滑，
       4 帧的眨眼尖峰会被削到 0.27（输入 1.0）—— 所以这里的平滑只做
       "轻微整形"，强度由 smooth 参数控制，默认值应偏小。
    2) 平滑会在末尾留下拖尾（实测末值 0.0025）。收尾时若已接近 0，
       直接写 0，避免残留值糊在动画上。
    """
    if not samples:
        return []
    if len(samples) == 1:
        f0, v0 = samples[0]
        return [(f0, 0.0 if abs(v0) < zero_eps else v0)]

    vals = [v for _f, v in samples]
    # smooth <= 0 表示不平滑（录制时已滤过）
    if smooth and smooth > 0.0:
        sm = _facecap_smooth_series(vals, smooth, beta, fps)
    else:
        sm = list(vals)

    out = [(samples[0][0], sm[0])]
    last_val = sm[0]
    last_frame = samples[0][0]
    gap = max(1, int(min_gap))

    for i in range(1, len(samples) - 1):
        fr = samples[i][0]
        v = sm[i]
        d = abs(v - last_val)
        if d >= thresh or (fr - last_frame) >= gap:
            if d >= thresh or (fr - last_frame) >= gap * 2 or d > 1e-4:
                out.append((fr, v))
                last_val = v
                last_frame = fr

    if samples[-1][0] != out[-1][0]:
        out.append((samples[-1][0], sm[-1]))

    # 收尾归零：末值接近 0 就写 0，去掉平滑拖尾
    lf, lv = out[-1]
    if abs(lv) < zero_eps:
        out[-1] = (lf, 0.0)
    # 首值同理
    ff, fv = out[0]
    if abs(fv) < zero_eps:
        out[0] = (ff, 0.0)
    return out


def _facecap_bake(rig_name, min_gap, thresh, smooth, beta, clear_first):
    """缓冲 -> 关键帧。全通道写入（含归零），避免残留值飘。
    返回 (写入了关键帧的通道数, 关键帧总数)。

    smooth 语义：<= 0 表示不再平滑（录制时实时滤波已滤过一遍，默认走这条，
    避免二次平滑削掉眨眼尖峰）；> 0 时按该截止频率做一次离线平滑。"""
    cfg, arm = _rig(rig_name)
    if arm is None:
        return 0, 0
    buf = _FC["buffer"]
    if not buf:
        return 0, 0

    sc = _fc_scene()
    fps = float(sc.render.fps) if sc else 24.0

    keys = _FC["channels"] or _facecap_channels(arm)
    if not keys:
        return 0, 0

    frames_sorted = sorted(set(f for f, _d in buf))
    per_frame = {}
    for f, d in buf:
        per_frame.setdefault(f, {}).update(d)

    channels = sorted(keys.keys())

    if clear_first:
        for ch in channels:
            k = keys[ch]
            for o in _face_meshes(arm):
                sk = o.data.shape_keys
                if sk is None or sk.animation_data is None:
                    continue
                ad = sk.animation_data
                if ad.action is None:
                    continue
                path = 'key_blocks["%s"].value' % k
                for fc in list(ad.action.fcurves):
                    if fc.data_path == path:
                        try:
                            ad.action.fcurves.remove(fc)
                        except Exception:
                            pass

    total_keys = 0
    used_channels = 0
    for ch in channels:
        k = keys[ch]
        series = []
        cur = 0.0
        for f in frames_sorted:
            d = per_frame.get(f) or {}
            if ch in d:
                cur = d[ch]
            series.append((f, cur))

        # 全零通道不写关键帧：白占曲线、干扰后期手调
        if not any(abs(v) > 1e-9 for _f, v in series):
            continue

        picked = _facecap_autokey(series, fps, min_gap, thresh,
                                  smooth, beta)
        if not picked:
            continue
        wrote = False
        for f, v in picked:
            v = 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)
            if not _face_set(arm, k, v):
                continue
            for o in _face_meshes(arm):
                sk = o.data.shape_keys
                if sk is None:
                    continue
                kb = sk.key_blocks.get(k)
                if kb is None:
                    continue
                kb.keyframe_insert("value", frame=int(round(f)))
                total_keys += 1
                wrote = True
        if wrote:
            used_channels += 1
    _upd()
    return used_channels, total_keys


def _facecap_target(self, context):
    """面捕目标角色的动态枚举。
    与插件其他下拉一致：运行时从 rig_profile 扫描结果生成，不写死角色名。"""
    rigs = _ensure_rigs()
    if not rigs:
        return [("NONE", "（未发现角色）", "请先导入带 rig_profile 的角色")]
    return [(k, rigs[k]["label"], rigs[k].get("_source", ""))
            for k in sorted(rigs)]


def _facecap_rig_name(scene):
    """当前选中的面捕目标。运行中时以实际运行的角色为准。"""
    if _FC["running"] and _FC["rig"]:
        return _FC["rig"]
    v = getattr(scene, "fc_rig", "")
    rigs = _ensure_rigs()
    if v and v in rigs:
        return v
    if v and v != "NONE":
        return v
    return sorted(rigs)[0] if rigs else ""


def _facecap_status_text(scene):
    """面板顶部的状态摘要。"""
    if not _FC["running"]:
        return "未运行"
    n = _FC["frames_in"]
    if _FC["err"]:
        return "错误"
    if n == 0:
        return "运行中（等待人脸…）"
    return "运行中 %.1f fps" % _FC["fps"]


def _facecap_deps_ok():
    """依赖与模型是否就绪，返回 (ok, 说明)。用于面板提前给出可读提示。"""
    cv2, vision, err = _facecap_import()
    if err:
        return False, err
    m = _facecap_model_path()
    if not os.path.isfile(m):
        return False, "缺少模型文件：%s" % m
    return True, "依赖就绪（%s）" % _facecap_deps_dir()


# ---------------------------------------------------------------- 表情校准
# mediapipe 只输出绝对分数，没有校准接口 —— 静息偏置（面无表情时
# eyeBlinkLeft 可能已是 0.15、jawOpen 可能是 0.08）会直接叠加到形态键上，
# 表现为"角色表情一直微微不对"。这里做两阶段校准：
#   阶段一 中性  记录每个通道的静息值 → 之后全部减掉（去偏置）
#                同时记录头部朝向 → 作为零位
#   阶段二 闭眼  记录眨眼通道的满值 → 把 0..满值 归一化到 0..1（补幅度）
FC_CALIB_NEUTRAL_SEC = 2.0
FC_CALIB_CLOSED_SEC = 2.0
FC_CALIB_GAP1_SEC = 2.0      # 中性 → 闭眼 之间的间隔（给准备时间）
FC_CALIB_GAP2_SEC = 1.0      # 闭眼结束后的收尾间隔
FC_CALIB_EYE_CHANNELS = ("blink_left", "blink_right", "blink",
                         "lowerlid_left", "lowerlid_right")
FC_CALIB_MSG_NEUTRAL = "保持面部无表情（眼自然睁开·嘴闭紧·头正对镜头）"
FC_CALIB_MSG_GAP1 = "准备闭眼"
FC_CALIB_MSG_CLOSED = "请闭眼"
FC_CALIB_MSG_GAP2 = "校准完成"

# 阶段时长表
FC_CALIB_DUR = {
    "neutral": FC_CALIB_NEUTRAL_SEC,
    "gap1": FC_CALIB_GAP1_SEC,
    "closed": FC_CALIB_CLOSED_SEC,
    "gap2": FC_CALIB_GAP2_SEC,
}
FC_CALIB_MSG = {
    "neutral": FC_CALIB_MSG_NEUTRAL,
    "gap1": FC_CALIB_MSG_GAP1,
    "closed": FC_CALIB_MSG_CLOSED,
    "gap2": FC_CALIB_MSG_GAP2,
}


# ---------------------------------------------------------------- 提示音
# 用 winsound 播系统音（Windows 自带，不依赖音频文件）。
# 全部异步（SND_ASYNC），绝不阻塞主线程 —— 主线程一卡，面捕就掉帧。
FC_SOUND_OK = True
try:
    import winsound as _winsound
except Exception:
    _winsound = None
    FC_SOUND_OK = False


def _fc_beep(kind="stage"):
    """提示音。kind: stage(阶段切换) / done(完成) / start(开始)

    音量/音色由系统事件音决定，不引入外部文件：
      start → MB_ICONASTERISK  轻提示
      stage → MB_ICONHAND      明确提示（要你做动作）
      done  → MB_ICONEXCLAMATION 完成感
    """
    if not FC_SOUND_OK or _winsound is None:
        return
    # 尊重面板开关
    try:
        sc = _fc_scene()
        if sc is not None and not getattr(sc, "fc_calib_sound", True):
            return
    except Exception:
        pass
    try:
        if kind == "start":
            _winsound.MessageBeep(_winsound.MB_ICONASTERISK)
        elif kind == "done":
            _winsound.MessageBeep(_winsound.MB_ICONEXCLAMATION)
        else:
            _winsound.MessageBeep(_winsound.MB_ICONHAND)
    except Exception:
        # 系统音不可用就退回频率蜂鸣（同样异步会阻塞，故用极短音）
        try:
            _winsound.Beep(880, 90)
        except Exception:
            pass


def _fc_calib_begin():
    """开始两阶段校准（含间隔与提示音）。返回 (ok, msg)"""
    if not _FC["running"]:
        return False, "请先开始面捕"
    if _FC.get("calib_state", "idle") != "idle":
        return False, "校准进行中"
    if _FC.get("frames_in", 0) < 5:
        return False, "还没有收到面部数据，请稍候"
    _FC["calib_expr"] = {}
    _FC["calib_state"] = "neutral"
    _FC["calib_buf"] = []
    _FC["calib_t0"] = time.monotonic()
    _FC["calib_msg"] = FC_CALIB_MSG_NEUTRAL
    _FC["calib_stage_no"] = 1
    _fc_beep("start")
    return True, "校准开始：%s" % FC_CALIB_MSG_NEUTRAL


def _fc_calib_cancel():
    _FC["calib_state"] = "idle"
    _FC["calib_buf"] = []
    _FC["calib_msg"] = ""


def _fc_calib_progress():
    """返回 (阶段, 剩余秒, 提示语)"""
    st = _FC.get("calib_state", "idle")
    if st == "idle":
        return "idle", 0.0, ""
    dur = FC_CALIB_DUR.get(st, 1.0)
    el = time.monotonic() - _FC.get("calib_t0", 0.0)
    return st, max(0.0, dur - el), _FC.get("calib_msg", "")


def _fc_calib_tick(raw, rot):
    """每个推理结果到达时采样。阶段结束自动推进/落库。

    状态机：neutral → gap1 → closed → gap2 → idle
    只有 neutral 与 closed 阶段采样；两个 gap 阶段不采样（纯提示）。
    gap 阶段同样有提示音，这样你不用盯着屏幕也能跟上节奏。"""
    st = _FC.get("calib_state", "idle")
    if st == "idle":
        return
    if raw and st in ("neutral", "closed"):
        _FC["calib_buf"].append((dict(raw), rot))
    el = time.monotonic() - _FC.get("calib_t0", 0.0)
    dur = FC_CALIB_DUR.get(st, 1.0)
    if el < dur:
        return

    # ---- 阶段推进 ----
    if st == "neutral":
        _fc_calib_commit_neutral(_FC["calib_buf"])
        _FC["calib_buf"] = []
        _FC["calib_state"] = "gap1"
        _FC["calib_t0"] = time.monotonic()
        _FC["calib_msg"] = FC_CALIB_MSG_GAP1
        _FC["calib_stage_no"] = 2
        _fc_beep("stage")
    elif st == "gap1":
        _FC["calib_state"] = "closed"
        _FC["calib_t0"] = time.monotonic()
        _FC["calib_msg"] = FC_CALIB_MSG_CLOSED
        _FC["calib_stage_no"] = 2
        _fc_beep("stage")
    elif st == "closed":
        _fc_calib_commit_closed(_FC["calib_buf"])
        _FC["calib_buf"] = []
        _FC["calib_state"] = "gap2"
        _FC["calib_t0"] = time.monotonic()
        _FC["calib_msg"] = FC_CALIB_MSG_GAP2
        _FC["calib_stage_no"] = 3
        _fc_beep("done")
    elif st == "gap2":
        _FC["calib_state"] = "idle"
        _FC["calib_msg"] = ""


def _fc_calib_commit_neutral(buf):
    """阶段一：汇总静息基线 + 头部零位"""
    if not buf:
        return
    n = len(buf)
    chans = set()
    for r, _rot in buf:
        chans |= set(r.keys())
    neutral = {}
    for ch in chans:
        neutral[ch] = sum(r.get(ch, 0.0) for r, _rot in buf) / n

    rots = [rot for _r, rot in buf if rot is not None]
    hz = None
    if rots:
        m = len(rots)
        hz = (sum(r[0] for r in rots) / m,
              sum(r[1] for r in rots) / m,
              sum(r[2] for r in rots) / m)

    _FC["calib_expr"] = {"neutral": neutral, "closed": {},
                         "head_zero": hz, "stamp": time.monotonic()}


def _fc_calib_commit_closed(buf):
    """阶段二：汇总眨眼满值（用于幅度归一化）"""
    ce = _FC.get("calib_expr") or {}
    if not buf:
        return
    neutral = ce.get("neutral") or {}
    closed = {}
    for ch in FC_CALIB_EYE_CHANNELS:
        vals = [r.get(ch, 0.0) for r, _rot in buf if ch in r]
        if not vals:
            continue
        # 丢掉前 1/3（闭眼动作的过渡段），取后段平均更接近真实满值
        tail = vals[len(vals) // 3:] or vals
        closed[ch] = sum(tail) / len(tail)
    ce["closed"] = closed
    ce["stamp"] = time.monotonic()
    _FC["calib_expr"] = ce


def _fc_calib_apply(raw):
    """去偏置 + 眼睛幅度归一化"""
    ce = _FC.get("calib_expr")
    if not ce:
        return raw
    neutral = ce.get("neutral") or {}
    closed = ce.get("closed") or {}
    out = {}
    for ch, v in raw.items():
        d = v - neutral.get(ch, 0.0)
        if d < 0.0:
            d = 0.0
        if ch in FC_CALIB_EYE_CHANNELS and ch in closed:
            denom = closed[ch] - neutral.get(ch, 0.0)
            if denom > 0.05:
                d = d / denom
        out[ch] = d
    return out


def _fc_calib_head_zero():
    """头部零位（pitch, yaw, roll），无校准时返回 None"""
    ce = _FC.get("calib_expr") or {}
    return ce.get("head_zero")


def _fc_calib_summary():
    """校准结果摘要（面板显示用）"""
    ce = _FC.get("calib_expr")
    if not ce:
        return "未校准"
    neutral = ce.get("neutral") or {}
    closed = ce.get("closed") or {}
    # 挑几个有代表性的通道
    keys = ("jaw_open", "blink_left", "blink_right", "mouth_smile")
    parts = []
    for k in keys:
        if k in neutral:
            s = "%s %.2f" % (k.replace("_", " "), neutral[k])
            if k in closed:
                s += "→%.2f" % closed[k]
            parts.append(s)
    hz = ce.get("head_zero")
    tail = ("  头部零位 %.1f/%.1f" % (hz[0], hz[1])) if hz else ""
    return "  ".join(parts) + tail if parts else "已校准"


class EZ_OT_facecap_calib_expr(bpy.types.Operator):
    bl_idname = "ez.facecap_calib_expr"
    bl_label = "校准表情"
    bl_description = ("两阶段校准：先保持面部无表情 2 秒（记录静息基线 + 头部零位），"
                      "再闭眼 2 秒（记录眨眼满值做幅度归一化）")
    bl_options = {"REGISTER"}

    def execute(self, context):
        ok, msg = _fc_calib_begin()
        if not ok:
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}
        _redraw()
        self.report({"INFO"}, msg)
        return {"FINISHED"}


class EZ_OT_facecap_calib_cancel(bpy.types.Operator):
    bl_idname = "ez.facecap_calib_cancel"
    bl_label = "取消校准"
    bl_description = "中止正在进行的表情校准"
    bl_options = {"REGISTER"}

    def execute(self, context):
        _fc_calib_cancel()
        _redraw()
        self.report({"INFO"}, "校准已取消")
        return {"FINISHED"}


class EZ_OT_facecap_calib_clear(bpy.types.Operator):
    bl_idname = "ez.facecap_calib_clear"
    bl_label = "清除校准"
    bl_description = "丢弃已保存的表情校准数据，回到原始输出"
    bl_options = {"REGISTER"}

    def execute(self, context):
        _FC["calib_expr"] = {}
        _redraw()
        self.report({"INFO"}, "已清除表情校准")
        return {"FINISHED"}


class EZ_OT_facecap_flip_pitch(bpy.types.Operator):
    bl_idname = "ez.facecap_flip_pitch"
    bl_label = "上下反向"
    bl_description = "抬头/低头方向反了时点这里"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        sc = context.scene
        sc.fc_head_pitch_sign = -float(getattr(sc, "fc_head_pitch_sign", -1.0))
        _redraw()
        self.report({"INFO"}, "上下方向已%s" % (
            "反向" if sc.fc_head_pitch_sign < 0 else "正向"))
        return {"FINISHED"}


class EZ_OT_facecap_roll_install(bpy.types.Operator):
    bl_idname = "ez.facecap_roll_install"
    bl_label = "安装歪头约束"
    bl_description = ("给头部骨骼加一条 COPY_ROTATION(mix=ADD, 只开Z) 约束，"
                      "让 TRACK_TO 之外多出一个 roll 自由度。幂等，可重复执行")
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        rig = _facecap_rig_name(context.scene)
        cfg, arm = _rig(rig)
        if arm is None:
            self.report({"ERROR"}, "未找到角色骨架")
            return {"CANCELLED"}
        ok, msg = _facecap_ensure_roll(arm, install=True)
        if not ok:
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}
        _upd()
        _redraw()
        self.report({"INFO"}, "%s：%s" % (rig, msg))
        return {"FINISHED"}


class EZ_OT_facecap_roll_remove(bpy.types.Operator):
    bl_idname = "ez.facecap_roll_remove"
    bl_label = "移除歪头约束"
    bl_description = "移除歪头约束，回到只有 pitch/yaw 的原生行为"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        rig = _facecap_rig_name(context.scene)
        cfg, arm = _rig(rig)
        if arm is None:
            self.report({"ERROR"}, "未找到角色骨架")
            return {"CANCELLED"}
        ok, msg = _facecap_ensure_roll(arm, install=False)
        if not ok:
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}
        _upd()
        _redraw()
        self.report({"INFO"}, "%s：%s" % (rig, msg))
        return {"FINISHED"}


class EZ_OT_facecap_flip_roll(bpy.types.Operator):
    bl_idname = "ez.facecap_flip_roll"
    bl_label = "歪头反向"
    bl_description = "歪头方向反了时点这里"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        sc = context.scene
        sc.fc_head_roll_sign = -float(getattr(sc, "fc_head_roll_sign", -1.0))
        _redraw()
        self.report({"INFO"}, "歪头方向已%s" % (
            "反向" if sc.fc_head_roll_sign < 0 else "正向"))
        return {"FINISHED"}


# ---------------------------------------------------------------- 操作符
class EZ_OT_facecap_start(bpy.types.Operator):
    bl_idname = "ez.facecap_start"
    bl_label = "开启面捕"
    bl_description = "启动摄像头追踪，实时驱动表情形态键"
    bl_options = {"REGISTER"}

    def execute(self, context):
        sc = context.scene
        rig = _facecap_rig_name(sc)
        if not rig:
            self.report({"ERROR"}, "未发现角色，请先导入带 rig_profile 的角色")
            return {"CANCELLED"}
        ok, msg = _facecap_start(rig, int(getattr(sc, "fc_cam", 0)),
                                 bool(getattr(sc, "fc_head", True)))
        if not ok:
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}
        _redraw()
        self.report({"INFO"}, msg)
        return {"FINISHED"}


class EZ_OT_facecap_stop(bpy.types.Operator):
    bl_idname = "ez.facecap_stop"
    bl_label = "停止面捕"
    bl_description = "停止追踪并松开头部控制"
    bl_options = {"REGISTER"}

    def execute(self, context):
        _facecap_stop()
        _redraw()
        self.report({"INFO"}, "已停止")
        return {"FINISHED"}


class EZ_OT_facecap_record(bpy.types.Operator):
    bl_idname = "ez.facecap_record"
    bl_label = "开始录制"
    bl_description = "把面捕数据录进缓冲（可反复重烘焙，不用重演）"
    bl_options = {"REGISTER"}

    def execute(self, context):
        sc = context.scene
        if _FC["recording"]:
            n = _facecap_rec_stop()
            self.report({"INFO"}, "已停止录制，缓冲 %d 帧" % n)
        else:
            rig = _facecap_rig_name(sc)
            ok, msg = _facecap_rec_start(rig)
            if not ok:
                self.report({"ERROR"}, msg)
                return {"CANCELLED"}
            self.report({"INFO"}, msg)
        _redraw()
        return {"FINISHED"}


class EZ_OT_facecap_bake(bpy.types.Operator):
    bl_idname = "ez.facecap_bake"
    bl_label = "烘焙到时间轴"
    bl_description = "平滑 + 自适应抽帧后写入关键帧（全通道，含归零）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        sc = context.scene
        rig = _facecap_rig_name(sc)
        if not rig:
            self.report({"ERROR"}, "未发现角色")
            return {"CANCELLED"}
        if _FC["recording"]:
            _facecap_rec_stop()
        n_ch, n_key = _facecap_bake(
            rig,
            int(getattr(sc, "fc_mingap", 3)),
            float(getattr(sc, "fc_thresh", 0.06)),
            float(getattr(sc, "fc_bake_smooth", 0.0)),
            float(getattr(sc, "fc_beta", 0.007)),
            bool(getattr(sc, "fc_clear", True)))
        if n_key == 0:
            self.report({"WARNING"}, "缓冲为空或没有可映射的通道")
            return {"CANCELLED"}
        _redraw()
        self.report({"INFO"}, "已烘焙 %d 通道 / %d 关键帧" % (n_ch, n_key))
        return {"FINISHED"}


class EZ_OT_facecap_calib(bpy.types.Operator):
    bl_idname = "ez.facecap_calib"
    bl_label = "重新标定头部"
    bl_description = "重新测量手柄映射矩阵与距离（改了 rig 之后需要）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        rig = _facecap_rig_name(context.scene)
        cfg, arm = _rig(rig)
        if arm is None:
            self.report({"ERROR"}, "未找到角色骨架")
            return {"CANCELLED"}
        calib = _facecap_calib_head(arm)
        if calib is None:
            self.report({"ERROR"}, "标定失败：未发现头部结构或矩阵退化")
            return {"CANCELLED"}
        _FC["calib"][rig] = calib
        M = calib["M"]
        self.report({"INFO"}, "%s | %s | D=%.6f Mdiag=[%.5f %.5f %.5f]" % (
            calib["driven"], calib["handle"], calib["D"],
            M[0][0], M[1][1], M[2][2]))
        return {"FINISHED"}


class EZ_OT_facecap_clear_buffer(bpy.types.Operator):
    bl_idname = "ez.facecap_clear_buffer"
    bl_label = "清空缓冲"
    bl_description = "丢弃已录制的面捕数据"
    bl_options = {"REGISTER"}

    def execute(self, context):
        _FC["buffer"] = []
        _FC["recording"] = False
        _redraw()
        self.report({"INFO"}, "缓冲已清空")
        return {"FINISHED"}


class EZ_OT_facecap_probe(bpy.types.Operator):
    bl_idname = "ez.facecap_probe"
    bl_label = "检查依赖"
    bl_description = "检查 mediapipe / opencv / 模型文件是否就绪，并显示实际使用的路径"
    bl_options = {"REGISTER"}

    def execute(self, context):
        ok, msg = _facecap_deps_ok()
        if ok:
            self.report({"INFO"}, msg)
            return {"FINISHED"}
        self.report({"ERROR"}, msg)
        return {"CANCELLED"}


class EZ_PT_facecap(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "EZ V2B"
    bl_label = "面部捕捉（摄像头）"

    def draw(self, context):
        layout = self.layout

        # ---- 依赖状态（分发版新增：先把"能不能跑"讲清楚）----
        _st = _EZD.status()
        _db = layout.box()
        _db.label(text="面捕依赖", icon="PLUGIN")
        if _st["ok"]:
            _db.label(text="cv2 %s / mediapipe %s" % (_st["cv2"], _st["mediapipe"]),
                      icon="CHECKMARK")
        else:
            _db.label(text="未就绪", icon="ERROR")
            for _m in _st["missing"][:3]:
                _db.label(text="缺 %s" % str(_m)[:58])
            _r = _db.row(align=True)
            _r.scale_y = 1.3
            _r.operator("ez.facecap_deps_install", text="安装面捕依赖", icon="IMPORT")
            _r.operator("ez.facecap_deps_open", text="", icon="FILE_FOLDER")
            _db.label(text="依赖约 273 MB，与插件分开存放", icon="INFO")

        # 提示：面板下方「目标与依赖」区可手动指定依赖目录 / 模型文件 / 摄像头序号，
        # 顶部这里是自动检测结果，两者配合使用。
        sc = context.scene
        rigs = _ensure_rigs()
        if not rigs:
            layout.label(text="未发现角色", icon="ERROR")
            return

        cur = _facecap_rig_name(sc)

        # ---- 主按钮：一键全开 ----
        box = layout.box()
        col = box.column(align=True)
        col.scale_y = 1.6
        if _FC["running"]:
            col.operator("ez.facecap_window", text="打开预览窗口",
                         icon="VIEW_CAMERA")
        else:
            col.operator("ez.facecap_window",
                         text="开始面捕（打开预览窗口）", icon="PLAY")

        # ---- 摄像头打开进度（打开期间主线程会被短时阻塞，这里给出可见反馈）----
        if _FC.get("cap_state") == "opening":
            _ob = layout.box()
            _ob.label(text="正在打开摄像头…", icon="TIME")
            _i = _FC.get("open_try", 0)
            _tot = 3
            _ob.label(text="后端 %d/%d（MSMF / ANY / DSHOW）" % (min(_i + 1, _tot), _tot))
            _ob.label(text="若长时间无响应，点下面的按钮取消", icon="INFO")
            _ob.operator("ez.facecap_wstart", text="取消打开", icon="X")

        # ---- 运行控制（全部按钮集中在这里）----
        box = layout.box()
        box.label(text="控制", icon="PLAY")
        row = box.row(align=True)
        row.scale_y = 1.3
        if _FC["running"]:
            row.operator("ez.facecap_wstart", text="停止面捕",
                         icon="PAUSE")
        else:
            row.operator("ez.facecap_wstart", text="开始面捕", icon="PLAY")
        row = box.row(align=True)
        row.scale_y = 1.2
        if _FC["recording"]:
            row.alert = True
            row.operator("ez.facecap_wrec", text="停止录制", icon="PAUSE")
            row.alert = False
        else:
            row.operator("ez.facecap_wrec", text="录制", icon="REC")
        row.operator("ez.facecap_clear_buffer", text="", icon="TRASH")
        row = box.row(align=True)
        row.scale_y = 1.2
        row.operator("ez.facecap_wbake", text="烘焙到时间轴",
                     icon="KEYFRAME")
        row = box.row(align=True)
        row.operator("ez.facecap_toggle_mirror", text="镜像",
                     icon="IMAGE_ALPHA",
                     depress=bool(getattr(sc, "fc_mirror", True)))
        row.operator("ez.facecap_toggle_head", text="头部跟随",
                     icon="BONE_DATA",
                     depress=bool(getattr(sc, "fc_head", True)))
        row.operator("ez.facecap_close_window", text="关闭预览",
                     icon="X")
        box.label(text="缓冲 %d 帧%s" % (
            len(_FC["buffer"]), "（录制中）" if _FC["recording"] else ""))

        # ---- 表情校准 ----
        box = layout.box()
        box.label(text="表情校准", icon="DRIVER")
        st, remain, msg = _fc_calib_progress()
        if st != "idle":
            # 四阶段状态机：neutral → gap1 → closed → gap2
            col = box.column(align=True)
            col.alert = (st in ("neutral", "closed"))   # 只有采样阶段闪红
            col.scale_y = 1.5
            col.label(text="%s（还剩 %.1f 秒）" % (msg, remain),
                      icon="TIME")
            stage_txt = {
                "neutral": "第 1 步 / 3：记录静息基线",
                "gap1": "间隔中：请准备闭眼",
                "closed": "第 2 步 / 3：记录闭眼满值",
                "gap2": "收尾中",
            }.get(st, st)
            col.label(text=stage_txt, icon="INFO")
            box.operator("ez.facecap_calib_cancel", text="取消校准",
                         icon="X")
        else:
            row = box.row(align=True)
            row.scale_y = 1.3
            row.operator("ez.facecap_calib_expr",
                         text="校准表情", icon="DRIVER")
            row.operator("ez.facecap_calib_clear", text="", icon="TRASH")
            box.label(text="1. 保持无表情 2 秒", icon="LAYER_USED")
            box.label(text="2. 听到提示音后闭眼 2 秒", icon="LAYER_USED")
            box.label(text="3. 自动完成", icon="LAYER_USED")
            box.label(text=_fc_calib_summary())
        row = box.row(align=True)
        row.operator("ez.facecap_flip_pitch", text="上下反向",
                     icon="TRIA_UP")
        row.prop(sc, "fc_calib_sound", text="提示音",
                 icon="SOUND", toggle=True)

        # ---- 歪头（roll）----
        box = layout.box()
        box.label(text="歪头（roll）", icon="BONE_DATA")
        cfg2, arm2 = _rig(cur)
        if arm2 is not None:
            installed, info = _facecap_roll_state(arm2)
        else:
            installed, info = False, "未找到骨架"
        row = box.row(align=True)
        row.label(text="约束: %s" % ("已安装" if installed else "未安装"),
                  icon="CHECKMARK" if installed else "ERROR")
        if installed:
            box.label(text=info)
            col = box.column(align=True)
            col.prop(sc, "fc_head_roll", text="歪头跟随")
            if getattr(sc, "fc_head_roll", True):
                col.prop(sc, "fc_head_roll_strength", text="强度",
                         slider=True)
                col.prop(sc, "fc_head_roll_max", text="最大角", slider=True)
                row = col.row(align=True)
                row.operator("ez.facecap_flip_roll", text="歪头反向",
                             icon="TRIA_LEFT")
            box.operator("ez.facecap_roll_remove", text="移除歪头约束",
                         icon="X")
        else:
            box.label(text="TRACK_TO 只能表达 pitch/yaw")
            box.operator("ez.facecap_roll_install", text="安装歪头约束",
                         icon="ADD")

        # ---- 状态 ----
        status = _facecap_status_text(sc)
        row = box.row(align=True)
        if _FC["running"]:
            row.label(text=status, icon="PLAY")
        else:
            row.label(text=status, icon="RADIOBUT_OFF")
        if _FC["err"]:
            box.label(text=_FC["err"][:60], icon="ERROR")

        # ---- 目标与依赖（折叠）----
        ok, msg = _facecap_deps_ok()
        if not ok:
            box = layout.box()
            box.label(text=msg[:56], icon="ERROR")
            col = box.column(align=True)
            col.prop(sc, "fc_rig", text="目标角色")
            col.prop(sc, "fc_cam", text="摄像头序号")
            col.prop(sc, "fc_deps", text="依赖目录")
            col.prop(sc, "fc_model", text="模型文件")
            col.operator("ez.facecap_probe", text="重新检查",
                         icon="FILE_REFRESH")
        else:
            box = layout.box()
            col = box.column(align=True)
            col.enabled = not _FC["running"]
            col.prop(sc, "fc_rig", text="目标角色")
            col.prop(sc, "fc_cam", text="摄像头序号")

        # ---- 头部与参数（折叠进一个子面板，保持简洁）----
        box = layout.box()
        col = box.column(align=True)
        col.prop(sc, "fc_head", text="头部跟随")
        col.prop(sc, "fc_live", text="实时预览驱动")
        col.prop(sc, "fc_strength", text="表情强度", slider=True)
        # 单眼眨增益：只放大 wink，不动双眼闭
        col.prop(sc, "fc_wink_gain", text="单眼眨增益", slider=True)


# -*- coding: utf-8 -*-
# ============================================================================
# 面部捕捉预览（并入 karin_ik_tools 的独立区块）
#
# 按钮全部在 3D 侧栏面板里；本模块只负责"把画面画出来"。
#
# 本模块修掉过的四个真实缺陷（都有实测依据）：
#   1) 图像每帧重建 → 编辑器绑定失效 → 数帧后报错卡住
#      成因：占位图按 320x240 建，真帧 640x480 到达时 _fc_ensure_preview_image
#            尺寸不符就 remove+new，而编辑器仍指着旧 datablock。
#      修法：图像尺寸固定用 FC_PREVIEW_W/H，写入前把帧 resize 到该尺寸，
#            尺寸恒定 → 永不重建。另加"绝不删除正在使用的图像"守卫。
#   2) 两个 timer 抢同一个队列 → 每秒上百次空队列异常
#      修法：追踪线程只投递到队列；tick 取走后存进 _FC["last_frame"]，
#            预览 tick 只读这份快照，不再碰队列。
#   3) 画面上下颠倒
#      成因：Blender Image 像素原点在左下角，cv2 在左上角。
#      修法：写入前 rgb[::-1]。
#   4) mask 太稀（只有 36 点外轮廓）
#      修法：用官方 FaceLandmarksConnections 拓扑画五官轮廓 + 特征点。
# ============================================================================

FC_PREVIEW_IMG = "FaceCapPreview"
FC_PREVIEW_W = 640
FC_PREVIEW_H = 480

# 绘制样式（BGR）
FC_C_OVAL = (90, 255, 150)      # 外脸轮廓：亮绿
FC_C_MESH = (60, 110, 90)       # 三角网：暗绿
FC_C_FEAT = (70, 230, 255)      # 五官轮廓：黄
FC_C_DOT = (120, 200, 255)      # 特征点：橙黄
FC_C_IRIS = (255, 120, 220)     # 虹膜：紫

# 官方拓扑（_face_work/mesh_pairs.py 实测导出，勿手改索引）
# 格式 (起点, 终点)，直接喂 cv2.polylines
MESH_FACE_OVAL = (
    (10,21), (21,54), (54,58), (58,67), (67,93), (93,103), (103,109), (109,127),
    (127,132), (132,136), (136,148), (148,149), (149,150), (150,152), (152,162),
    (162,172), (172,176), (176,234), (234,251), (251,284), (284,288), (288,297),
    (297,323), (323,332), (332,338), (338,356), (356,361), (361,365), (365,377),
    (377,378), (378,379), (379,389), (389,397), (397,400), (400,454), (454,10),
)
MESH_LEFT_EYE = (
    (249,263), (263,362), (362,373), (373,374), (374,380), (380,381), (381,382),
    (382,384), (384,385), (385,386), (386,387), (387,388), (388,390), (390,398),
    (398,466), (466,249),
)
MESH_RIGHT_EYE = (
    (7,33), (33,133), (133,144), (144,145), (145,153), (153,154), (154,155),
    (155,157), (157,158), (158,159), (159,160), (160,161), (161,163), (163,173),
    (173,246), (246,7),
)
MESH_LEFT_BROW = (
    (276,282), (282,283), (283,285), (285,293), (293,295), (295,296),
    (296,300), (300,334), (334,336), (336,276),
)
MESH_RIGHT_BROW = (
    (46,52), (52,53), (53,55), (55,63), (63,65), (65,66), (66,70),
    (70,105), (105,107), (107,46),
)
MESH_LIPS = (
    (0,13), (13,14), (14,17), (17,37), (37,39), (39,40), (40,61), (61,78),
    (78,80), (80,81), (81,82), (82,84), (84,87), (87,88), (88,91), (91,95),
    (95,146), (146,178), (178,181), (181,185), (185,191), (191,267), (267,269),
    (269,270), (270,291), (291,308), (308,310), (310,311), (311,312), (312,314),
    (314,317), (317,318), (318,321), (321,324), (324,375), (375,402), (402,405),
    (405,409), (409,415), (415,0),
)
MESH_NOSE = (
    (1,2), (2,4), (4,5), (5,6), (6,19), (19,45), (45,48), (48,64), (64,94),
    (94,97), (97,98), (98,115), (115,168), (168,195), (195,197), (197,220),
    (220,275), (275,278), (278,294), (294,326), (326,327), (327,344), (344,440),
    (440,1),
)
MESH_LEFT_IRIS = ((474,475), (475,476), (476,477), (477,474))
MESH_RIGHT_IRIS = ((469,470), (470,471), (471,472), (472,469))

# 五官轮廓组（画线用）
FC_CONTOUR_GROUPS = (
    (MESH_FACE_OVAL, FC_C_OVAL, 2),
    (MESH_LEFT_BROW, FC_C_FEAT, 1),
    (MESH_RIGHT_BROW, FC_C_FEAT, 1),
    (MESH_LEFT_EYE, FC_C_FEAT, 1),
    (MESH_RIGHT_EYE, FC_C_FEAT, 1),
    (MESH_LIPS, FC_C_FEAT, 1),
    (MESH_NOSE, FC_C_MESH, 1),
    (MESH_LEFT_IRIS, FC_C_IRIS, 1),
    (MESH_RIGHT_IRIS, FC_C_IRIS, 1),
)

# 特征点（画点用）：五官关键点 + 虹膜
FC_DOT_POINTS = tuple(sorted({
    10, 152, 234, 454, 127, 356, 33, 133, 362, 263,      # 轮廓关键
    1, 4, 6, 168, 197, 195, 5, 98, 327, 2, 326, 97,       # 鼻
    61, 291, 13, 14, 17, 0, 267, 269, 270, 37, 39, 40,    # 唇
    185, 409, 78, 308, 80, 310, 81, 311, 82, 312,         # 唇细分
    46, 276, 53, 283, 52, 282, 105, 334, 63, 293,         # 眉
    468, 469, 470, 471, 472, 473, 474, 475, 476, 477,     # 虹膜
}))


def _fc_cv():
    """预览用到的 cv2。必须经 _facecap_import() 装载 ——
    真源里没有模块级 import cv2，裸名会 NameError（验证抓到过）。"""
    c = _FACECAP_MOD.get("cv2")
    if c is None:
        _facecap_import()
        c = _FACECAP_MOD.get("cv2")
    return c


def _fc_ensure_preview_image(w=FC_PREVIEW_W, h=FC_PREVIEW_H):
    """预览图像 datablock。

    【关键】尺寸必须恒定。曾经的做法是"尺寸不符就 remove+new"，
    而占位图按 320x240 建、真帧 640x480 到达时会触发重建，
    编辑器仍绑定旧 datablock → 数帧后报错卡住（实测确认）。
    现在一律用 FC_PREVIEW_W/H，写入前把帧 resize 过来，永不重建。"""
    img = bpy.data.images.get(FC_PREVIEW_IMG)
    if img is None:
        img = bpy.data.images.new(FC_PREVIEW_IMG, w, h, alpha=False)
        img.use_fake_user = True
        return img
    # 守卫：绝不删除已在使用的图像。尺寸不符就地缩放像素缓冲不现实，
    # 因此这里只做"记录"，由调用方保证请求尺寸恒等于 FC_PREVIEW_W/H。
    if img.size[0] != w or img.size[1] != h:
        _FC["err"] = ("预览图像尺寸异常 %dx%d（应为 %dx%d），已忽略"
                      % (img.size[0], img.size[1], w, h))
    return img


def _fc_fit_frame(cv2, frame):
    """把任意尺寸的帧缩放到固定预览尺寸，保证图像 datablock 永不被重建"""
    if frame is None:
        return None
    h, w = frame.shape[:2]
    if (w, h) == (FC_PREVIEW_W, FC_PREVIEW_H):
        return frame
    return cv2.resize(frame, (FC_PREVIEW_W, FC_PREVIEW_H),
                      interpolation=cv2.INTER_AREA)


def _fc_draw_mask(frame_bgr, lms, mirror=False, show_mesh=True,
                  show_dots=True):
    """在 BGR 帧上画人脸 mask：外轮廓 + 三角网 + 五官轮廓 + 特征点。
    无 landmark 时原样返回。不修改调用方的 frame（在副本上绘制）。

    mirror=True 时按 (1-x) 取 landmark 的 x，与已翻转的画面保持一致。
    这一点必须成对处理：只翻画面不翻坐标，绿框会和脸左右错位。"""
    cv2 = _fc_cv()
    if lms is None or cv2 is None:
        return frame_bgr
    H, W = frame_bgr.shape[:2]

    def P(i):
        p = lms[i]
        x = (1.0 - p.x) if mirror else p.x
        return (int(x * W), int(p.y * H))

    vis = frame_bgr.copy()

    # ---- 1. 三角网（半透明底纹，先画在最下层）----
    if show_mesh:
        overlay = vis.copy()
        # 用五官轮廓围成的区域做半透明填充（比全脸填充更克制）
        oval_pts = [P(i) for i, _j in MESH_FACE_OVAL]
        cv2.fillPoly(overlay, [np.array(oval_pts, dtype=np.int32)],
                     (70, 200, 130))
        cv2.addWeighted(overlay, 0.20, vis, 0.80, 0, vis)

    # ---- 2. 五官轮廓线 ----
    for pairs, color, thick in FC_CONTOUR_GROUPS:
        segs = []
        for a, b in pairs:
            segs.append(np.array([P(a), P(b)], dtype=np.int32))
        cv2.polylines(vis, segs, False, color, thick, cv2.LINE_AA)

    # ---- 3. 特征点 ----
    if show_dots:
        for i in FC_DOT_POINTS:
            if i < len(lms):
                cv2.circle(vis, P(i), 2, FC_C_DOT, -1, cv2.LINE_AA)

    # ---- 4. 双眼与嘴的关键点加粗（便于观察驱动是否跟随）----
    for i in (468, 473):   # 左右虹膜中心
        if i < len(lms):
            cv2.circle(vis, P(i), 3, FC_C_IRIS, -1, cv2.LINE_AA)

    return vis


def _fc_roll_tag():
    """状态栏上的 roll 标记。骨架查不到就返回空串，绝不抛异常。"""
    try:
        cfg, arm = _rig(_FC.get("rig", ""))
        if arm is None:
            return ""
        return " | roll" if _facecap_roll_ready(arm) else ""
    except Exception:
        return ""


def _fc_frame_text():
    """画面顶部状态条文本与颜色（BGR）"""
    sc = _fc_scene()
    # 校准优先：此时提示语最重要
    st = _FC.get("calib_state", "idle")
    if st != "idle":
        _s, remain, _msg = _fc_calib_progress()
        tag = {"neutral": "1/3 NEUTRAL", "gap1": "GET READY",
               "closed": "2/3 CLOSE EYES", "gap2": "3/3 DONE"}.get(st, st)
        return ("CALIB %s  %.1fs" % (tag, remain), (60, 200, 255))
    if not _FC["running"]:
        return ("STOPPED", (150, 150, 150))
    if _FC["err"]:
        return ("ERR: %s" % _FC["err"][:40], (80, 80, 255))
    if _FC["frames_in"] == 0:
        return ("starting camera... (~8s)", (120, 200, 255))
    if _FC["recording"]:
        return ("REC %d fr | %s | %.1f fps" % (
            len(_FC["buffer"]), _FC["rig"], _FC["fps"]), (255, 200, 60))
    return ("%s | %.1f fps | head %s%s" % (
        _FC["rig"], _FC["fps"],
        "on" if getattr(sc, "fc_head", True) else "off",
        _fc_roll_tag()), (90, 255, 150))


def _fc_draw_calib_overlay(vis):
    """校准期间在画面中央画大字提示 + 进度条（四阶段状态机）"""
    cv2 = _fc_cv()
    st = _FC.get("calib_state", "idle")
    if st == "idle":
        return vis
    _s, remain, msg = _fc_calib_progress()
    H, W = vis.shape[:2]
    dur = FC_CALIB_DUR.get(st, 1.0)
    frac = 1.0 - (remain / dur if dur > 0 else 0.0)
    frac = 0.0 if frac < 0.0 else (1.0 if frac > 1.0 else frac)

    # 阶段配色：采样阶段绿、间隔阶段橙、收尾灰
    if st == "neutral":
        accent, title, sub = ((90, 255, 150), "1/3  KEEP NEUTRAL FACE",
                              "eyes open, mouth closed, face camera")
    elif st == "gap1":
        accent, title, sub = ((60, 200, 255), "GET READY",
                              "about to ask you to close your eyes")
    elif st == "closed":
        accent, title, sub = ((90, 255, 150), "2/3  CLOSE YOUR EYES",
                              "keep eyes closed")
    else:
        accent, title, sub = ((180, 180, 180), "3/3  DONE",
                              "calibration saved")

    ov = vis.copy()
    band = 92
    cv2.rectangle(ov, (0, H // 2 - band), (W, H // 2 + band),
                  (20, 20, 24), -1)
    cv2.addWeighted(ov, 0.72, vis, 0.28, 0, vis)

    ts = cv2.getTextSize(title, cv2.FONT_HERSHEY_SIMPLEX, 1.0, 2)[0]
    cv2.putText(vis, title, ((W - ts[0]) // 2, H // 2 - 26),
                cv2.FONT_HERSHEY_SIMPLEX, 1.0, accent, 2, cv2.LINE_AA)

    ss = cv2.getTextSize(sub, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)[0]
    cv2.putText(vis, sub, ((W - ss[0]) // 2, H // 2 + 4),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 200), 1,
                cv2.LINE_AA)

    cd = "%.1f s" % remain
    cs = cv2.getTextSize(cd, cv2.FONT_HERSHEY_SIMPLEX, 0.9, 2)[0]
    cv2.putText(vis, cd, ((W - cs[0]) // 2, H // 2 + 44),
                cv2.FONT_HERSHEY_SIMPLEX, 0.9, accent, 2, cv2.LINE_AA)

    bx0, bx1 = W // 4, W * 3 // 4
    by = H // 2 + band - 14
    cv2.rectangle(vis, (bx0, by), (bx1, by + 6), (70, 70, 74), -1)
    cv2.rectangle(vis, (bx0, by), (bx0 + int((bx1 - bx0) * frac), by + 6),
                  accent, -1)
    return vis


def _fc_write_preview(img, frame_bgr, lms):
    """画好 mask 和状态条，写入 Blender Image。不修改调用方的 frame。"""
    cv2 = _fc_cv()
    sc = _fc_scene()
    mirror = bool(getattr(sc, "fc_mirror", True)) if sc else True
    frame_bgr = _fc_fit_frame(cv2, frame_bgr)
    if frame_bgr is None:
        return
    if mirror:
        frame_bgr = cv2.flip(frame_bgr, 1)
    vis = _fc_draw_mask(frame_bgr, lms, mirror=mirror)
    vis = _fc_draw_calib_overlay(vis)

    H, W = vis.shape[:2]
    txt, color = _fc_frame_text()
    vis = vis.copy()
    cv2.rectangle(vis, (0, 0), (W, 30), (20, 20, 20), -1)
    cv2.putText(vis, txt, (8, 21), cv2.FONT_HERSHEY_SIMPLEX,
                0.55, color, 1, cv2.LINE_AA)

    rgb = cv2.cvtColor(vis, cv2.COLOR_BGR2RGB)
    # Blender Image 像素原点在左下角，cv2 在左上角 → 必须垂直翻转
    rgb = rgb[::-1]
    a = np.frombuffer(rgb.tobytes(), dtype=np.uint8).astype(np.float32)
    a *= (1.0 / 255.0)
    n = W * H
    buf = np.ones((n, 4), dtype=np.float32)
    buf[:, :3] = a.reshape(n, 3)
    img.pixels.foreach_set(buf.ravel())
    img.update()


def _fc_placeholder_frame(text, color, sub=None):
    """未追踪时的占位画面（尺寸固定 FC_PREVIEW_W/H，避免触发图像重建）"""
    cv2 = _fc_cv()
    frame = np.zeros((FC_PREVIEW_H, FC_PREVIEW_W, 3), dtype=np.uint8)
    frame[:] = 38
    size = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)[0]
    cv2.putText(frame, text,
                ((FC_PREVIEW_W - size[0]) // 2, FC_PREVIEW_H // 2),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2, cv2.LINE_AA)
    if sub:
        s2 = cv2.getTextSize(sub, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)[0]
        cv2.putText(frame, sub,
                    ((FC_PREVIEW_W - s2[0]) // 2, FC_PREVIEW_H // 2 + 38),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1,
                    cv2.LINE_AA)
    return frame


# ---------------------------------------------------------------- 预览 timer
def _facecap_preview_tick():
    """预览渲染 timer。

    【关键】不碰追踪队列 —— 队列由 _facecap_tick 独占消费，
    取到的最新帧存进 _FC["last_frame"]，这里只读这份快照。
    原实现两个 timer 都去抽同一个队列，实测每秒上百次空队列异常。"""
    if not _FC.get("preview_on"):
        _FC["preview_timer"] = None
        return None

    img = _fc_ensure_preview_image()
    snap = _FC.get("last_frame")

    if snap is None:
        if _FC["err"]:
            ph = _fc_placeholder_frame("ERROR", (80, 80, 255),
                                       _FC["err"][:44])
        elif _FC["running"]:
            ph = _fc_placeholder_frame(
                "starting camera...", (120, 200, 255),
                "first open takes ~8s on Windows")
        else:
            ph = _fc_placeholder_frame(
                "STOPPED", (150, 150, 150),
                "press the button in the 3D sidebar")
        _fc_write_preview(img, ph, None)
        return 0.4

    try:
        _fc_write_preview(img, snap.get("frame"), snap.get("lms"))
    except Exception as e:
        _FC["err"] = "预览写入失败: %s" % e
    return 1.0 / 30.0


def _facecap_preview_ensure_timer():
    if not bpy.app.timers.is_registered(_facecap_preview_tick):
        bpy.app.timers.register(_facecap_preview_tick,
                                first_interval=0.0, persistent=True)
    _FC["preview_timer"] = True


def _facecap_preview_kill_timer():
    if bpy.app.timers.is_registered(_facecap_preview_tick):
        try:
            bpy.app.timers.unregister(_facecap_preview_tick)
        except Exception:
            pass
    _FC["preview_timer"] = None


# ---------------------------------------------------------------- 推理线程（不含摄像头）
class _FaceTrackerPreview(_FaceTracker):
    """推理线程：从 in_q 取帧，推理后把结果投到 q。

    【架构】摄像头 I/O 全部在主线程（_fc_capture_tick）——
    实测 MSMF 在 worker 线程里 opened=True 却读到 0 帧（COM 每线程初始化），
    ANY 在 worker 里初始化要 22 秒且随后读不出帧；主线程 60/60 成功。
    本线程只做 CPU 密集的推理，不碰 cv2 设备。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.want_preview = kwargs.get("want_preview", True)
        self.latest = None
        self.frames_total = 0     # 累计成功帧数（诊断用）
        self.backend = "main-thread"
        # 主线程抓帧后投这里（只保留最新，满了就丢旧的）
        self.in_q = queue.Queue(maxsize=2)

    def run(self):
        cv2, vision, err = _facecap_import()
        if err:
            self.error = err
            return
        if not os.path.isfile(self.model_path):
            self.error = "缺少模型文件 face_landmarker.task：%s" % self.model_path
            return
        try:
            opts = vision.FaceLandmarkerOptions(
                base_options=_FACECAP_MOD["mpp"].BaseOptions(
                    model_asset_path=self.model_path),
                running_mode=vision.RunningMode.VIDEO,
                num_faces=1,
                output_face_blendshapes=True,
                output_facial_transformation_matrixes=self.want_head,
            )
            lm = vision.FaceLandmarker.create_from_options(opts)

            t0 = time.monotonic()
            last = t0
            n = 0
            # mediapipe VIDEO 模式硬要求时间戳「严格递增」：
            # 实测相同时间戳或倒退都会抛
            #   ValueError: Input timestamp must be monotonically increasing.
            # 而 int(ms) 截断在摄像头抖动帧（两帧间隔 <1ms）下会产生相同值，
            # 一旦抛出，原实现直接 break 退出线程 → 表现为"数帧后卡住"。
            prev_ts = 0
            while not self.stop_ev.is_set():
                try:
                    frame = self.in_q.get(timeout=0.2)
                except Exception:
                    continue
                if frame is None:
                    continue

                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mpimg = _FACECAP_MOD["mp"].Image(
                    image_format=_FACECAP_MOD["mp"].ImageFormat.SRGB,
                    data=rgb)
                ts = int((time.monotonic() - t0) * 1000.0)
                if ts <= prev_ts:
                    ts = prev_ts + 1
                prev_ts = ts
                try:
                    res = lm.detect_for_video(mpimg, ts)
                except ValueError as e:
                    # 时间戳问题不该杀死线程：跳过这一帧继续
                    self.skipped = getattr(self, "skipped", 0) + 1
                    if self.skipped <= 3:
                        self.error = "跳帧（时间戳）: %s" % e
                    continue
                except Exception as e:
                    self.error = "推理失败：%s: %s" % (type(e).__name__, e)
                    break

                lms = None
                if res.face_landmarks:
                    lms = res.face_landmarks[0]

                if res.face_blendshapes:
                    bs = {}
                    for c in res.face_blendshapes[0]:
                        bs[c.category_name] = float(c.score)
                    rot = None
                    if self.want_head and res.facial_transformation_matrixes:
                        rot = _facecap_matrix_rot(
                            res.facial_transformation_matrixes[0])
                    payload = {"bs": bs, "rot": rot,
                               "t": time.monotonic() - t0,
                               "lms": lms}
                    try:
                        while self.q.qsize() > 2:
                            self.q.get_nowait()
                    except Exception:
                        pass
                    try:
                        self.q.put_nowait(payload)
                    except Exception:
                        pass
                    n += 1

                self.frames_total += 1
                now = time.monotonic()
                if now - last >= 1.0:
                    self.fps = n / (now - last)
                    n = 0
                    last = now
        except Exception as e:
            self.error = "推理线程异常：%s: %s" % (type(e).__name__, e)


# ---------------------------------------------------------------- 主线程抓帧
def _fc_capture_tick():
    """主线程摄像头 I/O。

    【为什么必须在主线程】实测（_face_work/verify_backend.txt）：
      MSMF  在 worker 线程 opened=True 但读到 0 帧（COM 每线程初始化）
      ANY   在 worker 线程初始化 22 秒，随后仍读不出帧
      主线程 60/60 成功
    所以 VideoCapture 的创建与 read() 全部留在主线程；
    推理交给 _FaceTrackerPreview 线程（纯 CPU，无设备依赖）。

    状态机 idle -> opening -> running。
    opening 期间 VideoCapture 构造会阻塞约 8 秒（Windows MSMF 固定开销），
    这是实测值，无法消除，只能提前在面板上告知。"""
    if not _FC["running"]:
        _FC["cap_state"] = "idle"
        return None

    cv2 = _fc_cv()
    if cv2 is None:
        return 0.2

    state = _FC.get("cap_state", "idle")

    # ---- 打开摄像头（逐 tick 推进，避免主线程长时间冻结）----
    #
    # 【为什么逐 tick】实测（_autorig_work/diag_camera.txt）：
    #   摄像头 0  MSMF 构造 7.89s / ANY 构造 7.44s / DSHOW 0.08s
    #   串行尝试合计 15.42 秒，原实现在**一个 timer 回调里全试完**，
    #   期间 Blender 主线程完全冻结 -> 界面无响应，用户只能强杀。
    # 改成每个 tick 只试一个后端：主线程单次最多冻一个后端的耗时，
    # 界面能刷新，面板上的「停止」也能点得动。
    if state == "idle":
        _FC["cap_state"] = "opening"
        _FC["open_try"] = 0
        _FC["open_t0"] = time.time()
        _FC["open_best"] = None
        return 0.0

    if state == "opening":
        BACKENDS = (("MSMF", cv2.CAP_MSMF), ("ANY", cv2.CAP_ANY),
                    ("DSHOW", cv2.CAP_DSHOW))
        i = _FC.get("open_try", 0)
        t0 = _FC.get("open_t0", time.time())

        # 总超时保护：超过 OPEN_TIMEOUT 就放弃，给明确报错
        if time.time() - t0 > FC_OPEN_TIMEOUT:
            _FC["err"] = ("摄像头打开超时（%.0f 秒，已试 %d/%d 个后端）。"
                          "常见原因：设备被其他程序占用、或被防窥罩/隐私开关挡住。"
                          % (FC_OPEN_TIMEOUT, i, len(BACKENDS)))
            _FC["running"] = False
            _FC["cap_state"] = "idle"
            _facecap_preview_kill_timer()
            _fc_capture_kill_timer()
            return None

        # 用户中途点了停止
        if not _FC["running"]:
            _FC["cap_state"] = "idle"
            return None

        if i >= len(BACKENDS):
            # 全部试完
            cap = _FC.get("open_best")
            if cap is None:
                _FC["err"] = ("摄像头打不开（试过 MSMF/ANY/DSHOW），"
                              "可能被其他程序占用")
                _FC["running"] = False
                _FC["cap_state"] = "idle"
                _facecap_preview_kill_timer()
                _fc_capture_kill_timer()
                return None
            _FC["cap"] = cap
            _FC["cap_state"] = "running"
            _FC["read_fail"] = 0
            _FC["open_best"] = None
            return 0.0

        bname, bid = BACKENDS[i]
        _FC["open_try"] = i + 1
        cap = None
        try:
            cap = cv2.VideoCapture(int(_FC.get("cam_index", 0)), bid)
        except Exception:
            cap = None

        usable = False
        if cap is not None:
            try:
                if cap.isOpened():
                    # **关键修复**：必须实际读到一帧才算可用。
                    # 实测 isOpened()==True 但 read()==False 是常见情况，
                    # 只看 isOpened 会锁死在一个读不出帧的摄像头上（卡死的根因）。
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FC_PREVIEW_W)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FC_PREVIEW_H)
                    for _ in range(3):
                        ok, fr = cap.read()
                        if ok and fr is not None:
                            usable = True
                            break
            except Exception:
                usable = False

        if usable:
            # **一旦找到可用后端就立即定案，绝不再试后面的。**
            #
            # 踩过的坑（用户报 207 秒后出现 MSMF 错误 -1072873821）：
            # 原实现把"找到能用的"存进 open_best 后**继续尝试后续后端**，
            # 每次都会 release 掉上一个候选。MSMF 用的是异步回调
            # （SourceReaderCB::OnReadSample），同一个设备被反复
            # 打开又释放会让它的流被系统判为失效：
            #   -1072873821 = 0xC00D36C3 = MF_E_VIDEO_RECORDING_DEVICE_INVALIDATED
            # 表现就是"用一会儿突然断掉"。所以这里直接定案返回。
            _FC["cap"] = cap
            _FC["cap_backend"] = bname
            _FC["cap_state"] = "running"
            _FC["read_fail"] = 0
            _FC["open_best"] = None
            return 0.0
        else:
            if cap is not None:
                try:
                    cap.release()
                except Exception:
                    pass
        # 每个后端之间留一点间隔，让界面有机会刷新
        return 0.05

    # ---- 读帧并投给推理线程 ----
    cap = _FC.get("cap")
    if cap is None:
        _FC["cap_state"] = "idle"
        return 0.0

    try:
        ok, frame = cap.read()
    except Exception as e:
        _FC["err"] = "读帧异常: %s" % e
        _FC["running"] = False
        _FC["cap_state"] = "idle"
        return None

    if not ok:
        n = _FC.get("read_fail", 0) + 1
        _FC["read_fail"] = n
        # ---- 运行时断流自动重连 ----
        #
        # 实测（用户日志 207 秒处）：
        #   OnReadSample() is called with error status: -1072873821
        #   = 0xC00D36C3 = MF_E_VIDEO_RECORDING_DEVICE_INVALIDATED
        # 这是 Windows 把设备判为失效（被其他程序抢占 / 休眠唤醒 / USB 抖动）。
        # 原实现只会一直 read 到 90 次然后报错，用户看到的是"用着用着就不动了"。
        # 做法：连续失败到阈值就释放旧句柄、回到 idle 重新打开。
        # 重连有次数上限，避免设备真的拔掉时无限重试。
        if n == FC_READFAIL_RECONNECT:
            cnt = _FC.get("reconnect", 0) + 1
            _FC["reconnect"] = cnt
            if cnt <= FC_RECONNECT_MAX:
                _FC["err"] = ("摄像头断流，正在重连（第 %d/%d 次）…"
                              % (cnt, FC_RECONNECT_MAX))
                old = _FC.pop("cap", None)
                if old is not None:
                    try:
                        old.release()
                    except Exception:
                        pass
                _FC["cap_state"] = "idle"
                _FC["read_fail"] = 0
                _FC["open_try"] = 0
                _FC["open_best"] = None
                return 0.2
            _FC["err"] = ("摄像头反复断流（已重连 %d 次），已停止。"
                          "请检查设备是否被其他程序占用、或 USB 连接是否稳定。"
                          % cnt)
            _FC["running"] = False
            _FC["cap_state"] = "idle"
            _facecap_preview_kill_timer()
            _fc_capture_kill_timer()
            return None
        return 0.01
    _FC["read_fail"] = 0
    # 读到帧说明连接健康，清零重连计数（避免累计误触发上限）
    if _FC.get("reconnect"):
        _FC["reconnect"] = 0
    frame = _fc_fit_frame(cv2, frame)

    # 投给推理线程（队列满就丢最旧的，保证总是最新帧）
    th = _FC.get("thread")
    if th is not None and hasattr(th, "in_q"):
        try:
            while th.in_q.full():
                th.in_q.get_nowait()
        except Exception:
            pass
        try:
            th.in_q.put_nowait(frame)
        except Exception:
            pass

    # 预览快照：帧来自本函数，landmarks 由 _facecap_tick 从推理结果补上
    _FC["last_frame"] = {"frame": frame, "lms": _FC.get("last_lms")}
    _FC["cap_frames"] = _FC.get("cap_frames", 0) + 1
    return 1.0 / 30.0


def _fc_capture_ensure_timer():
    if not bpy.app.timers.is_registered(_fc_capture_tick):
        bpy.app.timers.register(_fc_capture_tick, first_interval=0.0,
                                persistent=True)


def _fc_capture_kill_timer():
    if bpy.app.timers.is_registered(_fc_capture_tick):
        try:
            bpy.app.timers.unregister(_fc_capture_tick)
        except Exception:
            pass


def _fc_release_camera():
    """释放摄像头（必须显式做，否则设备一直被占）"""
    cap = _FC.pop("cap", None)
    _FC["cap_state"] = "idle"
    _FC["cap_backend"] = None
    _FC["read_fail"] = 0
    if cap is not None:
        try:
            cap.release()
        except Exception:
            pass


# ---------------------------------------------------------------- 窗口管理
def _fc_find_preview_win():
    """找到预览窗口（按 as_pointer 记录）"""
    ptr = _FC.get("preview_win")
    if not ptr:
        return None
    for w in bpy.context.window_manager.windows:
        if w.as_pointer() == ptr:
            return w
    return None


def _fc_setup_image_area(area):
    """把 area 配置成预览画面"""
    sp = area.spaces.active
    if sp.type != "IMAGE_EDITOR":
        try:
            area.type = "IMAGE_EDITOR"
            sp = area.spaces.active
        except Exception:
            return False
    try:
        sp.image = _fc_ensure_preview_image()
        sp.ui_mode = 'VIEW'
        # 确保 header 可见（否则用户找不到任何控件）
        if hasattr(sp, "show_region_header"):
            sp.show_region_header = True
    except Exception:
        pass
    return True


# ---------------------------------------------------------------- 操作符
class EZ_OT_facecap_window(bpy.types.Operator):
    bl_idname = "ez.facecap_window"
    bl_label = "开始面捕"
    bl_description = ("打开预览画面 + 启动摄像头追踪。"
                      "预览占用当前窗口的一个区域（不新建窗口）")
    bl_options = {"REGISTER"}

    def execute(self, context):
        sc = context.scene

        # ---- 1. 找一块区域显示预览 ----
        # 不新建窗口：实测 screen.area_dupli 在 GUI 里报
        #   invalid operator call 'SCREEN_OT_area_dupli'
        # wm.window_new 在后台 poll 失败；bpy.data.screens.new 会崩溃
        # （EXCEPTION_ACCESS_VIOLATION，实测）。
        # 因此改为"就地占用当前窗口的一个区域"，不依赖任何窗口操作符。
        win = context.window
        area = None
        # 优先找已经存在的预览区域
        for a in win.screen.areas:
            if a.type == 'IMAGE_EDITOR':
                sp = a.spaces.active
                if getattr(sp, "image", None) is not None and \
                        sp.image.name == FC_PREVIEW_IMG:
                    area = a
                    break
        # 其次找可以安全改类型的区域（避开主 3D 视口）
        if area is None:
            for want in ('FILE_EDITOR', 'INFO', 'OUTLINER',
                         'PROPERTIES', 'TEXT_EDITOR'):
                for a in win.screen.areas:
                    if a.type == want:
                        area = a
                        break
                if area is not None:
                    break
        # 最后才动 3D 视口（用户可随时切回）
        if area is None:
            for a in win.screen.areas:
                if a.type == 'VIEW_3D':
                    area = a
                    break
        if area is None:
            self.report({"ERROR"}, "找不到可用区域显示预览")
            return {"CANCELLED"}

        if not _fc_setup_image_area(area):
            self.report({"ERROR"}, "无法把该区域切换为图像编辑器")
            return {"CANCELLED"}

        _FC["preview_on"] = True
        _FC["preview_win"] = None
        _FC["preview_embedded_area"] = area.as_pointer()
        _facecap_preview_ensure_timer()

        # ---- 2. 启动面捕 ----
        if not _FC["running"]:
            rig = _facecap_rig_name(sc)
            if not rig:
                self.report({"ERROR"}, "未发现角色，请先导入带 rig_profile 的角色")
                return {"CANCELLED"}
            ok, msg = _facecap_start(rig, int(getattr(sc, "fc_cam", 0)),
                                     bool(getattr(sc, "fc_head", True)))
            if not ok:
                self.report({"ERROR"}, msg)
                return {"CANCELLED"}
            self.report({"INFO"}, msg)
        else:
            self.report({"INFO"}, "预览已打开（面捕已在运行）")
        _redraw()
        return {"FINISHED"}


class EZ_OT_facecap_close_window(bpy.types.Operator):
    bl_idname = "ez.facecap_close_window"
    bl_label = "关闭预览"
    bl_description = "停止预览刷新，并把被占用的区域恢复成 3D 视口"
    bl_options = {"REGISTER"}

    def execute(self, context):
        _FC["preview_on"] = False
        _facecap_preview_kill_timer()
        # 恢复被占用的区域（不关窗口：不依赖窗口操作符）
        ptr = _FC.pop("preview_embedded_area", None)
        restored = False
        if ptr:
            for w in bpy.context.window_manager.windows:
                for a in w.screen.areas:
                    if a.as_pointer() == ptr and a.type == 'IMAGE_EDITOR':
                        try:
                            a.type = 'VIEW_3D'
                            restored = True
                        except Exception:
                            pass
        _FC["preview_win"] = None
        _redraw()
        self.report({"INFO"}, "预览已关闭%s" % ("（区域已还原）" if restored
                                                else ""))
        return {"FINISHED"}


class EZ_OT_facecap_wstart(bpy.types.Operator):
    bl_idname = "ez.facecap_wstart"
    bl_label = "开始/停止面捕"
    bl_description = "开启 / 停止面捕"
    bl_options = {"REGISTER"}

    def execute(self, context):
        sc = context.scene
        if _FC["running"]:
            _facecap_stop()
            _redraw()
            self.report({"INFO"}, "已停止")
            return {"FINISHED"}
        rig = _facecap_rig_name(sc)
        if not rig:
            self.report({"ERROR"}, "未发现角色")
            return {"CANCELLED"}
        ok, msg = _facecap_start(rig, int(getattr(sc, "fc_cam", 0)),
                                 bool(getattr(sc, "fc_head", True)))
        if not ok:
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}
        _redraw()
        self.report({"INFO"}, msg)
        return {"FINISHED"}


class EZ_OT_facecap_wrec(bpy.types.Operator):
    bl_idname = "ez.facecap_wrec"
    bl_label = "录制"
    bl_description = "开始 / 停止录制"
    bl_options = {"REGISTER"}

    def execute(self, context):
        if _FC["recording"]:
            n = _facecap_rec_stop()
            self.report({"INFO"}, "已停止录制，缓冲 %d 帧" % n)
        else:
            rig = _facecap_rig_name(context.scene)
            ok, msg = _facecap_rec_start(rig)
            if not ok:
                self.report({"ERROR"}, msg)
                return {"CANCELLED"}
            self.report({"INFO"}, msg)
        _redraw()
        return {"FINISHED"}


class EZ_OT_facecap_wbake(bpy.types.Operator):
    bl_idname = "ez.facecap_wbake"
    bl_label = "烘焙到时间轴"
    bl_description = "把录制缓冲平滑+自适应抽帧后写成关键帧"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        sc = context.scene
        rig = _facecap_rig_name(sc)
        if not rig:
            self.report({"ERROR"}, "未发现角色")
            return {"CANCELLED"}
        if _FC["recording"]:
            _facecap_rec_stop()
        n_ch, n_key = _facecap_bake(
            rig,
            int(getattr(sc, "fc_mingap", 3)),
            float(getattr(sc, "fc_thresh", 0.06)),
            float(getattr(sc, "fc_bake_smooth", 0.0)),
            float(getattr(sc, "fc_beta", 0.007)),
            bool(getattr(sc, "fc_clear", True)))
        if n_key == 0:
            self.report({"WARNING"}, "缓冲为空或没有可映射的通道")
            return {"CANCELLED"}
        _redraw()
        self.report({"INFO"}, "已烘焙 %d 通道 / %d 关键帧" % (n_ch, n_key))
        return {"FINISHED"}


class EZ_OT_facecap_toggle_mirror(bpy.types.Operator):
    bl_idname = "ez.facecap_toggle_mirror"
    bl_label = "镜像"
    bl_description = "预览画面左右翻转（mask 同步翻转）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        sc = context.scene
        sc.fc_mirror = not bool(getattr(sc, "fc_mirror", True))
        _redraw()
        self.report({"INFO"}, "镜像 %s" % ("开" if sc.fc_mirror else "关"))
        return {"FINISHED"}


class EZ_OT_facecap_toggle_head(bpy.types.Operator):
    bl_idname = "ez.facecap_toggle_head"
    bl_label = "头部跟随"
    bl_description = "开/关头部跟随（TRACK_TO 手柄驱动）"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        sc = context.scene
        sc.fc_head = not bool(getattr(sc, "fc_head", True))
        _redraw()
        self.report({"INFO"}, "头部跟随 %s" % ("开" if sc.fc_head else "关"))
        return {"FINISHED"}


FACECAP_PROP_NAMES = ['fc_rig', 'fc_cam', 'fc_deps', 'fc_model', 'fc_live', 'fc_strength', 'fc_smooth', 'fc_beta', 'fc_head', 'fc_head_strength', 'fc_head_max', 'fc_head_yaw_sign', 'fc_head_pitch_sign', 'fc_head_roll', 'fc_head_roll_sign', 'fc_head_roll_strength', 'fc_head_roll_max', 'fc_mingap', 'fc_thresh', 'fc_bake_smooth', 'fc_clear', 'fc_mirror', 'fc_calib_sound', 'fc_wink_gain']


def _register_facecap_props():
    S = bpy.types.Scene
    # 目标角色：动态枚举，与插件其他下拉一致（运行时扫 rig_profile）
    S.fc_rig = bpy.props.EnumProperty(
        name="目标角色", items=_facecap_target,
        description="面捕驱动的角色（运行时自动发现）")
    S.fc_cam = bpy.props.IntProperty(name="摄像头", default=0, min=0, max=9)
    S.fc_deps = bpy.props.StringProperty(name="依赖目录", subtype="DIR_PATH", default="")
    S.fc_model = bpy.props.StringProperty(name="模型", subtype="FILE_PATH", default="")
    S.fc_live = bpy.props.BoolProperty(name="实时预览", default=True)
    S.fc_strength = bpy.props.FloatProperty(name="表情强度", default=1.0,
                                            min=0.1, max=2.0)
    S.fc_smooth = bpy.props.FloatProperty(name="平滑", default=1.0,
                                          min=0.1, max=10.0)
    S.fc_beta = bpy.props.FloatProperty(name="跟随灵敏度", default=0.007,
                                        min=0.0, max=0.1)
    S.fc_head = bpy.props.BoolProperty(name="跟随头部", default=True)
    S.fc_head_strength = bpy.props.FloatProperty(name="头部强度", default=1.0,
                                                 min=0.0, max=2.0)
    S.fc_head_max = bpy.props.FloatProperty(name="最大转角", default=45.0,
                                            min=5.0, max=85.0)
    S.fc_head_yaw_sign = bpy.props.FloatProperty(name="左右反向", default=1.0,
                                                 min=-1.0, max=1.0)
    # pitch 默认 -1：实测 mediapipe 的 euler X 在"抬头"时给负值，
    # 而驱动侧 pitch>0 才是抬头（见 _facework/pitch_dir.txt），需抵消。
    S.fc_head_pitch_sign = bpy.props.FloatProperty(name="上下反向",
                                                   default=-1.0,
                                                   min=-1.0, max=1.0)
    # ---- 歪头（roll）：TRACK_TO 之外的第三个自由度 ----
    S.fc_head_roll = bpy.props.BoolProperty(
        name="歪头跟随", default=True,
        description="跟随头部侧倾（需要先安装歪头约束）")
    # roll 默认 +1：与 mediapipe 输出的 roll 同号。
    # 推导（见 _facework/roll_sign.txt）：用户向左歪头 → 头顶倒向左肩 →
    # 画面里朝右倒 → 绕相机 Z 负转 → mediapipe roll 为负；而驱动侧
    # roll 参数为负正是"头向左倾"，两者同号。
    # 【注意】这条推导基于相机几何，不是实测（没有真人做已知方向歪头
    # 就无法测出 mediapipe 的符号）。曾按 pitch 的 -1 类推而写错，
    # 用户报告"方向反了"后改为 +1。若仍有偏差，面板「歪头反向」按钮一键翻转。
    S.fc_head_roll_sign = bpy.props.FloatProperty(
        name="歪头反向", default=1.0, min=-1.0, max=1.0)
    S.fc_head_roll_strength = bpy.props.FloatProperty(
        name="歪头强度", default=1.0, min=0.0, max=2.0)
    S.fc_head_roll_max = bpy.props.FloatProperty(
        name="歪头最大角", default=30.0, min=5.0, max=60.0)
    S.fc_mingap = bpy.props.IntProperty(name="最小间隔", default=3, min=1, max=30)
    S.fc_thresh = bpy.props.FloatProperty(name="变化阈值", default=0.06,
                                          min=0.001, max=0.5)
    S.fc_bake_smooth = bpy.props.FloatProperty(name="烘焙再平滑", default=0.0,
                                               min=0.0, max=10.0)
    S.fc_clear = bpy.props.BoolProperty(name="先清除旧帧", default=True)
    S.fc_mirror = bpy.props.BoolProperty(
        name="镜像预览", default=True,
        description="预览画面左右翻转（像照镜子）。mask 会同步翻转")
    S.fc_calib_sound = bpy.props.BoolProperty(
        name="校准提示音", default=True,
        description="校准阶段切换时播放系统提示音（用 winsound，不依赖音频文件）")
    # 单眼眨增益：mediapipe 单侧眼睑闭合的输出幅度天然低于双眼闭，
    # 加上校准归一化的压缩，表现为"单眼眨闭不紧"。只放大"单眼独有"分量，
    # 双眼闭（blink）不受影响。
    S.fc_wink_gain = bpy.props.FloatProperty(
        name="单眼眨增益", default=1.5, min=1.0, max=3.0,
        description="放大单眼眨的幅度（只影响 wink，不影响双眼闭）")


def _unregister_facecap_props():
    for n in FACECAP_PROP_NAMES:
        try:
            delattr(bpy.types.Scene, n)
        except Exception:
            pass


# ==================== FACECAP BLOCK END ====================



# ==================== 依赖定位重定向（分发版修复，自动生成）====================
# 原插件的依赖定位依赖开发机绝对路径 + 旧插件目录名，分发给用户后失效。
# 这里整体替换为 ez_deps 的实现：零绝对路径、多位置兜底、可手动指定。
try:
    from . import ez_deps as _EZD
except Exception:
    import ez_deps as _EZD

def _facecap_here():
    return _EZD._plugin_dir()

def _facecap_deps_candidates():
    return _EZD.candidates()

def _facecap_deps_dir():
    return _EZD.deps_dir()

def _facecap_looks_like_deps(path):
    return _EZD.looks_like_deps(path)

def _facecap_model_path():
    return _EZD.model_path()

def _facecap_deps_ok():
    s = _EZD.status()
    if s['ok']:
        return True, '依赖就绪（cv2 %s / mediapipe %s）' % (s['cv2'], s['mediapipe'])
    return False, s['hint'] or ('缺: ' + '、'.join(s['missing']))

_facecap_deps_status = _EZD.status


# ==================== 依赖安装操作符（分发版新增）====================
class EZ_OT_facecap_deps_install(bpy.types.Operator):
    bl_idname = "ez.facecap_deps_install"
    bl_label = "安装面捕依赖"
    bl_description = ("把 mediapipe / opencv 依赖部署到插件目录下。\n"
                      "可从本地目录或 zip 安装，也能自动查找现成依赖")
    bl_options = {"REGISTER", "INTERNAL"}

    src: bpy.props.StringProperty(name="依赖来源", subtype="FILE_PATH",
                                  description="依赖目录或 zip 包")
    mode: bpy.props.EnumProperty(
        name="方式",
        items=[("AUTO", "自动查找并部署", "扫描常见位置，找到现成依赖就复制过来"),
               ("PATH", "从指定目录/zip 部署", "手动指定依赖目录或 zip 包")],
        default="AUTO")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=460)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "mode")
        if self.mode == "PATH":
            layout.prop(self, "src")
        else:
            s = _EZD.status()
            layout.label(text="部署目标: %s" % s["target"])
            layout.label(text="约 273 MB，需要一点时间", icon="INFO")

    def execute(self, context):
        s = _EZD.status()
        if s["ok"]:
            self.report({"INFO"}, "依赖已就绪，无需安装")
            return {"FINISHED"}
        src = (self.src or "").strip('\"')
        if self.mode == "PATH" and src:
            if os.path.isdir(src):
                r = _EZD.deploy_from_dir(src)
            elif os.path.isfile(src) and src.lower().endswith(".zip"):
                r = _EZD.deploy_from_zip(src)
            else:
                self.report({"ERROR"}, "路径无效: %s" % src)
                return {"CANCELLED"}
        else:
            r = _auto_deploy_deps()
        for n in r.get("notes", []):
            self.report({"INFO"}, n)
        if not r.get("ok"):
            self.report({"ERROR"}, "安装未完成：%s" % "；".join(r.get("notes", ["未知"])))
            return {"CANCELLED"}
        self.report({"INFO"}, "依赖安装完成")
        _redraw()
        return {"FINISHED"}


def _auto_deploy_deps():
    """自动查找现成依赖并部署（全部相对推导，不依赖写死路径）"""
    out = {"ok": False, "notes": []}
    seen = set()
    for c in _EZD.candidates():
        if c in seen:
            continue
        seen.add(c)
        if os.path.isdir(c) and _EZD.looks_like_deps(c):
            r = _EZD.deploy_from_dir(c)
            if r.get("ok"):
                return r
            out["notes"] += r.get("notes", [])
    out["notes"].append("没找到现成依赖目录，请用「从指定目录/zip 部署」")
    return out


class EZ_OT_facecap_deps_open(bpy.types.Operator):
    bl_idname = "ez.facecap_deps_open"
    bl_label = "打开依赖目录"
    bl_options = {"REGISTER", "INTERNAL"}

    def execute(self, context):
        s = _EZD.status()
        d = s["deps"] if os.path.isdir(s["deps"]) else s["target"]
        try:
            os.makedirs(d, exist_ok=True)
            if hasattr(os, "startfile"):
                os.startfile(d)
            else:
                bpy.ops.wm.path_open(filepath=d)
        except Exception as e:
            self.report({"ERROR"}, "打不开目录: %s" % e)
            return {"CANCELLED"}
        return {"FINISHED"}
# ==================== 依赖定位重定向 END ====================

