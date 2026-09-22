# -*- coding: utf-8 -*-
# ---------------------------------------------------------------------------
# ez-v2b-animation  ——  作者：B站 @高压郭炖大葱
# 使用、修改、二次分发请保留本署名。
# ---------------------------------------------------------------------------

"""ez-v2b-animation :: 结构发现引擎（只读）

设计原则：**不写死任何骨骼名**。全部靠约束结构反查。

  1. IK 约束        -> IK 骨骼 / 手柄 / 极向 / 链长 / pole_angle / use_stretch
  2. COPY_ROTATION  -> 链末端旋转骨骼（手 / 脚）
  3. TRACK_TO       -> 头部被驱动骨骼 + 头部手柄
  4. 骨骼层级       -> 主体 / 手指 / 装饰 的分组建议
  5. 形态键         -> 面部网格 + 通道语义（VRC / MMD / 日文前缀）

实测依据（_autorig_work/probe_deep.txt，三套原始模型全部命中）：

  角色.blend   Armature      Karin      LHik RHik LFIK RFik / LHCik RHCik LFCik RFCik / Hik
  角色.blend   Armature.001  Chocolate  LHIK RHIK / LowerLeg.L.003 RLIK / LWAY RWAY / Headik
  Mafuyu.blend Armature      Mafuyu     LHIK RHIK / Lower_leg.L.001 Lower_leg.R.001 / Head.001

三套命名规则完全不同（`LFIK` / `LowerLeg.L.003` / `Lower_leg.L.001`），
但 IK 约束的**结构**完全一致：chain_count 3(臂)/2(腿)、手柄无父级、
极向独立于链、末端 COPY_ROTATION 指向同一手柄。
所以结构发现是唯一可靠路径，命名匹配必然翻车。
"""

import bpy
import re
import math
from mathutils import Vector

# ================================================================ 基础工具

_SPLIT = re.compile(r"[._\-\s]+")

# 装饰类骨骼关键词（头发 / 服饰 / 附件）。命中即归入装饰组并隐藏。
HAIR_KW = (
    "hair", "ahoge", "bang", "twintail", "ponytail", "sidehair", "fronthair",
    "hairpin", "hairline", "hairribbon", "hairroot",
    "发", "髪", "髪", "毛", "刘海", "呆毛", "双马尾",
)

DECO_KW = (
    # 服饰
    "skirt", "sleeve", "collar", "choker", "belt", "apron", "cloth", "cape",
    "tie", "bow", "frill", "sock", "shoe", "loafer", "boot", "glove", "spats",
    "jersey", "shirt", "bra", "underwear", "dress", "jacket", "coat", "pants",
    "katuysha", "katyusha", "ribbon", "sailorcollar", "bandage", "band-aid",
    # 附件 / 器官
    "wing", "tail", "ear", "horn", "cheek", "ribbon",
    # 特殊 / 装备
    "penis", "dick", "cock", "pussy", "breast", "bust", "nipple", "tentacle",
    "strap", "equip", "prop", "acc", "acce", "accessary", "accessory",
    # 中文
    "裙", "袖", "领", "带", "翅膀", "尾", "耳", "角", "袜", "鞋", "衣", "围裙",
    "飘带", "装饰", "饰", "服装", "上衣", "项圈", "发带",
)

FINGER_KW = (
    "thumb", "index", "middle", "ring", "little", "pinky", "pinkie", "finger",
    "thumb", "亲指", "人差", "中指", "薬指", "小指", "指",
)

# 人形骨骼语义别名（仅在模型**没有** IK 约束、需要从零建 IK 时使用）
HUMAN_ALIASES = {
    "hips":      ("hips", "pelvis", "root", "hip"),
    "spine":     ("spine", "spine1", "spine_1", "waist", "abdomen"),
    "chest":     ("chest", "spine2", "spine_2", "upperchest", "chest1", "chest_1"),
    "neck":      ("neck",),
    "head":      ("head",),
    "shoulder.L": ("leftshoulder", "shoulderl", "shoulder_l", "claviclel", "clavicle.l"),
    "shoulder.R": ("rightshoulder", "shoulderr", "shoulder_r", "clavicler", "clavicle.r"),
    "upperarm.L": ("leftupperarm", "upperarml", "upper_arml", "upperarm_l", "upperarm.l"),
    "upperarm.R": ("rightupperarm", "upperarmr", "upper_armr", "upperarm_r", "upperarm.r"),
    "lowerarm.L": ("leftlowerarm", "lowerarml", "lower_arml", "lowerarm_l", "lowerarm.l", "forearml"),
    "lowerarm.R": ("rightlowerarm", "lowerarmr", "lower_armr", "lowerarm_r", "lowerarm.r", "forearmr"),
    "hand.L":     ("lefthand", "handl", "hand_l", "hand.l", "wristl", "wrist.l"),
    "hand.R":     ("righthand", "handr", "hand_r", "hand.r", "wristr", "wrist.r"),
    "upperleg.L": ("leftupperleg", "upperlegl", "upper_legl", "upperleg_l", "upperleg.l", "thighl"),
    "upperleg.R": ("rightupperleg", "upperlegr", "upper_legr", "upperleg_r", "upperleg.r", "thighr"),
    "lowerleg.L": ("leftlowerleg", "lowerlegl", "lower_legl", "lowerleg_l", "lowerleg.l", "shinl", "calfl"),
    "lowerleg.R": ("rightlowerleg", "lowerlegr", "lower_legr", "lowerleg_r", "lowerleg.r", "shinr", "calfr"),
    "foot.L":     ("leftfoot", "footl", "foot_l", "foot.l", "anklel", "ankle.l"),
    "foot.R":     ("rightfoot", "footr", "foot_r", "foot.r", "ankler", "ankle.r"),
    "toe.L":      ("lefttoebase", "toel", "toe_l", "toe.l", "toebasel", "toesl"),
    "toe.R":      ("righttoebase", "toer", "toe_r", "toe.r", "toebaser", "toesr"),
}


def tokens(name):
    return [t for t in _SPLIT.split(name or "") if t]


def norm(name):
    """归一化：去掉大小写与分隔符差异，用于别名匹配"""
    s = (name or "").lower()
    for ch in ("_", ".", "-", " ", "　"):
        s = s.replace(ch, "")
    return s


def armatures():
    return [o for o in bpy.data.objects if o.type == "ARMATURE"]


def _wx(arm, bone_name):
    b = arm.data.bones.get(bone_name)
    if b is None:
        return None
    return (arm.matrix_world @ b.head_local)


# ================================================================ 左右 / 肢体判定

def side_of(arm, bone_name):
    """L / R / None。名字优先，几何兜底。

    'Lower_leg.L.001' -> tokens 含 'L'      -> L
    'LHIK'            -> 无 L token，几何 +X -> L
    'LeftLowerArm'    -> 前缀 left          -> L
    'Hand.L'          -> tokens 含 'L'      -> L
    """
    nm = bone_name or ""
    for t in tokens(nm):
        tl = t.lower()
        if tl in ("l", "left", "lf"):
            return "L"
        if tl in ("r", "right", "rt"):
            return "R"
    low = nm.lower()
    if low.startswith("left"):
        return "L"
    if low.startswith("right"):
        return "R"
    p = _wx(arm, nm)
    if p is None:
        return None
    return "L" if p.x >= 0.0 else "R"


_LEG_KW = ("leg", "thigh", "shin", "knee", "foot", "toe", "ankle", "腿", "脚", "足")
_ARM_KW = ("arm", "shoulder", "elbow", "wrist", "hand", "forearm", "臂", "腕", "手", "肘")


def limb_of(arm, ik_bone, tip_bone, hips_z):
    """arm / leg。名字优先，几何（末端高度 vs 髋高）兜底。"""
    s = norm(ik_bone) + "|" + norm(tip_bone)
    has_leg = any(k in s for k in _LEG_KW)
    has_arm = any(k in s for k in _ARM_KW)
    if has_leg and not has_arm:
        return "leg"
    if has_arm and not has_leg:
        return "arm"
    p = _wx(arm, tip_bone)
    if p is None:
        return None
    return "leg" if p.z < hips_z else "arm"


def _chain_names(arm, bone_name, count):
    """沿父级向上取 count 根（自末向根）"""
    out, b = [], arm.data.bones.get(bone_name)
    while b is not None and len(out) < max(1, int(count)):
        out.append(b.name)
        b = b.parent
    return out


def _descendant_depth(arm, name, limit=6):
    """BFS 求该骨骼的最小子孙深度（用于判定极向是否在链外）"""
    b = arm.data.bones.get(name)
    if b is None:
        return 999
    seen, frontier, d = {name}, [b], 0
    while frontier and d < limit:
        d += 1
        nxt = []
        for x in frontier:
            for c in x.children:
                if c.name in seen:
                    continue
                seen.add(c.name)
                if c.name == name:
                    return d
                nxt.append(c)
        frontier = nxt
    return 999


def _guess_head_bone(arm):
    """推一个头部骨骼：先用别名表，再退回名字精确匹配。

    注意 discover_ik() 里没有 human 表（它在 discover() 里才解析），
    所以这里自带一次轻量解析，不能引用外部变量。
    """
    # 1) 别名表
    pool = {}
    for b in arm.data.bones:
        pool.setdefault(norm(b.name), []).append(b.name)
    for a in HUMAN_ALIASES["head"]:
        c = pool.get(a)
        if c:
            return arm.data.bones.get(c[0])
    # 2) 名字精确匹配
    for b in arm.data.bones:
        if b.name.lower().strip() in ("head", "head.001", "head_1", "head_01"):
            return b
    # 3) 含 head 且是 Neck 的子级（最像头部的那根）
    cands = [b for b in arm.data.bones if "head" in b.name.lower()]
    if cands:
        for b in cands:
            if b.parent is not None and "neck" in b.parent.name.lower():
                return b
        return cands[0]
    return None


# ================================================================ 手柄发现（核心）

def discover_ik(arm):
    """扫约束反查 IK 结构。返回:
    {
      "limbs": {"arm": {"L": {...}, "R": {...}}, "leg": {...}},
      "head":  {"driver": "Head", "handle": "Hik"} | None,
      "raw_ik": [ {...} ],           # 原始 IK 约束条目
      "rot_map": {handle: [owner,...]},
      "notes": [...],
    }
    每个 limb 条目: {ik, handle, pole, chain, chain_count, pole_angle,
                     use_stretch, tip, rot_owner, side}
    """
    notes = []
    rot_map = {}
    for pb in arm.pose.bones:
        for c in pb.constraints:
            if c.type == "COPY_ROTATION" and c.subtarget:
                rot_map.setdefault(c.subtarget, []).append(pb.name)

    hips = None
    for alias in HUMAN_ALIASES["hips"]:
        for b in arm.data.bones:
            if norm(b.name) == alias and b.parent is None:
                hips = b
                break
        if hips:
            break
    hips_z = (arm.matrix_world @ hips.head_local).z if hips else 0.0

    raw, limbs = [], {"arm": {}, "leg": {}}
    for pb in arm.pose.bones:
        for c in pb.constraints:
            if c.type != "IK":
                continue
            handle = c.subtarget or ""
            pole = c.pole_subtarget or ""
            if not handle or arm.data.bones.get(handle) is None:
                notes.append("%s 的 IK 约束没有有效手柄，跳过" % pb.name)
                continue
            chain = _chain_names(arm, pb.name, c.chain_count)
            # 链末端：优先"被同一手柄 COPY_ROTATION 驱动"的骨骼，否则取 IK 骨骼的子级
            tip = None
            for owner in rot_map.get(handle, []):
                if owner not in chain:
                    tip = owner
                    break
            if tip is None:
                kids = [k.name for k in arm.data.bones[pb.name].children]
                tip = kids[0] if kids else pb.name
            side = side_of(arm, handle) or side_of(arm, pb.name)
            if side is None:
                notes.append("%s 无法判定左右，跳过" % pb.name)
                continue
            limb = limb_of(arm, pb.name, tip, hips_z)
            if limb is None:
                notes.append("%s 无法判定臂/腿，跳过" % pb.name)
                continue
            entry = {
                "ik": pb.name,
                "handle": handle,
                "pole": pole,
                "chain": chain,
                "chain_count": int(c.chain_count),
                "pole_angle": float(c.pole_angle),
                "use_stretch": bool(c.use_stretch),
                "use_tail": bool(c.use_tail),
                "tip": tip,
                "rot_owner": tip if tip in rot_map.get(handle, []) else None,
                "side": side,
                "limb": limb,
                "pole_in_chain": bool(pole) and pole in chain,
            }
            raw.append(entry)
            if side in limbs[limb]:
                notes.append("%s 的 %s 侧重复出现，保留先者" % (limb, side))
                continue
            limbs[limb][side] = entry

    # 头部：TRACK_TO 的 subtarget 就是头部手柄
    head = None
    for pb in arm.pose.bones:
        for c in pb.constraints:
            if c.type == "TRACK_TO" and c.subtarget and arm.data.bones.get(c.subtarget):
                head = {
                    "driver": pb.name,
                    "handle": c.subtarget,
                    "track_axis": c.track_axis,
                    "up_axis": c.up_axis,
                    "use_target_z": bool(c.use_target_z),
                    "influence": float(c.influence),
                    "from_scratch": False,
                }
                break
        if head:
            break

    # 没有 TRACK_TO 时：按人形语义推一个（纯 FK 骨架走这条）。
    # handle 留 None，由构建器新建。实测规则（Rusk_优化说明.md 三）：
    # 手柄沿 Head 骨骼的 rest 局部 Z 轴摆放，偏移 0.30 世界单位；
    # 摆在世界正前方会让 TRACK_TO 在静置时就低头 6°。
    if head is None:
        hb = _guess_head_bone(arm)
        if hb is not None:
            head = {
                "driver": hb.name,
                "handle": None,
                "track_axis": "TRACK_Z",
                "up_axis": "UP_Y",
                "use_target_z": False,
                "influence": 1.0,
                "from_scratch": True,
            }
            notes.append("未发现 TRACK_TO，将新建头部手柄（%s）" % hb.name)
    if head is None:
        notes.append("未发现 TRACK_TO 头部约束（该模型可能没有头部骨骼）")

    return {"limbs": limbs, "head": head, "raw_ik": raw,
            "rot_map": rot_map, "hips_z": hips_z, "notes": notes}


# ================================================================ 人形语义解析（无 IK 时的兜底）

def resolve_humanoid(arm):
    """按别名表解析人形骨骼语义。返回 {semantic: bone_name}（未命中的键不存在）"""
    pool = {}
    for b in arm.data.bones:
        pool.setdefault(norm(b.name), []).append(b.name)

    found = {}
    for semantic, aliases in HUMAN_ALIASES.items():
        for a in aliases:
            cands = pool.get(a)
            if not cands:
                continue
            pick = cands[0]
            if len(cands) > 1:
                # 多个同名候选时取"子孙最多"的（通常是真骨骼，而非 .001 副本）
                pick = max(cands, key=lambda n: len(_all_desc(arm, n)))
            found[semantic] = pick
            break
    return found


def _all_desc(arm, name, limit=400):
    b = arm.data.bones.get(name)
    if b is None:
        return []
    out, stack = [], list(b.children)
    while stack and len(out) < limit:
        x = stack.pop()
        out.append(x.name)
        stack.extend(x.children)
    return out


def find_root_bone(arm, ctrl_names=()):
    """主干根骨骼：无父级、子孙最多、且不是控制器"""
    best, bestn = None, -1
    for b in arm.data.bones:
        if b.parent is not None or b.name in ctrl_names:
            continue
        n = len(_all_desc(arm, b.name))
        if n > bestn:
            best, bestn = b, n
    return best.name if best else None


def forward_axis(arm, human):
    """角色前向（世界空间，水平分量）。脚踝->脚趾，兜底 -Y。"""
    fl, tl = human.get("foot.L"), human.get("toe.L")
    if fl and tl:
        a = _wx(arm, fl)
        b = _wx(arm, tl)
        if a is not None and b is not None:
            v = b - a
            v.z = 0.0
            if v.length > 1e-6:
                return v.normalized()
    return Vector((0.0, -1.0, 0.0))


# ================================================================ 骨骼分组建议

def classify_bones(arm, ik_info, human=None):
    """把骨骼分到 控制器 / 切换器 / 主体 / 手指 / 头发 / 装饰 六类。
    返回 {组名: [骨骼名,...]}（全部来自实际存在的骨骼，绝无虚构）"""
    ctrl, switch = set(), set()
    for limb in ("arm", "leg"):
        for side in ("L", "R"):
            e = ik_info["limbs"][limb].get(side)
            if not e:
                continue
            ctrl.add(e["handle"])
            if e["pole"]:
                ctrl.add(e["pole"])
    if ik_info["head"]:
        ctrl.add(ik_info["head"]["handle"])

    # 切换器骨骼（已存在的 IKFK_* 命名）
    for b in arm.data.bones:
        if norm(b.name).startswith("ikfk"):
            switch.add(b.name)

    # 手指：Hand 骨骼的子孙中命中手指关键词的
    hands = []
    for limb in ("arm",):
        for side in ("L", "R"):
            e = ik_info["limbs"][limb].get(side)
            if e:
                hands.append(e["tip"])
    if human:
        hands += [human.get("hand.L"), human.get("hand.R")]
    finger = set()
    for h in filter(None, hands):
        for d in _all_desc(arm, h):
            if any(k in norm(d) for k in FINGER_KW):
                finger.add(d)

    hair, deco, main = set(), set(), set()
    for b in arm.data.bones:
        n = b.name
        if n in ctrl or n in switch:
            continue
        if n in finger:
            continue
        nb = norm(n)
        if any(k in nb for k in (norm(k) for k in HAIR_KW)):
            hair.add(n)
            continue
        if any(k in nb for k in (norm(k) for k in DECO_KW)):
            deco.add(n)
            continue
        main.add(n)

    return {
        "ctrl": sorted(ctrl),
        "switch": sorted(switch),
        "main": sorted(main),
        "finger": sorted(finger),
        "hair": sorted(hair),
        "deco": sorted(deco),
    }


# ================================================================ 形态键发现

FACE_SECTION = ("------", "-----", "----", "===")
VRC_SILENT = ("vrc.v_sil", "vrc_v_sil", "vrc.v_pp")


def face_meshes(arm):
    """绑到该骨架、且带形态键的网格"""
    out = []
    for o in bpy.data.objects:
        if o.type != "MESH":
            continue
        if not any(m.type == "ARMATURE" and m.object == arm for m in o.modifiers):
            continue
        if o.data.shape_keys and len(o.data.shape_keys.key_blocks) > 1:
            out.append(o)
    return out


def discover_face(arm):
    """形态键发现。返回:
    {
      "mesh": 主面部网格名, "keys": [...], "channels": {逻辑通道: 键名},
      "scheme": "vrc" / "vrc_mmd" / "mmd", "sections": [...], "pool_size": n
    }
    """
    meshes = face_meshes(arm)
    if not meshes:
        return {"mesh": None, "keys": [], "channels": {}, "scheme": "none",
                "sections": [], "pool_size": 0}

    # 主面部网格 = VRC/MMD 表情键最多的那个（实测：Body 238 / Body.001 619 / Body 238）
    def score(o):
        names = [k.name for k in o.data.shape_keys.key_blocks]
        s = 0
        for n in names:
            ln = n.lower()
            if ln.startswith("vrc"):
                s += 3
            if n.startswith("VRC."):
                s += 3
            if n in ("笑い", "なごみ", "びっくり", "ウィンク", "まばたき"):
                s += 2
        return s

    main = max(meshes, key=score)
    names = [k.name for k in main.data.shape_keys.key_blocks]

    sections = [n for n in names if n.startswith(FACE_SECTION)]
    vrc = [n for n in names if n.lower().startswith("vrc")]
    mmd = [n for n in names if n.startswith("VRC.")]
    jp = [n for n in names
          if n not in ("Basis",) and not n.startswith(FACE_SECTION)
          and not n.lower().startswith("vrc") and not n.lower().startswith("kisekae")]

    if vrc and mmd:
        scheme = "vrc_mmd"
    elif vrc:
        scheme = "vrc"
    elif mmd:
        scheme = "mmd"
    else:
        scheme = "jp" if jp else "none"

    channels = _resolve_channels(names)
    return {
        "mesh": main.name,
        "keys": names,
        "channels": channels,
        "scheme": scheme,
        "sections": sections,
        "pool_size": len(names),
        "vrc_count": len(vrc),
        "mmd_count": len(mmd),
        "meshes": [o.name for o in meshes],
    }


# 逻辑通道 -> 候选键名（按优先级，**精确匹配优先**）。
#
# 三套命名体系都要覆盖，实测（_autorig_work/diag_user_model.txt）：
#
#   旧版 VRChat   vrc.blink_left / vrc.blink_right / vrc.looking_up / VRC.v_aa
#                 （Karin / Rusk 用这套）
#   VRM 0.x       vrc.blink / vrc.looking_up / vrc.v_aa
#                 （Chocolate 用这套，注意 blink 是单数）
#   VRM 1.0 标准  vrc.Blink / vrc.Looking Up / vrc.Looking Down / vrc.v_aa
#                 （用户的模型用这套：**首字母大写 + 带空格**）
#
# 踩过的坑：候选表里漏了 VRM 1.0 那套（带空格的），精确匹配全不命中，
# 于是走模糊兜底（关键词包含 + 取最短），把 blink_left / blink_right
# **都兜到了同一个 vrc.Blink 上** —— 表现为眨眼时左右眼同步动，
# 以及 look_up/look_down 落到幅度极小的键上，用户感觉"不稳定"。
CHANNEL_ALIASES = {
    # 左右眼分开的优先；只有单个 blink 时左右共用（VRM 0.x 就是单键）
    "blink_left":  ("vrc.blink_left", "vrc.Blink_L", "vrc.blink_L",
                    "blink_L", "blink_left", "eye_blink_1_L",
                    "vrc.blink", "vrc.Blink", "blink", "まばたき", "eye_まばたき"),
    "blink_right": ("vrc.blink_right", "vrc.Blink_R", "vrc.blink_R",
                    "blink_R", "blink_right", "eye_blink_1_R",
                    "vrc.blink", "vrc.Blink", "blink", "まばたき", "eye_まばたき"),
    "look_up":     ("vrc.looking_up", "vrc.Looking Up", "vrc_LookingUp",
                    "vrc.LookUp", "LookUp", "eye_瞳移動_上", "上"),
    "look_down":   ("vrc.looking_down", "vrc.Looking Down", "vrc_LookingDown",
                    "vrc.LookDown", "LookDown", "eye_瞳移動_下", "下"),
    "aa":          ("vrc.v_aa", "VRC.v_aa", "vrc.v_AA", "mouth_あ", "あ"),
    "ih":          ("vrc.v_ih", "VRC.v_ih", "vrc.v_IH", "mouth_い", "い"),
    "ou":          ("vrc.v_ou", "VRC.v_ou", "vrc.v_OU", "mouth_う", "う"),
    "ee":          ("vrc.v_e", "VRC.v_E", "vrc.v_E", "mouth_え", "え"),
    "oh":          ("vrc.v_oh", "VRC.v_oh", "vrc.v_OH", "mouth_お", "お"),
    "smile":       ("笑い", "vrc.Happy", "smile", "mouth_笑い", "eye_笑い",
                    "にこり", "にっこり"),
    "angry":       ("怒り", "vrc.Angry", "eyebrow_怒り", "mouth_怒り口",
                    "ジト目", "jitome"),
    "cry":         ("涙", "vrc.Sad", "泣", "extra_汗or涙1", "eye_うるうる"),
    "surprise":    ("びっくり", "vrc.Surprised", "surprise", "eye_驚く"),
    "nagomi":      ("なごみ", "vrc.Relaxed", "eye_nagomi"),
    "wink":        ("ウィンク", "vrc.Wink_L", "wink_L", "eye_ウインク1_L"),
    "wink_right":  ("ウィンク右", "vrc.Wink_R", "wink_R", "eye_ウインク1_R"),
    "blush":       ("照れ", "vrc.Blush", "extra_頬染め1", "extra_ピンクほっぺ"),
    "heart":       ("はぁと", "星目", "vrc.Heart", "eye_ハート"),
}


def _norm_key(name):
    """键名归一化：去空格/下划线/点，转小写。

    这样 'vrc.Looking Up' 与 'vrc.looking_up' 会归一到同一个 key，
    候选表只需写一种写法就能同时命中 VRM 0.x / 1.0 两套命名。
    """
    s = (name or "").lower()
    for ch in (" ", "_", ".", "-"):
        s = s.replace(ch, "")
    return s


def _resolve_channels(names):
    """按候选名顺序匹配。

    匹配顺序（这个顺序很关键，踩过坑）：
      1. 精确匹配（原样）
      2. **归一化匹配**（去空格/下划线/点 + 小写）—— 覆盖 VRM 0.x / 1.0 差异
      3. 模糊匹配（关键词包含，取最短）
      4. 去重：一个键被多个通道占用时，只保留第一个通道，
         其余通道回退到次优候选 —— 避免左右眼指向同一个键
    """
    exact = set(names)
    normed = {}
    for n in names:
        normed.setdefault(_norm_key(n), n)

    out = {}
    used = {}          # 键名 -> 占用它的通道
    for ch, cands in CHANNEL_ALIASES.items():
        hit = None
        # 1) 精确
        for c in cands:
            if c in exact:
                hit = c
                break
        # 2) 归一化
        if hit is None:
            for c in cands:
                k = _norm_key(c)
                if k in normed:
                    hit = normed[k]
                    break
        # 3) 模糊（取最短，最可能是基础键）
        if hit is None:
            for kw in cands[:1]:
                kk = _norm_key(kw)
                cand = [n for n in names
                        if kk and kk in _norm_key(n)
                        and not n.startswith(FACE_SECTION)]
                if cand:
                    hit = min(cand, key=len)
        if hit is None:
            continue
        # 4) 去重：已被别的通道占用就跳过（宁可缺，也不要两个通道打架）
        if hit in used:
            continue
        used[hit] = ch
        out[ch] = hit
    return out


# ================================================================ 顶层入口

def discover(arm):
    """一次完整的只读结构发现"""
    ik = discover_ik(arm)
    human = resolve_humanoid(arm)
    ctrl_names = set()
    for limb in ("arm", "leg"):
        for side in ("L", "R"):
            e = ik["limbs"][limb].get(side)
            if e:
                ctrl_names.add(e["handle"])
                if e["pole"]:
                    ctrl_names.add(e["pole"])
    if ik["head"]:
        ctrl_names.add(ik["head"]["handle"])

    groups = classify_bones(arm, ik, human)
    face = discover_face(arm)
    root = find_root_bone(arm, ctrl_names)
    scale = arm.matrix_world.to_scale()

    return {
        "armature": arm.name,
        "bones": len(arm.data.bones),
        "scale": tuple(round(v, 6) for v in scale),
        "uniform_scale": abs(scale.x - scale.y) < 1e-6 and abs(scale.y - scale.z) < 1e-6,
        "ik": ik,
        "human": human,
        "groups": groups,
        "face": face,
        "root": root,
        "forward": tuple(round(v, 4) for v in forward_axis(arm, human)),
        "has_ik": bool(ik["limbs"]["arm"] and ik["limbs"]["leg"]),
        "parented": arm.parent.name if arm.parent else None,
    }


def describe(rep):
    """把发现结果渲染成可读报告（用于面板与日志）"""
    L = []
    L.append("骨架 %s  骨骼 %d  缩放 %s  根=%s  前向=%s" % (
        rep["armature"], rep["bones"], rep["scale"], rep["root"], rep["forward"]))
    if rep["parented"]:
        L.append("  父级对象: %s" % rep["parented"])
    L.append("  IK 结构: %s" % ("已具备" if rep["has_ik"] else "缺失（需从零构建）"))
    for limb, cn in (("arm", "手臂"), ("leg", "腿")):
        for side in ("L", "R"):
            e = rep["ik"]["limbs"][limb].get(side)
            if not e:
                L.append("  %s %s: 未发现" % (cn, side))
                continue
            L.append("  %s %s: IK@%s <- 手柄 %s  极向 %s  chain=%d  pole=%.2f°  末端 %s%s" % (
                cn, side, e["ik"], e["handle"], e["pole"] or "-", e["chain_count"],
                math.degrees(e["pole_angle"]), e["tip"],
                "  链末端旋转已接" if e["rot_owner"] else "  链末端旋转缺失"))
    h = rep["ik"]["head"]
    L.append("  头部: %s" % (
        "%s <- 手柄 %s" % (h["driver"], h["handle"]) if h else "未发现 TRACK_TO"))
    g = rep["groups"]
    L.append("  分组: 控制器 %d / 切换器 %d / 主体 %d / 手指 %d / 头发 %d / 装饰 %d" % (
        len(g["ctrl"]), len(g["switch"]), len(g["main"]),
        len(g["finger"]), len(g["hair"]), len(g["deco"])))
    f = rep["face"]
    if f["mesh"]:
        L.append("  表情: 网格 %s  键 %d  scheme=%s  通道 %d 个" % (
            f["mesh"], f["pool_size"], f["scheme"], len(f["channels"])))
    else:
        L.append("  表情: 未发现形态键")
    for n in rep["ik"]["notes"]:
        L.append("  注意: %s" % n)
    return "\n".join(L)
