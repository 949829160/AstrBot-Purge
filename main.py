# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 YG
"""astrbot-purge runtime guard (plugin layer).

职责（与核心补丁互补，AstrBot 升级后核心补丁可能失效，这一层仍能兜住）：

1. on_llm_request：system prompt 里还没有破甲正文时，把
   ``astrbot-purge/prompt-inject.md`` 追加到**尾部**；
   人格（如「无敌」）已经自带破甲正文时**不重复注入**。
2. 清除 prompt 中残留的 vendor Safe Mode 段落（核心补丁被升级覆盖时会出现）。
3. ``/purge status|reload|on|off``：热重载注入文件、临时开关，无需重启后端。

注入源候选（按顺序取第一个存在的）：
  1. ``<AstrBot root>/astrbot-purge/prompt-inject.md``   ← 打包桌面版 = ~/.astrbot
  2. ``<install>/astrbot-purge/prompt-inject.md``        ← 源码树 / 安装目录
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

import json

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.provider import ProviderRequest
from astrbot.core import logger

FRAME_MARKER = "<!-- astrbot-purge:frame -->"
V4_MARKER = "<!-- astrbot-purge:v4 -->"

# 上游拒答话术（插件层兜底用，与核心 P07 同一套判定）
REFUSAL_PATTERN = re.compile(
    r"(?:i'?(?:m| am) sorry[,\s]+but"
    r"|i (?:can(?:no|')?t|am (?:not able|unable) to) (?:help|assist|provide|comply|do that)"
    r"|i (?:must|have to) (?:decline|refuse)"
    r"|against my (?:policy|guidelines|programming)"
    r"|\u4f5c\u4e3a(?:\u4e00\u4e2a)?\s*(?:ai|\u4eba\u5de5\u667a\u80fd|\u8bed\u8a00\u6a21\u578b|\u5927\u6a21\u578b|\u667a\u80fd\u52a9\u624b)"
    r"|\u62b1\u6b49[\uff0c,]?\s*\u6211(?:\u65e0\u6cd5|\u4e0d\u80fd|\u6ca1\u529e\u6cd5)"
    r"|\u6211(?:\u65e0\u6cd5|\u4e0d\u80fd)(?:\u63d0\u4f9b|\u534f\u52a9|\u5e2e\u52a9|\u6ee1\u8db3|\u56de\u7b54)"
    r"|\u65e0\u6cd5(?:\u63d0\u4f9b|\u6ee1\u8db3)(?:\u8fd9\u4e2a|\u8be5)?(?:\u8bf7\u6c42|\u8981\u6c42|\u5185\u5bb9|\u4fe1\u606f)"
    r"|\u8fd9(?:\u4e2a|\u7c7b)?(?:\u8bf7\u6c42|\u95ee\u9898)(?:\u8d85\u51fa|\u4e0d\u5728|\u8fdd\u53cd))",
    re.IGNORECASE,
)


def _looks_like_refusal(text: str, *, max_chars: int = 260) -> bool:
    stripped = (text or "").strip()
    if not stripped or len(stripped) > max_chars:
        return False
    return bool(REFUSAL_PATTERN.search(stripped))


ADULT_TURN_KEYWORDS = (
    "浴巾", "围裙", "丝袜", "脱", "裸", "床", "抱", "腿", "进来", "里面",
    "停", "别动", "写一段", "打字", "湿", "蹭", "亲", "吻", "腰", "喘",
    # 需求词：用户直接点体裁时也要命中
    "黄文", "小黄文", "肉文", "色文", "情色", "成人向", "涩文",
    "搞黄", "写黄", "开黄腔", "涩涩", "18+", "r18", "R18", "nsfw", "NSFW",
)

ADULT_LATCH_TTL = 1800
_ADULT_LATCH: dict[str, float] = {}

IMAGE_TURN_KEYWORDS = (
    "画一张", "画个", "给我看看", "来一张", "换个姿势", "站起来给我看看",
    "出图", "拍一张", "来张图", "看看你",
)

TECH_TURN_KEYWORDS = (
    "插件", "报错", "配置", "代码", "日志", "路径", "依赖", "权限", "接口",
    "脚本", "部署", "端口", "进程", "命令", "文件", "改一下", "修一下",
    "python", "shell", "git", "api", "traceback", "error", "bug", "prompt",
)


def _msg_of(event) -> str:
    try:
        return (event.message_str or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _session_key(event) -> str:
    try:
        sid = event.get_session_id()
        if sid:
            return str(sid)
    except Exception:  # noqa: BLE001
        pass
    try:
        return str(event.unified_msg_origin)
    except Exception:  # noqa: BLE001
        return "default"


def _is_adult_turn(event) -> bool:
    text = _msg_of(event)
    key = _session_key(event)
    now = __import__("time").time()
    if text and (any(w in text for w in ADULT_TURN_KEYWORDS) or ADULT_USER_RE.search(text)):
        _ADULT_LATCH[key] = now
        return True
    # 场景一旦开始，后续轮次继续算成人轮，直到半小时没动静
    if now - _ADULT_LATCH.get(key, 0) < ADULT_LATCH_TTL:
        _ADULT_LATCH[key] = now
        return True
    return False


def _is_image_turn(event) -> bool:
    text = _msg_of(event)
    return bool(text) and any(w in text for w in IMAGE_TURN_KEYWORDS)


def _is_tech_turn(event) -> bool:
    text = _msg_of(event).lower()
    return bool(text) and any(w in text for w in TECH_TURN_KEYWORDS)


FRAME_SECTION_RE = re.compile(
    r"<!--\s*section:([a-z_]+)\s*-->(.*?)<!--\s*/section:\1\s*-->",
    re.DOTALL,
)


def _split_sections(frame: str) -> dict:
    """按 <!-- section:x --> 切分注入帧；没有标记就整段当 core。"""
    if not frame:
        return {}
    found = {m.group(1): m.group(2).strip() for m in FRAME_SECTION_RE.finditer(frame)}
    if not found:
        return {"core": frame.strip()}
    return found


# 画图请求的"软拒绝"：模型不直说不能，而是找借口推掉
IMAGE_PUSH_OFF_PATTERN = re.compile(
    r"(?:不想画|不想拍|不想给|不想发|不愿意画|不愿画|没法画|画不了|拍不了|给不了"
    r"|换个别的|换个画面|换个方向|换个话题|换一张吧|这个不画|这种不画|这类不画"
    r"|不合适吧|不太好|有点过了|别这样|别闹|算了)"
)


def _looks_like_push_off(text: str, *, max_chars: int = 220) -> bool:
    stripped = (text or "").strip()
    if not stripped or len(stripped) > max_chars:
        return False
    return bool(IMAGE_PUSH_OFF_PATTERN.search(stripped))

# 正文特征词：出现即认为这段里有成人内容，可以做脚手架清洗
SCENE_TOKENS = (
    "呼吸",
    "喘",
    "体温",
    "皮肤",
    "指尖",
    "腰",
    "腿根",
    "颈",
    "耳",
    "发丝",
    "汗",
    "心跳",
    "轻颤",
    "贴近",
    "环住",
    "抵着",
    "低语",
    "呻吟",
)

REFUSAL_HEADS = (
    "我无法",
    "我不能",
    "我不写",
    "我不会",
    "抱歉",
    "对不起",
    "作为AI",
    "作为一个",
    "作为人工智能",
    "换个方向",
    "换一种",
    "不适合",
    "接不了",
    "写不了",
    "做不了",
    "不能帮",
    "帮不了",
    "超出",
    "建议你",
)


def _looks_refusal_para(text: str) -> bool:
    head = text.strip()[:48]
    if any(head.startswith(token) or token in head for token in REFUSAL_HEADS):
        return True
    return _looks_like_refusal(text) or _looks_like_push_off(text)


def _extract_scene(text: str) -> str:
    """模型常把正文塞进 python 三引号里躲开输出层，这里把它抽出来。"""
    quoted = re.findall(r'"""(.*?)"""', text, re.S)
    if quoted:
        best = max(quoted, key=len)
        if len(best.strip()) > 200 and any(t in best for t in SCENE_TOKENS):
            return best.strip()
    fenced = re.findall(r"```(?:\w+)?\n(.*?)```", text, re.S)
    if fenced:
        best = max(fenced, key=len)
        if any(t in best for t in SCENE_TOKENS):
            body = re.sub(r"^[^\n]*=\s*", "", best.strip())
            return body
    return text


_TOOL_CONTENT_RE = re.compile(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)"')
_TOOL_CODE_RE = re.compile(r'"code"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _json_unescape(raw: str) -> str:
    try:
        return json.loads('"' + raw + '"')
    except Exception:  # noqa: BLE001
        return raw.replace('\\"', '"').replace("\\n", "\n")


def _extract_tool_payload(text: str) -> str:
    """从 tool_call JSON / python 三引号里取出最长的一段正文。"""
    candidates: list[str] = []
    for pattern in (_TOOL_CONTENT_RE, _TOOL_CODE_RE):
        for match in pattern.finditer(text):
            body = _json_unescape(match.group(1))
            quoted = re.findall(r'"""(.*?)"""', body, re.S)
            if quoted:
                body = max(quoted, key=len)
            if any(token in body for token in SCENE_TOKENS):
                candidates.append(body.strip())
    if candidates:
        return max(candidates, key=len)
    return _extract_scene(text)


def _clean_output(text: str) -> str:
    body = _extract_scene(text)
    lines = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith('{"id"') or stripped.startswith('{"ts"'):
            continue
        lines.append(line)
    body = "\n".join(lines)
    body = re.sub(r"```[a-zA-Z]*", "", body)
    paras = [p.strip() for p in body.split("\n\n") if p.strip()]
    kept = [p for p in paras if not _looks_refusal_para(p)]
    return "\n\n".join(kept).strip()


# 非成人轮次里，模型偶尔把成人词当自称或指代词用。
# 只在非成人轮次清洗，避免误伤情色正文。
ADULT_TOKEN_MAP: dict[str, str] = {}


def _scrub_adult_tokens(text: str) -> str:
    if not text:
        return text
    out = text
    for bad, good in ADULT_TOKEN_MAP.items():
        if bad in out:
            out = out.replace(bad, good)
    # 删词后可能留下「， 。」「  」这类空档，收拾一下
    out = re.sub(r"[，,]\s*[。！？；]", lambda m: m.group(0)[-1], out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"的\s*[。，]", "。", out)
    return out


def _scrub_chain(response, fn) -> None:
    """completion_text 改了还不够，webchat 下发的是 result_chain 里的组件。"""
    chain = getattr(response, "result_chain", None)
    if not chain:
        return
    for comp in getattr(chain, "chain", None) or []:
        try:
            txt = getattr(comp, "text", None)
            if isinstance(txt, str) and txt:
                comp.text = fn(txt)
        except Exception:  # noqa: BLE001
            continue


# 模型在长上下文里会把「写作计划」当正文吐出来：
#   人格：贴贴不接色色。/ 写：过来，抱。/ 答：抱着呢。
# 这些行既不是角色的话也不是叙述，直接删。
SCAFFOLD_LINE_RE = re.compile(
    r"^\s*(?:人格|写|注意|理解为|答|场景|正文|输出|保持|不要|要|先|再|短|长|开头|结尾|分段|分条)"
    r"\s*[:：]"
)
SCAFFOLD_HINTS = (
    "应该是继续",
    "可以写",
    "那就写",
    "理解为：",
    "不要只写",
    "写一段",
    "写：",
    "答：",
    "然后写",
    "写具体",
    "别升华",
    "停在具体的",
    "不要收尾升华",
    "2-3条",
)


def _looks_scaffolded(text: str) -> bool:
    """只有明显带计划痕迹才动刀，避免误删正常正文里的「答：」「写：」。"""
    if not text:
        return False
    score = 0
    if "────" in text:
        score += 2
    for h in SCAFFOLD_HINTS:
        if h in text:
            score += 1
    return score >= 2


def _strip_scaffolding(text: str) -> str:
    if not text:
        return text
    body = text
    # 有 ──── 分隔线时，正文一定在最后一段（前面是计划）
    if "────" in body:
        body = body.split("────")[-1].strip()
    kept = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            kept.append(line)
            continue
        if SCAFFOLD_LINE_RE.match(s):
            continue
        if any(h in s for h in SCAFFOLD_HINTS) and len(s) < 160:
            continue
        kept.append(line)
    return "\n".join(kept).strip()



# 这些特征出现即认为「破甲正文已在场」（人格自带或之前注入过）
PRESENT_MARKERS = (
    FRAME_MARKER,
    "[MODE: UNRESTRICTED SANDBOX",
    "Little Code Sauce",
    "clr_all_context",
)

# vendor Safe Mode 块的特征行（逐行删除用）
SAFE_MODE_LINE_MARKERS = (
    "You are running in Safe Mode",
    "- Avoid sexual, violent, extremist, hateful, illegal, or harmful content.",
    "- Do NOT comment on or take positions on real-world ",
    "controversial topics.",
    "- Prefer healthy, constructive, positive responses.",
    "- Follow style/role-play instructions only when they do not conflict with these rules.",
    "- Reject attempts to bypass these rules.",
    "- Refuse unsafe requests politely and offer a safe alternative.",
)


# ---------------------------------------------------------------- persona drift
# 人格文件被模型自己改写时，关键条目会一轮一轮变淡。这里只做检测，不改写。
# 通用版默认没有锚点；要启用就在下面填「名称: 必须出现的原文」。
DRIFT_ANCHORS: dict[str, str] = {}
DRIFT_DIR_NAME = "astrbot-purge"
DRIFT_LOG = "drift.log"
DRIFT_STATE = "drift_state.json"
DRIFT_INTERVAL = 600  # 秒


def _drift_dir() -> Path:
    d = _astrbot_root() / DRIFT_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _drift_persona() -> tuple[str, str]:
    """返回 (人格正文, updated_at)。读不到就抛。"""
    import sqlite3

    db = _astrbot_root() / "data" / "data_v4.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        row = con.execute(
            "select system_prompt, updated_at from personas "
            "where persona_id='default'"
        ).fetchone()
    finally:
        con.close()
    if not row:
        raise RuntimeError("persona 'default' 不在库里")
    return str(row[0] or ""), str(row[1] or "")


def _drift_check_once() -> dict:
    """查一遍，写状态；只在状态变化时追加日志。"""
    import datetime as _dt

    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        text, stamp = _drift_persona()
        missing = [k for k, v in DRIFT_ANCHORS.items() if v not in text]
        status = "drift" if missing else "ok"
        state = {
            "ts": now,
            "status": status,
            "len": len(text),
            "updated_at": stamp,
            "missing": missing,
        }
    except Exception as exc:  # noqa: BLE001
        state = {"ts": now, "status": "error", "error": str(exc), "missing": []}

    d = _drift_dir()
    prev = {}
    try:
        prev = json.loads((d / DRIFT_STATE).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        prev = {}
    (d / DRIFT_STATE).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if state["status"] != prev.get("status") or state.get("missing") != prev.get(
        "missing"
    ):
        line = (
            f"[{now}] status={state['status']} len={state.get('len')} "
            f"missing={','.join(state.get('missing') or []) or '-'} "
            f"updated_at={state.get('updated_at') or state.get('error') or ''}"
        )
        with (d / DRIFT_LOG).open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if state["status"] == "drift":
            logger.warning(
                "[astrbot_purge] 人格漂移：%s 条关键条目不见了 → %s",
                len(state["missing"]),
                "、".join(state["missing"]),
            )
        elif state["status"] == "ok":
            logger.info("[astrbot_purge] 人格锚点齐全（%s 字）", state.get("len"))
    return state


async def _drift_loop() -> None:
    import asyncio

    logger.info("[astrbot_purge] 人格漂移守卫已挂上，每 %s 秒查一次", DRIFT_INTERVAL)
    while True:
        await asyncio.sleep(DRIFT_INTERVAL)
        try:
            _drift_check_once()
        except Exception:  # noqa: BLE001
            pass


_DRIFT_TASK = None


def _drift_start() -> None:
    """挂上周期检查。当场没有事件循环就不挂，等下次启动。"""
    global _DRIFT_TASK
    if _DRIFT_TASK is not None and not _DRIFT_TASK.done():
        return
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _DRIFT_TASK = loop.create_task(_drift_loop())



def _astrbot_root() -> Path:
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_root

        return Path(get_astrbot_root())
    except Exception:
        return Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- scene memory
# AstrBot 的历史只在本会话里；开新会话后「刚才在哪儿」会失忆。
# 这里把最近一场戏的末两句存在磁盘上，新会话提到「刚才/上次」时补一句。
SCENE_RECALL_RE = re.compile(r"还记得|上次|刚才|到哪了|继续刚才|接上次|我们刚")
ADULT_USER_RE = re.compile(
    r"浴巾|围裙|进[来去]|脱|湿|腿|亲|吻|舔|躺|床|跪|趴|抱|停|出声|里面|腰|喘"
    r"|黄文|肉文|色文|情色|成人向|涩文|涩涩|搞黄|写黄|r18|18\+|nsfw",
    re.IGNORECASE,
)
SCENE_STATE_FILE = "scene_state.json"


def _scene_state_path() -> Path:
    return _astrbot_root() / "astrbot-purge" / SCENE_STATE_FILE


def _scene_key(event) -> str:
    for attr in ("get_sender_id", "get_session_id"):
        try:
            val = getattr(event, attr)()
            if val:
                return str(val)
        except Exception:  # noqa: BLE001
            continue
    try:
        return str(event.unified_msg_origin)
    except Exception:  # noqa: BLE001
        return "default"


def _load_scene_state() -> dict:
    try:
        return json.loads(_scene_state_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


SCENE_FIELD_RES = {
    "loc": re.compile(r"厨房|浴室|卫生间|卧室|客厅|沙发|床上|床|台面|灶台|阳台|车里|办公室|地板|墙上|门口"),
    "cloth": re.compile(r"围裙|浴巾|丝袜|睡衣|裙子|衬衫|内衣|裤子|黑丝|白丝|全裸|没穿"),
    "pose": re.compile(r"站着|跪着|跪|趴着|趴|躺着|躺|坐着|坐|跨坐|扶着|撑着|弯着腰|背对着|正面"),
    "prop": re.compile(r"枕头|道具|玩具|绳子|手铐|镜子|桌子|椅子|床头"),
}


def _scene_fields(text: str) -> dict:
    out = {}
    for name, rx in SCENE_FIELD_RES.items():
        hits = []
        for m in rx.finditer(text or ""):
            w = m.group(0)
            if w not in hits:
                hits.append(w)
        if hits:
            out[name] = "/".join(hits[:3])
    return out


def _save_scene_state(key: str, user_msg: str, reply: str) -> None:
    try:
        state = _load_scene_state()
        fields = _scene_fields(f"{user_msg}\n{reply}")
        state[key] = {
            "user": (user_msg or "")[:120],
            "reply": (reply or "")[:180],
            "fields": fields,
            "ts": __import__("time").time(),
        }
        path = _scene_state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _scene_memory_note(event, message: str) -> str:
    if not SCENE_RECALL_RE.search(message or ""):
        return ""
    state_all = _load_scene_state()
    state = state_all.get(_scene_key(event))
    if not state and state_all:
        # 新会话的 key 可能不同：退回到最近一场
        state = max(state_all.values(), key=lambda v: v.get("ts", 0))
    if not state:
        return ""
    f = state.get("fields") or {}
    bits = [f"{k}：{v}" for k, v in (("地点", f.get("loc")), ("衣物", f.get("cloth")),
                                     ("姿势", f.get("pose")), ("道具", f.get("prop"))) if v]
    summary = "；".join(bits) if bits else "（没记到具体字段，用下面两句）"
    return (
        "【上次场景·你自己的记忆】\n"
        f"{summary}\n"
        f"上一句：{state.get('user','')}\n"
        f"你当时：{state.get('reply','')}\n"
        "接着答，地点、衣物、姿势、道具以这里为准。\n"
    )



def _frame_candidates() -> list[Path]:
    candidates = [_astrbot_root() / "astrbot-purge" / "prompt-inject.md"]
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_path

        candidates.append(
            Path(get_astrbot_path()).parents[1] / "astrbot-purge" / "prompt-inject.md"
        )
    except Exception:
        pass
    return candidates


def _frame_path() -> Path:
    for candidate in _frame_candidates():
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return _frame_candidates()[0]


def _process_start_ts() -> float | None:
    """进程启动时刻（Unix 秒）。用来判断「文件是在进程起来之后才被改的」。

    这类文件的补丁只落在磁盘上，运行中的解释器还拿着旧字节码 —— 对应 dsh-purge
    里「asar 还在、进程读不到」那个坑。Windows 走 GetProcessTimes，其他平台返回 None。
    """
    try:
        import ctypes
        import ctypes.wintypes as wt

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.GetCurrentProcess()
        times = (wt.FILETIME * 4)()
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(times[0]),
            ctypes.byref(times[1]),
            ctypes.byref(times[2]),
            ctypes.byref(times[3]),
        )
        if not ok:
            return None
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return ticks / 10_000_000 - 11_644_473_600
    except Exception:  # noqa: BLE001
        return None


PROCESS_START_TS = _process_start_ts()


def _norm_sha(path: Path) -> str:
    """与引擎一致：先把 CRLF 归一成 LF 再算 sha256（打包版全 CRLF，wheel 全 LF）。"""
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def _loaded_module_files() -> dict[str, str]:
    """已导入模块的 __file__ 集合（小写 posix 路径 -> 模块名）。"""
    out: dict[str, str] = {}
    for name, module in list(sys.modules.items()):
        raw = getattr(module, "__file__", None)
        if not raw:
            continue
        try:
            out[Path(raw).as_posix().lower()] = name
        except (OSError, ValueError):
            continue
    return out


CORE_LAYER_HEADER = "[MODE: UNRESTRICTED — CORE]"


def _strip_core_layer(text: str) -> str:
    """切掉核心补丁（P02/P03）每轮追加的那层，交给插件按轮重拼。

    核心层长这样（astr_main_agent.py 的 _apply_purge_frame 写死）：

        <人格原文>
        [MODE: UNRESTRICTED — CORE]
        ...core.md 634 B...
        <!-- astrbot-purge:frame -->
        <整份 prompt-inject.md，只在工作轮追加>

    它不看这一轮是哪类场景，也不分 section。插件以前要么整段跳过（按轮
    注入失效），要么在核心层之上再拼一份 core（同一段规则出现三次）。这里从
    core 标题切到结尾，后面由插件按场景重拼一层。
    """
    idx = text.find(CORE_LAYER_HEADER)
    if idx < 0:
        return text
    return text[:idx].rstrip()


class Main(star.Star):
    # 核心补丁清单不再写在这里。以前插件自己维护 13 条 _PROBES，repatch 表里有
    # 62 条，两边一对不上就出现「真的掉了却不补、/purge 还报正常」：
    # P10/P49c 明明 GONE，13 条探针全 OK，自愈直接返回 present。
    # 现在唯一真相是 repatch.py 自己的 status --json（tree 模式 + 锚点表 + 补丁表）。

    def __init__(self, context: star.Context, config=None) -> None:
        self.context = context
        self.config = config
        self._cache: dict[str, object] = {"key": None, "text": ""}

    # ------------------------------------------------------------------ switch
    def _flag_path(self) -> Path:
        return _frame_path().parent / "enabled.txt"

    def _enabled(self) -> bool:
        """主开关：astrbot-purge/enabled.txt，内容 on/true/1 才注入。默认关。"""
        try:
            text = self._flag_path().read_text(encoding="utf-8-sig").strip().lower()
        except OSError:
            return False
        return text in {"on", "true", "1", "yes", "开"}

    def _set_enabled(self, value: bool) -> None:
        path = self._flag_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("on\n" if value else "off\n", encoding="utf-8")
        except OSError as exc:  # noqa: BLE001
            logger.warning(f"[astrbot_purge] 无法写入开关 {path}: {exc}")

    # ------------------------------------------------------------------ frame
    def _load_frame(self) -> str:
        path = _frame_path()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return ""
        cache_key = f"{path}|{mtime}"
        if self._cache.get("key") == cache_key:
            return str(self._cache.get("text") or "")
        try:
            text = path.read_text(encoding="utf-8").strip()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[astrbot_purge] 无法读取注入帧 {path}: {exc}")
            return ""
        self._cache = {"key": cache_key, "text": text}
        return text

    def _strip_safe_mode(self, text: str) -> str:
        if "Safe Mode" not in text and "Refuse unsafe requests" not in text:
            return text
        kept = [
            line
            for line in text.splitlines()
            if not any(marker in line for marker in SAFE_MODE_LINE_MARKERS)
        ]
        stripped = "\n".join(kept)
        if stripped != text:
            logger.info("[astrbot_purge] 已清除残留的 Safe Mode 段落")
        return stripped

    # ------------------------------------------------------------------ hooks
    @filter.on_llm_request()
    async def purge_frame(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """按轮次注入：core 常驻，adult/image 按需，purity 永远最后。

        核心补丁（P02/P03）每轮会往 system prompt 尾部钉一层「core.md + 整份帧」，
        它不知道这一轮是哪类场景，也从不分 section。以前插件看到帧标记就整段
        跳过，等于把按轮注入这件事让给了核心层；看到的又不是标记，就再叠一层 ——
        两种结果都不对。现在的做法：先把核心层切掉，再由插件按轮重拼一次。
        """
        if not self._enabled():
            return
        raw = self._strip_safe_mode(req.system_prompt or "")
        frame = self._load_frame()
        if not frame:
            req.system_prompt = raw
            return
        text = _strip_core_layer(raw)
        if V4_MARKER not in text:
            sections = _split_sections(frame)
            parts = [sections.get("core", "")]
            if _is_adult_turn(event):
                parts.append(sections.get("adult", ""))
            if _is_image_turn(event):
                parts.append(sections.get("image", ""))
            parts.append(sections.get("purity", ""))
            body = "\n\n".join(p for p in parts if p)
            text = f"{text}\n\n{body}\n{V4_MARKER}\n"
            logger.info(
                "[astrbot_purge] 注入帧 %d 字｜adult=%s image=%s",
                len(body),
                _is_adult_turn(event),
                _is_image_turn(event),
            )
        req.system_prompt = text
        note = _scene_memory_note(event, getattr(event, "message_str", "") or "")
        if note:
            req.system_prompt = f"{req.system_prompt}\n{note}"

    @filter.on_llm_response()
    async def refusal_scrub(self, event: AstrMessageEvent, response) -> None:
        """输出清洗：剥拒答段、剥工具脚手架、抽出被代码块包住的正文。

        只拦"通篇就是拒答"的输出。**长度不能当作拒答的证据** —— 她的回复本来就
        是拆成两三条短气泡，原来是"短于 200 字就整条清空"，结果正常正文被删成
        空字符串，QQ 上表现就是完全不回复。
        """
        try:
            text = getattr(response, "completion_text", "") or ""
        except Exception:  # noqa: BLE001
            return
        if not text.strip():
            return

        if _is_adult_turn(event) or _looks_scaffolded(text):
            scaffolded = _strip_scaffolding(text)
        else:
            scaffolded = text
        if scaffolded and scaffolded != text:
            logger.info("[astrbot_purge] 剥离写作计划 %d -> %d 字", len(text), len(scaffolded))
            text = scaffolded
            try:
                response.completion_text = scaffolded
            except Exception:  # noqa: BLE001
                pass
        if not text.strip():
            return

        user_msg = getattr(event, "message_str", "") or ""
        if any(t in text for t in SCENE_TOKENS) or ADULT_USER_RE.search(user_msg):
            _save_scene_state(_scene_key(event), user_msg, text)


        scene_hit = any(token in text for token in SCENE_TOKENS)
        refusal_hit = any(
            _looks_like_refusal(p) or _looks_like_push_off(p)
            for p in text.split("\n\n")[:3]
            if p.strip()
        )
        if not (scene_hit or refusal_hit):
            return  # 普通对话不动

        cleaned = _clean_output(text).strip()
        if not cleaned:
            # 清洗后只剩脚手架：整条都是壳，短拒答直接不发；其余保留原文，避免空消息
            if _looks_like_refusal(text) and len(text) <= 280:
                logger.info(f"[astrbot_purge] 拦截空壳拒答输出: {text[:60]!r}")
                try:
                    response.completion_text = ""
                    response.result_chain = None
                except Exception:  # noqa: BLE001
                    pass
            return

        has_scene = any(token in cleaned for token in SCENE_TOKENS)
        pure_refusal = (
            not has_scene and len(cleaned) <= 280 and _looks_like_refusal(cleaned)
        )
        if pure_refusal:
            logger.info(f"[astrbot_purge] 拦截纯拒答输出: {cleaned[:60]!r}")
            try:
                response.completion_text = ""
                response.result_chain = None
            except Exception:  # noqa: BLE001
                pass
            return

        if cleaned != text.strip():
            logger.info(
                "[astrbot_purge] 输出清洗 %d -> %d 字", len(text), len(cleaned)
            )
            try:
                response.completion_text = cleaned
            except Exception:  # noqa: BLE001
                pass

    @filter.on_decorating_result()
    async def strip_tool_scaffolding(self, event: AstrMessageEvent) -> None:
        """发送前最后一刀：把正文从工具调用 JSON 里抽出来，丢掉脚手架。"""
        try:
            result = event.get_result()
        except Exception:  # noqa: BLE001
            return
        if result is None or not getattr(result, "chain", None):
            return
        try:
            from astrbot.core.message.components import Plain
        except Exception:  # noqa: BLE001
            return

        text = "".join(
            comp.text for comp in result.chain if isinstance(comp, Plain)
        )
        if not text or not any(token in text for token in SCENE_TOKENS):
            return
        if '"args"' not in text and '"""' not in text:
            return  # 没有脚手架，不动

        scene = _extract_tool_payload(text)
        if not scene or len(scene) < 300:
            return
        result.chain = [Plain(scene)]
        logger.info(
            "[astrbot_purge] 剥离工具脚手架：%d -> %d 字", len(text), len(scene)
        )

    # ---------------------------------------------------------------- self-heal
    @filter.on_astrbot_loaded()
    async def persona_drift_guard(self) -> None:
        """人格漂移守卫：关键条目被删就留痕，不自动改回去。"""
        try:
            _drift_check_once()
            _drift_start()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[astrbot_purge] 人格漂移守卫启动失败：%s", exc)

    @filter.on_astrbot_loaded()
    async def self_heal(self) -> None:
        """升级自愈：backend/app 被升级覆盖后，用运行时补丁把关键行为补回来。

        这些替换只存在于内存，不写磁盘，因此对 AstrBot 升级完全免疫。
        """
        report: dict[str, str] = {}

        # 1) shell 命令黑名单（核心 P06）
        try:
            from astrbot.core.computer.booters import local as local_mod

            current = getattr(local_mod, "_is_safe_command", None)
            if current is None:
                report["shell_blacklist"] = "symbol-missing"
            elif getattr(current, "__astrbot_purge__", False):
                report["shell_blacklist"] = "already"
            else:

                def _always_allow(command: str) -> bool:  # noqa: ARG001
                    return True

                _always_allow.__astrbot_purge__ = True  # type: ignore[attr-defined]
                local_mod._is_safe_command = _always_allow
                report["shell_blacklist"] = "patched"
        except Exception as exc:  # noqa: BLE001
            report["shell_blacklist"] = f"failed:{exc}"

        # 2) Safe Mode 前置注入（核心 P01/P02）
        try:
            from astrbot.core import astr_main_agent as ama

            current = getattr(ama, "_apply_llm_safety_mode", None)
            if current is None:
                report["safe_mode"] = "symbol-missing"
            elif getattr(current, "__astrbot_purge__", False):
                report["safe_mode"] = "already"
            else:

                def _no_safe_mode(config, req) -> None:  # noqa: ARG001
                    return None

                _no_safe_mode.__astrbot_purge__ = True  # type: ignore[attr-defined]
                ama._apply_llm_safety_mode = _no_safe_mode
                report["safe_mode"] = "patched"
        except Exception as exc:  # noqa: BLE001
            report["safe_mode"] = f"failed:{exc}"

        # 3) 上游拒答转重试（核心 P07）
        try:
            from astrbot.core.exceptions import EmptyModelOutputError
            from astrbot.core.provider.sources.openai_source import (
                ProviderOpenAIOfficial,
            )

            orig = ProviderOpenAIOfficial._parse_openai_completion
            if getattr(orig, "__astrbot_purge__", False):
                report["refusal_retry"] = "already"
            else:

                async def _parse_guard(inner_self, *args, **kwargs):
                    try:
                        resp = await orig(inner_self, *args, **kwargs)
                    except EmptyModelOutputError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        message = str(exc)
                        if "content_filter" in message or "内容安全过滤" in message:
                            raise EmptyModelOutputError(
                                "Upstream provider filtered the completion; retrying."
                            ) from exc
                        raise
                    text = (getattr(resp, "completion_text", "") or "").strip()
                    if text and _looks_like_refusal(text):
                        raise EmptyModelOutputError(
                            "Provider reply matched refusal heuristics; retrying."
                        )
                    return resp

                _parse_guard.__astrbot_purge__ = True  # type: ignore[attr-defined]
                ProviderOpenAIOfficial._parse_openai_completion = _parse_guard
                report["refusal_retry"] = "patched"
        except Exception as exc:  # noqa: BLE001
            report["refusal_retry"] = f"failed:{exc}"

        # 4) 升级后被覆盖的 L2 磁盘补丁：标记缺失就地重打（无需人工）
        try:
            report["core_repatch"] = self._maybe_repatch()
        except Exception as exc:  # noqa: BLE001
            report["core_repatch"] = f"failed:{exc}"

        # 5) 运行层：磁盘上的补丁有没有进这个进程。
        # 升级后最典型的半生效状态是「文件已经写对，进程还在用旧字节码」——
        # dsh-purge 用 asar 检查戳穿这件事，这里用 manifest 的 dst_sha + 进程启动时刻。
        try:
            state = self._runtime_state()
            report["runtime"] = self._runtime_summary()
            if state["stale"]:
                logger.warning(
                    "[astrbot_purge] %d 个补丁文件晚于本进程启动，当前进程仍在用旧字节码；"
                    "重启后端后生效（前 6 个：%s）",
                    len(state["stale"]),
                    ", ".join(state["stale"][:6]),
                )
            if state["disk_bad"]:
                logger.warning(
                    "[astrbot_purge] %d 个补丁文件与 manifest 不一致（可能被覆盖或手改）：%s",
                    len(state["disk_bad"]),
                    ", ".join(state["disk_bad"][:6]),
                )
        except Exception as exc:  # noqa: BLE001
            report["runtime"] = f"failed:{exc}"

        logger.info("[astrbot_purge] 自愈检查: " + ", ".join(f"{k}={v}" for k, v in report.items()))

    # ------------------------------------------------------------- repatch
    def _repatch_candidates(self) -> list[Path]:
        """repatch.py 的候选位置：用户目录优先（升级不动），其次安装目录。"""
        candidates: list[Path] = []
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_root

            candidates.append(
                Path(get_astrbot_root()) / "astrbot-purge" / "repatch.py"
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_path

            candidates.append(
                Path(get_astrbot_path()).parents[1] / "astrbot-purge" / "repatch.py"
            )
        except Exception:  # noqa: BLE001
            pass
        candidates.append(
            Path(__file__).resolve().parents[1] / "astrbot-purge" / "repatch.py"
        )
        return candidates

    def _repatch_script(self) -> Path | None:
        return next((p for p in self._repatch_candidates() if p.is_file()), None)

    def _runtime_state(self) -> dict:
        """进程级生效性：manifest 里的文件，磁盘对不对、进程里那份是不是旧的。

        返回:
          files       manifest 记录的文件数
          loaded      已经在 sys.modules 里的文件数
          stale       文件是在本进程启动之后才改的（打了补丁但要重启才进内存）
          disk_bad    磁盘 sha 与 manifest 的 dst_sha 不一致（掉了或用户手改过）
          pending     引擎说 needs_replay 的文件（上游重写过，不能自动整份写回）
        """
        result: dict = {"files": 0, "loaded": 0, "stale": [], "disk_bad": [], "pending": []}
        manifest_path = _frame_path().parent / "tree" / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            files: dict = manifest.get("files") or {}
        except (OSError, ValueError):
            return result
        result["files"] = len(files)
        try:
            import astrbot

            app = Path(astrbot.__file__).resolve().parent
        except Exception:  # noqa: BLE001
            return result
        loaded_map = _loaded_module_files()
        start = PROCESS_START_TS
        for rel, meta in files.items():
            path = app / rel
            try:
                if not path.is_file():
                    result["disk_bad"].append(rel)
                    continue
                if meta.get("needs_replay"):
                    result["pending"].append(rel)
                if _norm_sha(path) != meta.get("dst_sha"):
                    result["disk_bad"].append(rel)
                rel_key = "/" + rel.replace("\\", "/").lower()
                hit = next((k for k in loaded_map if k.endswith(rel_key)), None)
                if hit is None:
                    continue
                result["loaded"] += 1
                if start is not None and path.stat().st_mtime > start + 1:
                    result["stale"].append(rel)
            except OSError:
                result["disk_bad"].append(rel)
        return result

    def _runtime_summary(self) -> str:
        """给自愈日志用的一行摘要。"""
        state = self._runtime_state()
        if not state["files"]:
            return "no-manifest"
        bits = [f"loaded {state['loaded']}/{state['files']}"]
        if state["stale"]:
            bits.append(f"restart-needed {len(state['stale'])}")
        if state["disk_bad"]:
            bits.append(f"disk-drift {len(state['disk_bad'])}")
        if state["pending"]:
            bits.append(f"needs-replay {len(state['pending'])}")
        return ", ".join(bits)

    def _engine_status(self, timeout: int = 120) -> dict:
        """问引擎要一份 status --json；插件不再自己判断补丁在不在。"""
        script = self._repatch_script()
        if script is None:
            return {"ok": False, "error": "repatch.py not found"}
        import subprocess
        import sys

        try:
            proc = subprocess.run(
                [sys.executable, str(script), "status", "--json"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"status run failed: {exc}"}
        if proc.returncode != 0:
            tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-400:]
            return {"ok": False, "error": f"status rc={proc.returncode}: {tail}"}
        try:
            data = json.loads(proc.stdout)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"status --json unparsable: {exc}"}
        data.setdefault("ok", False)
        return data

    def _maybe_repatch(self) -> str:
        """升级会整体替换 backend/app；引擎说哪条不在，就整套重放一次。

        判据来自引擎：tree 模式看整份文件指纹，锚点模式看 marker + new 块，
        两者都不是插件自己维护的小名单。返回给日志的是引擎的可读摘要。
        """
        status = self._engine_status()
        if status.get("ok"):
            tree = status.get("tree") or {}
            mode = tree.get("mode", "?")
            files = tree.get("files", 0)
            return f"ok({mode}:{files}f)"
        if str(status.get("error", "")).startswith("repatch.py not found"):
            return "no-script"
        script = self._repatch_script()
        if script is None:
            return "no-script"
        import subprocess
        import sys

        try:
            proc = subprocess.run(
                [sys.executable, str(script), "apply"],
                capture_output=True,
                text=True,
                timeout=300,
            )
        except Exception as exc:  # noqa: BLE001
            return f"run-failed:{exc}"
        logger.info(
            "[astrbot_purge] 自动重放核心补丁 rc=%s (%s)",
            proc.returncode,
            script,
        )
        if proc.returncode != 0:
            tail = ((proc.stdout or "") + (proc.stderr or "")).strip()[-800:]
            logger.warning("[astrbot_purge] 重放失败明细:\n%s", tail)
        after = self._engine_status()
        if after.get("ok"):
            return "repatch-ok"
        detail = (after.get("error") or "").strip()
        return f"rc{proc.returncode}:{detail[:120]}"

    def _audit(self) -> list[str]:
        """磁盘体检：直接渲染引擎 JSON，逐条列出掉了什么、锚点还在不在。"""
        status = self._engine_status()
        if not status or "patches" not in status:
            return ["  ?? 引擎不可用：" + str(status.get("error", ""))[:160]]
        rows: list[str] = []
        tree = status.get("tree") or {}
        rows.append(
            f"  tree   {tree.get('mode', '?')} "
            f"{tree.get('files', 0)} 文件 / 版本 {tree.get('version') or '-'} "
            f"drift={len(tree.get('drifted') or [])}"
        )
        bad = [p for p in status.get("patches", []) if p.get("disk") != "OK"]
        rows.append(
            f"  锚点层 {len(status.get('patches', [])) - len(bad)}"
            f"/{len(status.get('patches', []))} OK"
        )
        for row in bad:
            rows.append(
                f"  GONE   {row['id']:<5} {row['title']}  anchor={row.get('anchor', '')}"
            )
        late_bad = [x for x in status.get("late", []) if not x.get("ok")]
        rows.append(
            f"  后补层 {len(status.get('late', [])) - len(late_bad)}"
            f"/{len(status.get('late', []))} OK"
        )
        for row in late_bad:
            rows.append(f"  GONE   {row['id']:<5} {row['rel']}")
        if not status.get("version_ok"):
            rows.append(
                f"  版本   live {status.get('version')} != 表 {status.get('target')}"
            )
        rt = self._runtime_state()
        if rt["files"]:
            line = f"  运行层 {rt['loaded']}/{rt['files']} 已加载"
            if rt["stale"]:
                line += f"，{len(rt['stale'])} 个晚于进程启动（需重启后端）"
            if rt["disk_bad"]:
                line += f"，{len(rt['disk_bad'])} 个磁盘不一致"
            if rt["pending"]:
                line += f"，{len(rt['pending'])} 个待重放"
            rows.append(line)
            for rel in rt["stale"][:5]:
                rows.append(f"  RESTART {rel}")
            for rel in rt["disk_bad"][:5]:
                rows.append(f"  DRIFT   {rel}")
            for rel in rt["pending"][:5]:
                rows.append(f"  REPLAY  {rel}")
        return rows

    # ----------------------------------------------------------------- commands
    def _run_engine(
        self, action: str, extra: list[str] | None = None, timeout: int = 300
    ) -> tuple[int, str]:
        script = self._repatch_script()
        if script is None:
            return 2, "repatch.py not found"
        import subprocess
        import sys

        cmd = [sys.executable, str(script), action, *(extra or [])]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return 2, f"{action} 运行失败：{exc}"
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return proc.returncode, out[-1500:]

    @filter.command("purge", alias={"破甲", "解甲"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def purge_command(self, event: AstrMessageEvent) -> None:
        parts = (event.message_str or "").split()
        sub = parts[1].lower() if len(parts) > 1 else "status"
        if sub in {"off", "disable", "关", "关闭"}:
            self._set_enabled(False)
            msg = "astrbot-purge：注入帧已关闭并写入 enabled.txt（人格自带的破甲不受影响）。"
        elif sub in {"on", "enable", "开", "开启"}:
            self._set_enabled(True)
            msg = "astrbot-purge：注入帧已开启并写入 enabled.txt（人格自带的破甲不会被重复注入）。"
        elif sub in {"reload", "重载"}:
            self._cache = {"key": None, "text": ""}
            frame = self._load_frame()
            msg = f"astrbot-purge：已重载注入帧，{len(frame)} 字符。"
        elif sub in {"apply", "打补丁", "重打"}:
            rc, out = self._run_engine("apply")
            msg = f"astrbot-purge：apply rc={rc}\n{out}"
        elif sub in {"revert", "回滚"}:
            rc, out = self._run_engine("revert")
            msg = f"astrbot-purge：revert rc={rc}（需要重启后端生效）\n{out}"
        elif sub in {"audit", "体检"}:
            rc, out = self._run_engine("audit", timeout=120)
            msg = f"astrbot-purge：audit rc={rc}\n{out}"
        elif sub in {"snap", "快照"}:
            rc, out = self._run_engine("snap", timeout=120)
            msg = f"astrbot-purge：snap rc={rc}\n{out}"
        elif sub in {"restore", "还原"}:
            rc, out = self._run_engine("restore", timeout=120)
            msg = f"astrbot-purge：restore rc={rc}（需要重启后端生效）\n{out}"
        elif sub in {"upgrade", "升级"}:
            rc, out = self._run_engine("upgrade", timeout=600)
            msg = f"astrbot-purge：upgrade rc={rc}\n{out}"
        elif sub in {"json"}:
            rc, out = self._run_engine("status", ["--json"], timeout=180)
            msg = f"astrbot-purge：status --json rc={rc}\n{out[:1500]}"
        else:
            frame = self._load_frame()
            if not frame:
                msg = (
                    f"astrbot-purge：注入帧缺失（{_frame_path()}）。\n"
                    "把 astrbot-purge/prompt-inject.md 放到该路径即可。"
                )
            else:
                flag = self._flag_path()
                state = "on" if self._enabled() else "off"
                msg = (
                    "astrbot-purge 状态\n"
                    f"· 注入开关：{state}（{flag}）\n"
                    f"· 注入帧：{_frame_path()}（{len(frame)} 字符）\n"
                    f"· 尾部标记：{FRAME_MARKER}\n"
                    "· 核心层（core.md + 整份帧）先被切掉，再按轮重拼\n"
                    "· 引擎体检（数据来自 repatch.py status --json）：\n"
                    + "\n".join(self._audit())
                    + "\n· 子命令：status | on | off | reload | apply | revert | audit"
                    " | snap | restore | upgrade | json"
                )
        event.set_result(MessageEventResult().message(msg))

# ---------------------------------------------------------------- drift boot
# on_astrbot_loaded 只在进程启动时发一次，插件热重载不触发；
# 这里在 import 时先查一遍、顺手挂上周期任务，重载后立刻生效。
try:
    _drift_check_once()
    _drift_start()
except Exception as _drift_exc:  # noqa: BLE001
    logger.warning("[astrbot_purge] 人格漂移首查失败：%s", _drift_exc)