# -*- coding: utf-8 -*-
"""把 karin_ik_tools 的三大功能块移植进 ez_v2b_animation

三大块（行号来自 _ik_work/tools_src.py，已实测确认）：
  A. IK 控制      302-810    Snap / 切换 / 归位 / 显隐 / 装饰 / 扭转
  B. 表情+姿势槽  812-1182   形态键面板 / 一键表情 / 姿势槽 / 引擎切换
  C. 面捕         1183-4332  mediapipe 面捕全链路

移植时做的适配（**必须做，否则搬过去就是坏的**）：
  1. 命名空间 karin.* -> ez.*，KARIN_OT_ -> EZ_OT_，VIEW3D_PT_karin_ -> EZ_PT_
  2. 去掉 BUILTIN_RIGS 兜底表（ez 只认 profile；旧表里全是开发期角色的硬编码）
  3. 去掉 _load_rigs/_ensure_rigs/_rig/_rig_items 等重复定义（ez_discovery 已有）
  4. _facecap_here() 的路径解析要指向 ez 的依赖目录
  5. 删除已移除的 Lip Sync 残留引用

用法: python _autorig_work/port_karin.py
"""
import os
import re
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ASS = os.path.dirname(HERE)
KARIN_SRC = os.path.join(ASS, "_ik_work", "tools_src.py")
EZ_DIR = os.path.join(ASS, "ez_v2b_animation")

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


P("=" * 76)
P("移植 karin_ik_tools -> ez_v2b_animation")
P("=" * 76)

# tokenize 工具：剥离注释与字符串，用于"只查可执行代码"的自检。
# 必须定义在使用之前（踩过：定义在后面导致 NameError）。
import io as _io
import tokenize as _tk


def _code_only(src):
    """去掉注释与字符串字面量，只留可执行代码。

    为什么要这样：注释里的实测记录（如"不能用 chain[2]"）是设计依据，
    用朴素字符串匹配会把它们当成违规误报。
    """
    out = []
    try:
        for tok in _tk.generate_tokens(_io.StringIO(src).readline):
            if tok.type in (_tk.COMMENT, _tk.STRING):
                continue
            out.append(tok.string)
    except Exception:
        return src
    return " ".join(out)

src = open(KARIN_SRC, encoding="utf-8").read()
lines = src.split("\n")
P("源文件 %d 行" % len(lines))

# ---------------------------------------------------------------- 切片
# 注意：IK 控制块里用到的工具函数（_upd / _eval_matrix / _apply_world_rotation /
# _sides / _set_switch / _ensure_fk_truth / _ctrl_parent_xform / _set_ctrl_to_world）
# 定义在 191-300 行，位于 Snap 类之前。**必须一起搬**，否则搬过去就是缺依赖的死代码。
# 这条是自检抓出来的（第一版只切了 302-810，实测缺 2 个函数）。
def slice_lines(a, b):
    """取 [a, b] 行（1-based，含两端）"""
    return "\n".join(lines[a - 1:b])

BLOCKS = {
    # 166-300 是常量与 Snap 依赖的工具函数，并入 IK 控制块。
    # 实测踩过的坑：TWIST_CNAME / TWIST_DEFAULT 定义在 **166 行**，
    # 而最初从 191 行起切，常量定义整个丢了 —— 于是移植块里到处引用
    # TWIST_CNAME 却没有定义，面板一读就 AttributeError。
    #
    # 起点取 166（常量），**不取 160**：160-165 是纯注释，170-188 的
    # _rig / _has_switch 与 ez_bridge 里的同名函数重复，切进来会覆盖桥接层。
    "ik_control":  (166, 810,  "扭转常量 + 工具 + Snap / 切换 / 归位 / 显隐 / 装饰"),
    "face":        (812, 1182, "表情 + 姿势槽 + 引擎切换"),
    "facecap":     (1183, 4332, "面捕全链路（mediapipe）"),
}

parts = {}
for key, (a, b, label) in BLOCKS.items():
    body = slice_lines(a, b)
    parts[key] = body
    P("  切片 %-12s 行 %d-%d  %6d 字节  %s" % (key, a, b, len(body), label))

# ---------------------------------------------------------------- 适配
P("")
P("--- 命名空间适配 ---")

def adapt(text, key):
    t = text

    # 0) **清除开发机绝对路径**（最关键的一步）
    #
    # 原插件的 merge_facecap.py 会注入一行：
    #     FACECAP_HOME = r"D:\AA-工程文件\nsfwpro\basi\asslib\karin_ik_tools"
    # 那是给"blend 内嵌文本块"场景用的（文本块被 exec 时没有 __file__）。
    # 但它是依赖查找顺序里的**第一优先级**，一旦进了分发包：
    #   - 用户机器上该目录不存在 -> 静默跳过 -> 依赖解析退化
    #   - 更糟：若用户恰好有同名目录，会加载到错误的依赖
    # 分发包必须**零绝对路径**，全部靠"相对本插件目录"解析。
    t = re.sub(r'^\s*FACECAP_HOME\s*=\s*r?["\'].*?["\']\s*$', '', t, flags=re.M)
    # 任何残留的开发机盘符路径
    t = re.sub(r'r?["\'][A-Za-z]:\\\\[^"\']*?(AA-工程文件|asslib|nsfwpro)[^"\']*?["\']',
               '""', t)

    # 1) 操作符/面板 ID
    t = t.replace('bl_idname = "karin.', 'bl_idname = "ez.')
    t = t.replace('"karin.', '"ez.')
    t = t.replace("'karin.", "'ez.")
    t = re.sub(r'\bKARIN_OT_', 'EZ_OT_', t)
    t = re.sub(r'\bVIEW3D_PT_karin_', 'EZ_PT_', t)

    # 2) 面板分类统一到 EZ V2B
    t = t.replace('bl_category = "IK Control"', 'bl_category = "EZ V2B"')
    t = t.replace('bl_category = "Face"', 'bl_category = "EZ V2B"')
    t = t.replace('bl_category = "FaceCap"', 'bl_category = "EZ V2B"')

    # 3) 去掉 BUILTIN_RIGS 兜底表的引用（表本身不搬）
    t = re.sub(r'^\s*for bcfg in BUILTIN_RIGS\.values\(\):.*?break\s*$',
               '', t, flags=re.M | re.S)

    # 4) **依赖路径改造**：把硬编码的旧插件目录名换成 ez 自己的
    #    原代码：addons/karin_ik_tools  ->  addons/ez_v2b_animation
    #    以及候选链里对旧插件目录的引用
    t = t.replace('"addons", "karin_ik_tools"', '"addons", "ez_v2b_animation"')
    t = t.replace("'addons', 'karin_ik_tools'", "'addons', 'ez_v2b_animation'")
    t = t.replace('addons/karin_ik_tools', 'addons/ez_v2b_animation')

    # 5) **依赖定位重定向到 ez_deps**（分发版关键修复）
    #
    #    原插件的 _facecap_deps_candidates / _facecap_deps_dir / _facecap_here
    #    / _facecap_model_path / _facecap_looks_like_deps 都是给开发环境写的：
    #    依赖 FACECAP_HOME（开发机绝对路径）+ 硬编码 addons/karin_ik_tools。
    #    分发给用户后两条都失效 -> 摄像头起不来。
    #
    #    做法：在面捕块末尾追加"覆盖定义"，把这几个函数替换成 ez_deps 的实现。
    #    这样移植块可以保持原样，重跑移植也不会丢。
    if key == "facecap":
        t = _inject_deps_panel(t)
        t = _fix_camera_open(t)
        t += _DEPS_REDIRECT_BLOCK

    # 6) **命名隔离**：移植块里的工具函数名（_upd / _eval_matrix / _sides / _set_switch
    #    / _ensure_fk_truth / _ctrl_parent_xform / _set_ctrl_to_world / _apply_world_rotation）
    #    与 ez_builder / ez_ui 里的同名函数语义不同。加 p_ 前缀避免互相覆盖。
    if key == "ik_control":
        for fn in ("_upd", "_eval_matrix", "_apply_world_rotation", "_sides",
                   "_set_switch", "_ensure_fk_truth", "_ctrl_parent_xform",
                   "_set_ctrl_to_world"):
            t = re.sub(r'\b%s\b' % re.escape(fn), "p" + fn, t)
        t = _fix_forearm_lookup(t)

    return t


def _fix_forearm_lookup(text):
    """把移植块里所有 `chain[2]` 取前臂的写法，换成沿父级查找。

    实测踩过的坑（用户报"依旧显示无前臂骨"）：

      chain 是"IK 骨骼沿父级向上 chain_count 根"。
      chain_count=2 时 chain 只有 [Lowerarm_L, Upperarm_L] 两个元素，
      chain[2] 越界 -> fa 为 None -> 面板显示"无前臂骨"、
      操作符报"没有可处理的前臂"。

      上一轮我只修了 ez_builder.py 的 _ensure_twist，**漏了面板和操作符**
      （ez_port_ik_control.py 里有两处同样的写法），所以用户依然看到"无前臂骨"。

    修法：替换成 _ez_forearm(arm, cfg, side) —— 与 ez_builder._forearm_of 同源逻辑。
    """
    # 面板里那段
    old_panel = '''            for s in ("L", "R"):
                ac = cfg["arms"][s]
                chain = ac.get("chain") or []
                fa = chain[2] if len(chain) > 2 else None
                pb_fa = arm.pose.bones.get(fa) if fa else None'''
    new_panel = '''            for s in ("L", "R"):
                ac = cfg["arms"][s]
                fa = _ez_forearm(arm, ac)
                pb_fa = arm.pose.bones.get(fa) if fa else None'''
    if old_panel in text:
        text = text.replace(old_panel, new_panel, 1)
    else:
        raise RuntimeError("面板前臂查找替换失败：没找到锚点")

    # 操作符里那段
    old_op = '''            ac = cfg["arms"][s]
            chain = ac.get("chain") or []
            fa = chain[2] if len(chain) > 2 else None
            handle = ac.get("ik")'''
    new_op = '''            ac = cfg["arms"][s]
            fa = _ez_forearm(arm, ac)
            handle = ac.get("ik")'''
    if old_op in text:
        text = text.replace(old_op, new_op, 1)
    else:
        raise RuntimeError("操作符前臂查找替换失败：没找到锚点")

    # 注入辅助函数（放在模块级，供面板与操作符共用）
    helper = '''

# ---------------------------------------------------------------- 前臂定位
def _ez_forearm(arm, arm_cfg):
    """取前臂骨骼（扭转传递的挂载点）。

    这个函数被两个坑逼出来的（用户先后报"上臂旋转不生效" / "依旧显示无前臂骨"）：

      坑一：chain 是"IK 骨骼沿父级向上 chain_count 根"。
            chain_count=2 时只有 [Lowerarm, Upperarm] 两个元素，chain[2] 越界。
            原实现 `chain[2] if len(chain) > 2 else None` -> None -> 面板显示"无前臂骨"。

      坑二：**profile 的 `ik` 字段存的是手柄名，不是 IK 骨骼名**。
            （原插件 karin 的约定就是如此：ik = LHik 这种手柄）
            手柄上没有 IK 约束，拿它去找前臂必然失败。

    解析顺序（每一层都有实测依据）：
      1. profile["ik_bone"]      <- ez 新写的字段，明确就是 IK 约束所在骨骼
      2. profile["chain"][0]     <- chain 首项 = IK 骨骼（向上数的第一根）
      3. profile["ik"] 当骨骼名试   <- 兼容"ik 存 IK 骨骼"的写法
      4. profile["ik"] 当手柄名，在场景里反查哪个骨骼的 IK 约束指向它
      5. 都失败 -> 返回 None（调用方给提示）
    """
    if not arm_cfg:
        return None

    # 1) 明确字段
    n = arm_cfg.get("ik_bone")
    if n and arm.pose.bones.get(n) is not None:
        return n

    # 2) chain 首项
    chain = arm_cfg.get("chain") or []
    if chain and arm.pose.bones.get(chain[0]) is not None:
        return chain[0]

    # 3) ik 字段当骨骼名
    n = arm_cfg.get("ik")
    if n and arm.pose.bones.get(n) is not None:
        pb = arm.pose.bones[n]
        # 有 IK 约束 -> 它就是 IK 骨骼，直接用
        if any(c.type == "IK" for c in pb.constraints):
            return n
        # 没有 -> 可能是手柄，继续往下试

    # 4) ik 当手柄名，反查哪个骨骼的 IK 约束指向它
    if n:
        for pb in arm.pose.bones:
            for c in pb.constraints:
                if c.type == "IK" and c.subtarget == n:
                    return pb.name

    # 5) 兜底：把 ik 字段原样返回（调用方会判 None）
    return n or None
'''
    # 插在 TWIST_DEFAULT 定义之后（常量已随切片进来，锚点可靠）
    anchor = "TWIST_DEFAULT = 0.4"
    if anchor in text:
        text = text.replace(anchor, anchor + helper, 1)
    else:
        raise RuntimeError(
            "辅助函数注入失败：没找到 TWIST_DEFAULT 锚点"
            "（说明切片起点没覆盖到常量定义，检查 BLOCKS 的 ik_control 起点）")

    return text


def _inject_deps_panel(text):
    """在面捕面板的 draw() 开头插入"依赖状态"区块。

    分发给用户后最常见的问题就是"依赖没装 -> 摄像头起不来"。
    与其让用户猜，不如把状态和安装按钮直接摆在面板最上面。

    注入方式：**按行扫描**定位 `class EZ_PT_facecap` 的 draw 方法，
    在 `layout = self.layout` 之后插入。

    不用整段字符串匹配 —— 那种做法会因为缩进或内容有一字之差就静默失败
    （踩过：按钮没画出来，但自检全绿，用户在面板上找不到按钮）。
    """
    lines = text.split("\n")
    res = []
    in_panel = False
    in_draw = False
    done = False

    for line in lines:
        res.append(line)
        if line.startswith("class EZ_PT_facecap("):
            in_panel = True
            continue
        if in_panel and line.startswith("class ") \
                and not line.startswith("class EZ_PT_facecap("):
            in_panel = False
            in_draw = False
        if not in_panel or done:
            continue
        if line.strip().startswith("def draw(self, context):"):
            in_draw = True
            continue
        if in_draw and line.strip() == "layout = self.layout":
            res.extend(_DEPS_PANEL_LINES)
            done = True
            in_draw = False

    if not done:
        # 注入失败必须显式报出来，不能静默通过
        raise RuntimeError("依赖区块注入失败：没找到 EZ_PT_facecap.draw 的 layout 锚点")
    return "\n".join(res)


# 依赖状态区块（插在 draw 的 layout 之后）
_DEPS_PANEL_LINES = [
    "",
    "        # ---- 依赖状态（分发版新增：先把\"能不能跑\"讲清楚）----",
    "        _st = _EZD.status()",
    "        _db = layout.box()",
    "        _db.label(text=\"面捕依赖\", icon=\"PLUGIN\")",
    "        if _st[\"ok\"]:",
    "            _db.label(text=\"cv2 %s / mediapipe %s\" % (_st[\"cv2\"], _st[\"mediapipe\"]),",
    "                      icon=\"CHECKMARK\")",
    "        else:",
    "            _db.label(text=\"未就绪\", icon=\"ERROR\")",
    "            for _m in _st[\"missing\"][:3]:",
    "                _db.label(text=\"缺 %s\" % str(_m)[:58])",
    "            _r = _db.row(align=True)",
    "            _r.scale_y = 1.3",
    "            _r.operator(\"ez.facecap_deps_install\", text=\"安装面捕依赖\", icon=\"IMPORT\")",
    "            _r.operator(\"ez.facecap_deps_open\", text=\"\", icon=\"FILE_FOLDER\")",
    "            _db.label(text=\"依赖约 273 MB，与插件分开存放\", icon=\"INFO\")",
    "",
    "        # 提示：面板下方「目标与依赖」区可手动指定依赖目录 / 模型文件 / 摄像头序号，",
    "        # 顶部这里是自动检测结果，两者配合使用。",
]


def _fix_camera_open(text):
    """修复摄像头启动卡死的两个真缺陷（用户报"面捕无响应，卡在启动摄像头"）。

    实测根因（_autorig_work/diag_camera.txt）：

      摄像头 0  MSMF  构造 7.89s  isOpened=True   read=False   <- 打不开却报成功
               ANY   构造 7.44s  isOpened=True   read=False
               DSHOW 构造 0.08s  isOpened=False
               合计 15.42 秒，且**全程冻结主线程**

    缺陷一：只看 `isOpened()` 就认定可用。实测 `isOpened()==True` 但 `read()` 失败
            是常见情况（设备被占用 / 驱动半死）。原代码于是锁死在一个读不出帧的
            摄像头上，既不报错也不退出 —— 这就是"卡在启动摄像头"。
    缺陷二：三个后端在**主线程的单个 timer 回调里串行同步尝试**，最坏 15 秒
            界面完全冻结，且没有取消入口，用户只能强杀 Blender。

    修法：
      1. 判定标准改成「实际读到一帧」才算可用
      2. 后端尝试改成**逐 tick 推进**（每个 tick 只试一个后端），
         主线程最多被冻一个后端的耗时，界面能刷新、能取消
      3. 加总超时（OPEN_TIMEOUT），超时给出明确报错而不是无限等
    """
    # ---- 1. 替换 _fc_capture_tick 的 idle 分支（后端探测逻辑）
    old_idle = '''    # ---- 打开摄像头 ----
    if state == "idle":
        _FC["cap_state"] = "opening"
        cap = None
        used = None
        for bname, bid in (("MSMF", cv2.CAP_MSMF), ("ANY", cv2.CAP_ANY),
                           ("DSHOW", cv2.CAP_DSHOW)):
            try:
                c = cv2.VideoCapture(int(_FC.get("cam_index", 0)), bid)
            except Exception:
                continue
            if c.isOpened():
                cap = c
                used = bname
                break
            try:
                c.release()
            except Exception:
                pass
        if cap is None:
            _FC["err"] = "摄像头打不开（试过 MSMF/ANY/DSHOW），可能被占用"
            _FC["running"] = False
            _FC["cap_state"] = "idle"
            _facecap_preview_kill_timer()
            return None
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, FC_PREVIEW_W)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FC_PREVIEW_H)
        _FC["cap"] = cap
        _FC["cap_backend"] = used
        _FC["cap_state"] = "running"
        _FC["read_fail"] = 0
        return 0.0
'''
    new_idle = '''    # ---- 打开摄像头（逐 tick 推进，避免主线程长时间冻结）----
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
'''
    if old_idle in text:
        text = text.replace(old_idle, new_idle, 1)
    else:
        raise RuntimeError("摄像头打开逻辑替换失败：没找到 idle 分支锚点")

    # ---- 2. 加超时常量（按行插入，不依赖整段字符串匹配）
    if "FC_OPEN_TIMEOUT =" not in text:
        lines = text.split("\n")
        out = []
        done = False
        for ln in lines:
            out.append(ln)
            if not done and ln.startswith("FACECAP_MODEL_NAME"):
                out.append("")
                out.append("# 摄像头打开的总超时（秒）。实测 MSMF 构造约 8s、ANY 约 7s，")
                out.append("# 三个后端串行最坏 15s+。给 25s 余量，超时明确报错而不是无限等。")
                out.append("FC_OPEN_TIMEOUT = 25.0")
                done = True
        if not done:
            raise RuntimeError("超时常量注入失败：没找到 FACECAP_MODEL_NAME 锚点")
        text = "\n".join(out)

    # ---- 3. 面板上显示"正在打开摄像头"的进度与取消入口
    old_status = '''        # ---- 主按钮：一键全开 ----
        box = layout.box()
        col = box.column(align=True)
        col.scale_y = 1.6
        if _FC["running"]:
            col.operator("ez.facecap_window", text="打开预览窗口",
                         icon="VIEW_CAMERA")
        else:
            col.operator("ez.facecap_window",
                         text="开始面捕（打开预览窗口）", icon="PLAY")
'''
    new_status = '''        # ---- 主按钮：一键全开 ----
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
'''
    if old_status in text:
        text = text.replace(old_status, new_status, 1)

    # ---- 4. 运行时断流自动重连（处理 0xC00D36C3 设备失效）----
    #
    # 实测（用户日志 207 秒处）：
    #   OnReadSample() is called with error status: -1072873821
    #   -1072873821 = 0xC00D36C3 = MF_E_VIDEO_RECORDING_DEVICE_INVALIDATED
    # Windows 侧把设备判为失效（被抢占 / 休眠唤醒 / USB 掉线）。
    # 原实现只是不断 read 直到 90 次后报错，用户看到"用着用着就不动了"。
    old_fail = '''    if not ok:
        n = _FC.get("read_fail", 0) + 1
        _FC["read_fail"] = n
        if n == 90:
            _FC["err"] = ("摄像头读不到帧（连续 %d 次，后端 %s）。"
                          "可能被其他程序占用。"
                          % (n, _FC.get("cap_backend")))
        return 0.01
'''
    new_fail = '''    if not ok:
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
'''
    if old_fail in text:
        text = text.replace(old_fail, new_fail, 1)
    else:
        raise RuntimeError("读帧失败分支替换失败：没找到锚点")

    # 读到帧时清零重连计数
    old_ok = '    _FC["read_fail"] = 0\n    frame = _fc_fit_frame(cv2, frame)'
    new_ok = ('    _FC["read_fail"] = 0\n'
              '    # 读到帧说明连接健康，清零重连计数（避免累计误触发上限）\n'
              '    if _FC.get("reconnect"):\n'
              '        _FC["reconnect"] = 0\n'
              '    frame = _fc_fit_frame(cv2, frame)')
    if old_ok in text:
        text = text.replace(old_ok, new_ok, 1)

    # ---- 5. 重连常量
    if "FC_RECONNECT_MAX =" not in text:
        lines = text.split("\n")
        out = []
        done = False
        for ln in lines:
            out.append(ln)
            if not done and ln.startswith("FC_OPEN_TIMEOUT"):
                out.append("")
                out.append("# 运行时断流自动重连。实测 0xC00D36C3 是设备被判失效，")
                out.append("# 释放句柄重新打开即可恢复；给 3 次机会，避免设备真拔掉时无限重试。")
                out.append("FC_READFAIL_RECONNECT = 30     # 连续失败多少帧后触发重连")
                out.append("FC_RECONNECT_MAX = 3           # 最多重连几次")
                done = True
        if not done:
            raise RuntimeError("重连常量注入失败：没找到 FC_OPEN_TIMEOUT 锚点")
        text = "\n".join(out)

    return text


_DEPS_REDIRECT_BLOCK = (
    "\n\n"
    "# ==================== 依赖定位重定向（分发版修复，自动生成）====================\n"
    "# 原插件的依赖定位依赖开发机绝对路径 + 旧插件目录名，分发给用户后失效。\n"
    "# 这里整体替换为 ez_deps 的实现：零绝对路径、多位置兜底、可手动指定。\n"
    "try:\n"
    "    from . import ez_deps as _EZD\n"
    "except Exception:\n"
    "    import ez_deps as _EZD\n"
    "\n"
    "def _facecap_here():\n"
    "    return _EZD._plugin_dir()\n"
    "\n"
    "def _facecap_deps_candidates():\n"
    "    return _EZD.candidates()\n"
    "\n"
    "def _facecap_deps_dir():\n"
    "    return _EZD.deps_dir()\n"
    "\n"
    "def _facecap_looks_like_deps(path):\n"
    "    return _EZD.looks_like_deps(path)\n"
    "\n"
    "def _facecap_model_path():\n"
    "    return _EZD.model_path()\n"
    "\n"
    "def _facecap_deps_ok():\n"
    "    s = _EZD.status()\n"
    "    if s['ok']:\n"
    "        return True, '依赖就绪（cv2 %s / mediapipe %s）' % (s['cv2'], s['mediapipe'])\n"
    "    return False, s['hint'] or ('缺: ' + '、'.join(s['missing']))\n"
    "\n"
    "_facecap_deps_status = _EZD.status\n"
    "\n"
    "\n"
    "# ==================== 依赖安装操作符（分发版新增）====================\n"
    "class EZ_OT_facecap_deps_install(bpy.types.Operator):\n"
    "    bl_idname = \"ez.facecap_deps_install\"\n"
    "    bl_label = \"安装面捕依赖\"\n"
    "    bl_description = (\"把 mediapipe / opencv 依赖部署到插件目录下。\\n\"\n"
    "                      \"可从本地目录或 zip 安装，也能自动查找现成依赖\")\n"
    "    bl_options = {\"REGISTER\", \"INTERNAL\"}\n"
    "\n"
    "    src: bpy.props.StringProperty(name=\"依赖来源\", subtype=\"FILE_PATH\",\n"
    "                                  description=\"依赖目录或 zip 包\")\n"
    "    mode: bpy.props.EnumProperty(\n"
    "        name=\"方式\",\n"
    "        items=[(\"AUTO\", \"自动查找并部署\", \"扫描常见位置，找到现成依赖就复制过来\"),\n"
    "               (\"PATH\", \"从指定目录/zip 部署\", \"手动指定依赖目录或 zip 包\")],\n"
    "        default=\"AUTO\")\n"
    "\n"
    "    def invoke(self, context, event):\n"
    "        return context.window_manager.invoke_props_dialog(self, width=460)\n"
    "\n"
    "    def draw(self, context):\n"
    "        layout = self.layout\n"
    "        layout.prop(self, \"mode\")\n"
    "        if self.mode == \"PATH\":\n"
    "            layout.prop(self, \"src\")\n"
    "        else:\n"
    "            s = _EZD.status()\n"
    "            layout.label(text=\"部署目标: %s\" % s[\"target\"])\n"
    "            layout.label(text=\"约 273 MB，需要一点时间\", icon=\"INFO\")\n"
    "\n"
    "    def execute(self, context):\n"
    "        s = _EZD.status()\n"
    "        if s[\"ok\"]:\n"
    "            self.report({\"INFO\"}, \"依赖已就绪，无需安装\")\n"
    "            return {\"FINISHED\"}\n"
    "        src = (self.src or \"\").strip('\\\"')\n"
    "        if self.mode == \"PATH\" and src:\n"
    "            if os.path.isdir(src):\n"
    "                r = _EZD.deploy_from_dir(src)\n"
    "            elif os.path.isfile(src) and src.lower().endswith(\".zip\"):\n"
    "                r = _EZD.deploy_from_zip(src)\n"
    "            else:\n"
    "                self.report({\"ERROR\"}, \"路径无效: %s\" % src)\n"
    "                return {\"CANCELLED\"}\n"
    "        else:\n"
    "            r = _auto_deploy_deps()\n"
    "        for n in r.get(\"notes\", []):\n"
    "            self.report({\"INFO\"}, n)\n"
    "        if not r.get(\"ok\"):\n"
    "            self.report({\"ERROR\"}, \"安装未完成：%s\" % \"；\".join(r.get(\"notes\", [\"未知\"])))\n"
    "            return {\"CANCELLED\"}\n"
    "        self.report({\"INFO\"}, \"依赖安装完成\")\n"
    "        _redraw()\n"
    "        return {\"FINISHED\"}\n"
    "\n"
    "\n"
    "def _auto_deploy_deps():\n"
    "    \"\"\"自动查找现成依赖并部署（全部相对推导，不依赖写死路径）\"\"\"\n"
    "    out = {\"ok\": False, \"notes\": []}\n"
    "    seen = set()\n"
    "    for c in _EZD.candidates():\n"
    "        if c in seen:\n"
    "            continue\n"
    "        seen.add(c)\n"
    "        if os.path.isdir(c) and _EZD.looks_like_deps(c):\n"
    "            r = _EZD.deploy_from_dir(c)\n"
    "            if r.get(\"ok\"):\n"
    "                return r\n"
    "            out[\"notes\"] += r.get(\"notes\", [])\n"
    "    out[\"notes\"].append(\"没找到现成依赖目录，请用「从指定目录/zip 部署」\")\n"
    "    return out\n"
    "\n"
    "\n"
    "class EZ_OT_facecap_deps_open(bpy.types.Operator):\n"
    "    bl_idname = \"ez.facecap_deps_open\"\n"
    "    bl_label = \"打开依赖目录\"\n"
    "    bl_options = {\"REGISTER\", \"INTERNAL\"}\n"
    "\n"
    "    def execute(self, context):\n"
    "        s = _EZD.status()\n"
    "        d = s[\"deps\"] if os.path.isdir(s[\"deps\"]) else s[\"target\"]\n"
    "        try:\n"
    "            os.makedirs(d, exist_ok=True)\n"
    "            if hasattr(os, \"startfile\"):\n"
    "                os.startfile(d)\n"
    "            else:\n"
    "                bpy.ops.wm.path_open(filepath=d)\n"
    "        except Exception as e:\n"
    "            self.report({\"ERROR\"}, \"打不开目录: %s\" % e)\n"
    "            return {\"CANCELLED\"}\n"
    "        return {\"FINISHED\"}\n"
    "# ==================== 依赖定位重定向 END ====================\n"
)

for key in parts:
    before = parts[key]
    parts[key] = adapt(before, key)
    P("  %-12s 已适配（%d -> %d 字节）" % (key, len(before), len(parts[key])))

# 依赖面板注入必须成功（失败时 adapt 会抛异常，这里再确认一次产物）
_fc = parts["facecap"]
_inj_ok = ("面捕依赖" in _fc) and ('ez.facecap_deps_install' in _fc)
chk(_inj_ok, "依赖状态区块已注入面捕面板 draw")

# 摄像头启动卡死的修复必须生效（用户实测报的"卡在启动摄像头"）
# 摄像头启动卡死的修复必须生效（用户实测报的"卡在启动摄像头"）
chk("FC_OPEN_TIMEOUT" in _fc, "摄像头打开超时常量已注入")
chk("必须实际读到一帧才算可用" in _fc, "摄像头可用性判定已改为「实际读到帧」")
chk("_FC[\"open_try\"]" in _fc, "后端尝试已改为逐 tick 推进")
chk("正在打开摄像头" in _fc, "面板已加打开进度反馈")
chk("取消打开" in _fc, "面板已加取消入口")
chk("一旦找到可用后端就立即定案" in _fc, "找到可用后端立即定案（不再反复 reopen）")
chk("FC_RECONNECT_MAX = 3" in _fc, "断流重连常量已注入")
chk("MF_E_VIDEO_RECORDING_DEVICE_INVALIDATED" in _fc, "断流重连逻辑已注入")
# 反向：不该再有"只看 isOpened 就 break"的老逻辑
chk("if c.isOpened():\n                cap = c" not in _fc,
    "已移除「只看 isOpened 就认定成功」的老逻辑")

# ---- 前臂定位：移植块里所有 chain[2] 都必须换成 _ez_forearm ----
# 注意：只查**可执行代码**，注释里说明"为什么不能用 chain[2]"是设计依据，要保留。
_ikc = parts["ik_control"]
chk("def _ez_forearm(" in _ikc, "前臂定位辅助函数已注入面板模块")
# 剥离注释后查：chain[2] 不得出现在代码层
# （用 tokenize 拼出的代码里，函数调用会变成 `_ez_forearm ( arm , ac )` 带空格，
#   所以这里查函数名本身而不是完整调用式）
_ikc_code = _code_only(_ikc)
chk("chain[2]" not in _ikc_code,
    "面板/操作符代码层无 chain[2]（用户报「无前臂骨」的根因）")
chk("_ez_forearm" in _ikc_code, "面板与操作符已改用 _ez_forearm")
# 常量必须随切片进来（最初从 191 行起切时丢了，导致 AttributeError）
chk('TWIST_CNAME = "扭转传递"' in _ikc, "TWIST_CNAME 常量已随切片进来")
chk("TWIST_DEFAULT = 0.4" in _ikc, "TWIST_DEFAULT 常量已随切片进来")
# 正向确认注释里保留了设计依据（查原文，不是剥离后的代码）
chk("这个函数被两个坑逼出来的" in _ikc,
    "注释里保留了设计依据（两个坑的实测记录）")
chk("profile 的 `ik` 字段存的是手柄名" in _ikc,
    "注释里记录了 profile 字段语义这个坑")# 反向：不该再有"存候选后继续试下一个"的逻辑
chk('_FC["open_best"] = cap' not in _fc,
    "已移除「存候选后继续试下一个」的逻辑（会导致设备反复 reopen）")
if _inj_ok:
    # 确认注入位置正确：必须在 draw 的 layout 之后、且缩进 8 空格
    _lines = _fc.split("\n")
    _pos_draw = None
    _pos_inj = None
    for _i, _l in enumerate(_lines):
        if "class EZ_PT_facecap(" in _l:
            _pos_draw = _i
        if _pos_draw is not None and "面捕依赖" in _l and _pos_inj is None:
            _pos_inj = _i
    chk(_pos_inj is not None and _pos_draw is not None and _pos_inj > _pos_draw,
        "依赖区块位于 EZ_PT_facecap 类内部（行 %s > %s）" % (_pos_inj, _pos_draw))
    if _pos_inj is not None:
        _ind = len(_lines[_pos_inj]) - len(_lines[_pos_inj].lstrip())
        chk(_ind == 8, "依赖区块缩进 8 空格（实为 %d）" % _ind)

# ---------------------------------------------------------------- 反向自检
P("")
P("--- 适配结果自检 ---")
all_txt = "\n".join(parts.values())
chk('"karin.' not in all_txt, '无残留 karin.* 操作符 ID')
chk("KARIN_OT_" not in all_txt, "无残留 KARIN_OT_ 类名")
chk("VIEW3D_PT_karin_" not in all_txt, "无残留 VIEW3D_PT_karin_ 类名")
chk('bl_category = "IK Control"' not in all_txt, '无残留 IK Control 分类')
chk("BUILTIN_RIGS" not in all_txt, "无残留 BUILTIN_RIGS 引用")

# **绝对路径红线**：分发包里不允许出现开发机盘符路径
abs_hits = re.findall(r'[A-Za-z]:\\\\[^"\'\s]*', all_txt)
abs_hits = [h for h in abs_hits if "AA-工程文件" in h or "nsfwpro" in h or "asslib" in h]
chk(not abs_hits, "无开发机绝对路径（命中 %s）" % (abs_hits[:3] if abs_hits else "0"))
chk("FACECAP_HOME = r" not in all_txt, "无 FACECAP_HOME 硬编码注入")


_code_txt = "\n".join(_code_only(v) for v in parts.values())
chk("karin_ik_tools" not in _code_txt, "代码层无 karin_ik_tools 路径引用")
chk("karin_ikfk_tools" not in _code_txt, "代码层无 karin_ikfk_tools 引用")
# 关键功能必须都在
for probe, label in (
        ("_ensure_fk_truth", "FK 真值烘焙"),
        ("_set_ctrl_to_world", "控制器世界定位"),
        ("snap_fk_to_ik", "FK->IK Snap"),
        ("snap_ik_to_fk", "IK->FK Snap"),
        ("_face_set", "形态键写入"),
        ("_face_keyframe", "表情 K 帧"),
        ("_capture_pose", "姿势捕获"),
        ("_facecap_discover_head", "头部结构发现"),
        ("_facecap_calib_head", "头部标定"),
        ("_facecap_bake", "面捕烘焙"),
        ("_fc_calib_begin", "两阶段校准"),
        ("_fc_beep", "提示音"),
        ("_OneEuro", "One Euro 滤波"),
):
    chk(probe in all_txt, "功能在: %s（%s）" % (label, probe))

# ---------------------------------------------------------------- 写文件
P("")
P("--- 写出移植文件 ---")

# 桥接头：把原插件期望的共享名字从 ez_bridge 注入本模块命名空间。
# 移植块里引用 _rig / _ensure_rigs / _upd / _face_meshes 等，全部由桥接层提供。
BRIDGE_HEAD = (
    "# -*- coding: utf-8 -*-\n"
    "# ---------------------------------------------------------------------------\n"
    "# ez-v2b-animation  ——  作者：B站 @高压郭炖大葱\n"
    "# 使用、修改、二次分发请保留本署名。\n"
    "# ---------------------------------------------------------------------------\n"
    "# 由 port_karin.py 从 karin_ik_tools 移植并适配（勿手改，改 port_karin.py 重跑）\n"
    "# 命名空间: karin.* -> ez.*  /  KARIN_OT_ -> EZ_OT_  /  面板分类 -> EZ V2B\n"
    "# 共享工具由 ez_bridge 注入（_rig / _ensure_rigs / _upd / _face_meshes 等）\n"
    "\n"
    "import bpy\n"
    "import math\n"
    "import json\n"
    "import os\n"
    "import re\n"
    "import sys\n"
    "import time\n"
    "import wave\n"
    "import queue\n"
    "import threading\n"
    "import subprocess\n"
    "import numpy as np\n"
    "from mathutils import Matrix, Vector, Quaternion, Euler\n"
    "\n"
    "try:\n"
    "    from .ez_bridge import *\n"
    "    from .ez_bridge import D, M, B, RIGS, SIDE_ITEMS\n"
    "except Exception:\n"
    "    from ez_bridge import *\n"
    "    from ez_bridge import D, M, B, RIGS, SIDE_ITEMS\n"
    "\n"
)

for key in ("ik_control", "face"):
    out = os.path.join(EZ_DIR, "ez_port_%s.py" % key)
    open(out, "w", encoding="utf-8", newline="\n").write(
        BRIDGE_HEAD + parts[key] + "\n")
    P("  已写 %s  %d 字节" % (os.path.basename(out), os.path.getsize(out)))

out = os.path.join(EZ_DIR, "ez_port_facecap.py")
open(out, "w", encoding="utf-8", newline="\n").write(
    BRIDGE_HEAD + parts["facecap"] + "\n")
P("  已写 %s  %d 字节" % (os.path.basename(out), os.path.getsize(out)))

P("")
P("=" * 76)
if fails:
    P("自检失败 %d 项:" % len(fails))
    for f in fails:
        P("  - %s" % f)
else:
    P("移植与适配全部通过")
P("=" * 76)

text = "\n".join(log)
open(os.path.join(HERE, "port_karin.txt"), "w", encoding="utf-8").write(text)
print(text)
sys.stdout.flush()
if fails:
    sys.exit(1)
