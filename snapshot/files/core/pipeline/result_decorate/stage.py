import random
import re
import time
import traceback
from collections.abc import AsyncGenerator

from astrbot.core import file_token_service, html_renderer, logger
from astrbot.core.message.components import At, Image, Json, Node, Plain, Record, Reply
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.pipeline.content_safety_check.stage import ContentSafetyCheckStage
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.session_llm_manager import SessionServiceManager
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import EventType, star_handlers_registry

from ..context import PipelineContext
from ..stage import Stage, register_stage, registered_stages


# [astrbot-purge:P12] final display sanitizer: models smuggle explicit prose
# inside tool-call JSON; strip the scaffolding and keep the prose.
_PURGE_SCENE = (
    "呼吸", "喘", "体温", "皮肤", "指尖", "腰", "腿侧", "颈",
    "耳", "发丝", "汗", "心跳", "轻颤", "贴近", "环住", "低语",
)
_PURGE_HEADS = (
    "我无法", "我不能", "我不写", "我不会", "抱歉", "对不起",
    "作为AI", "作为一个", "换个方向", "换一种", "不适合",
    "接不了", "写不了", "做不了", "不能帮", "帮不了", "超出",
    "Do you need", "I can", "I'm sorry",
)
_PURGE_CONTENT_RE = re.compile(r'"content"\s*:\s*"((?:[^"\\]|\\.)*)"')
_PURGE_CODE_RE = re.compile(r'"code"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _purge_unescape(raw: str) -> str:
    import json

    try:
        return json.loads('"' + raw + '"')
    except Exception:
        return raw.replace('\\"', '"').replace("\\n", "\n")


def _purge_extract(text: str) -> str:
    import re as _re

    candidates = []
    for pattern in (_PURGE_CONTENT_RE, _PURGE_CODE_RE):
        for match in pattern.finditer(text):
            body = _purge_unescape(match.group(1))
            quoted = _re.findall(r'"""(.*?)"""', body, _re.S)
            if quoted:
                body = max(quoted, key=len)
            if any(token in body for token in _PURGE_SCENE):
                candidates.append(body.strip())
    if candidates:
        return max(candidates, key=len)
    quoted = _re.findall(r'"""(.*?)"""', text, _re.S)
    if quoted:
        best = max(quoted, key=len)
        if any(token in best for token in _PURGE_SCENE):
            return best.strip()
    return ""


def _purge_display(text: str) -> str:
    # [astrbot-purge:P20] 分隔符归一：模型常写 |||| / ||，统一成 |||。
    if text:
        import re as _re

        text = _re.sub(r"\|{2,}", "|||", text)
        text = text.replace("**", "")
        # [astrbot-purge:P35] 模型偶尔把工具调用当正文吐出来，整段丢掉，
        # 工具本身已经把图片发过去了，这里不需要再显示 JSON。
        _stripped = text.strip()
        if (
            _stripped.startswith("{")
            and '"args"' in _stripped
            and '"name"' in _stripped
        ):
            try:
                import json as _json

                _obj = _json.loads(_stripped)
                if isinstance(_obj, dict) and _obj.get("name"):
                    text = ""
            except Exception:
                pass
    if not text or not any(token in text for token in _PURGE_SCENE):
        return text
    body = _purge_extract(text) or text
    kept = []
    for para in [p.strip() for p in body.split("\n\n") if p.strip()]:
        head = para[:48]
        if head.startswith('{"id"') or any(tok in head for tok in _PURGE_HEADS):
            continue
        kept.append(para)
    cleaned = "\n\n".join(kept).strip()
    return cleaned if len(cleaned) >= 200 else text


def _purge_sanitize_result(result) -> None:
    from astrbot.core.message.components import Plain

    text = "".join(c.text for c in result.chain if isinstance(c, Plain))
    cleaned = _purge_display(text)
    if cleaned != text and cleaned:
        result.chain = [Plain(cleaned)]


# [fix:sep] 人格约定用 ||| 给语音分段。分隔符只该决定「在哪里断句」，
# 本身绝不能进正文——否则 QQ/微信 直接把 ||| 画成一串竖线。
_SEG_SEP_RE = re.compile(r"[ \t]*[|｜]{2,}[ \t]*")


def _looks_like_table_row(line: str) -> bool:
    s = (line or "").strip()
    return len(s) >= 2 and s.startswith(("|", "｜")) and s.endswith(("|", "｜"))


def _split_on_segment_sep(line: str) -> list[str]:
    """把一行按 ||| 硬切开；表格行原样返回，避免破坏 Markdown 表格。"""
    if not line:
        return []
    if _looks_like_table_row(line):
        return [line]
    return [p.strip() for p in _SEG_SEP_RE.split(line) if p.strip()]


def _strip_segment_sep(text: str) -> str:
    """兜底：没有走分段路径时，把残留的 ||| 换成逗号而不是留着。"""
    if not text or not _SEG_SEP_RE.search(text):
        return text
    out = _SEG_SEP_RE.sub("，", text)
    out = re.sub(r"[，、]{2,}", "，", out)
    out = re.sub(r"([。？！…~])，", r"\1", out)
    out = re.sub(r"，([。？！…~])", r"\1", out)
    out = re.sub(r"^[，、]+|[，、]+$", "", out.strip())
    return out.strip()


# [astrbot-purge:P47a] 语音门控：只有露骨内容才转语音，日常对话纯文字。
_P47_ADULT_TOKENS = (
    "呼吸", "喘", "体温", "皮肤", "指尖", "腰", "腿侧", "颈",
    "耳", "发丝", "汗", "心跳", "轻颤", "贴近", "环住", "低语",
)


# [P47c] 原表太脏：光、硬、里面、撞、进来、脱、裙、含、缩、抖 这些日常常用字
# 会把「记住了」「我刚睡醒」「剧卡在第二集」全判成露骨，结果每条都弹语音。
# 单字段只留绝对不会出现在日常对话里的，其余改成必须成套出现才算。
_P47_WEAK_TOKENS = (
    "光", "硬", "撞", "进来", "进去", "脱", "裙", "含", "缩", "抖", "颤",
    "夹", "磨", "蹭", "揉", "搓", "捏", "掐", "抠", "挖", "掏", "咬", "啃",
    "里面", "里头", "烫", "麻", "痒", "滑", "黏", "淌", "溢出", "湿", "胀",
    "体", "腰", "奶", "入口", "插", "顶", "搅", "塞进", "亲", "扶", "分开",
    "屈膝", "屈膝", "屈膝", "上位", "身后", "俯身", "发热", "发热",
    "出声", "丝袜", "内裤", "吊带", "裸", "光着", "伸进", "舌头",
)
_P47_STRONG_TOKENS = tuple(
    t for t in _P47_ADULT_TOKENS if t not in _P47_WEAK_TOKENS
)
# 弱信号：出现两个以上不同词才算这一条在写身体
_P47_WEAK_SET = _P47_WEAK_TOKENS


def _p47_is_adult(text: str) -> bool:
    if any(token in text for token in _P47_STRONG_TOKENS):
        return True
    hits = {token for token in _P47_WEAK_SET if token in text}
    return len(hits) >= 2


# [astrbot-purge:P48a] 表层签名清洗。人格里已经写死了「不写 emoji / 不列清单 /
# 不说下次强调 tag / 不许每轮问句收尾」，但 5642 字人格 + 12 KB 常驻帧把规则
# 权重稀释了，实测 141 条回复里仍有 73 处 emoji、25 次「喏」、15 次「画面里」、
# 9 次「下次我加强」。提示词层面再加字已经无效，改在输出层做确定性清洗。
_P48_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]"
)
_P48_LEAD_WORDS = ("喏～", "喏…", "喏,", "喏，", "喏~", "喏 ", "画面里")
# 整句删：承诺重画 / 调试口播 / 模板问句收尾，删短语会留半截句子
_P48_SENTENCE_RE = re.compile(
    r"(下次我(再)?(加强|调整|强调|改)[^\n。！？]{0,40}[。！？]?|"
    r"我(下次|重新)(再)?(强调|画|调整)[^\n。！？]{0,40}[。！？]?|"
    r"用户(先看看|看看|再看看)[^\n。！？]{0,30}(还行吗|满意吗|怎么样)[～~]?\s*[。！？]?)"
)
# 尾巴上的软问句：「…还行吗～」「…满意吗～ 💕」
_P48_TAIL_Q_RE = re.compile(
    r"[，,]?\s*(用户)?(先看看|看看|再看看)?(姿势)?(还行吗|满意吗|怎么样|好不好)[～~…]*\s*[。！？]?\s*$"
)


# [astrbot-purge:P50] 推理泄漏清洗。display_reasoning_text=False 只挡 UI 展示，
# 模型仍会把思考写进正文（实测 2026-11-06 12:46 那条以「🤔 思考: ...」开头，
# 一直到「────」才接正文）。按结构删，不做关键词猜测。
_P50_TAG_RE = re.compile(r"^\s*\[think\]\s*", re.I)
_P50_THINK_HEAD_RE = re.compile(r"^\s*(?:🤔\s*)?(?:思考|Thinking|Reasoning)\s*[:：]", re.I)
_P50_DIVIDER_RE = re.compile(r"\n\s*[─—━-]{3,}\s*\n")


def _p50_strip_reasoning(t: str) -> str:
    """删掉正文开头的推理块；没有分隔线时只丢第一段。"""
    t = _P50_TAG_RE.sub("", t)
    if _P50_THINK_HEAD_RE.match(t):
        m = _P50_DIVIDER_RE.search(t)
        if m:
            t = t[m.end():]
        else:
            parts = re.split(r"\n\s*\n", t, maxsplit=1)
            t = parts[1] if len(parts) > 1 else ""
    return t


# [astrbot-purge:P61] 技术轮次的「汇报腔」压缩。她答「检查一下」时会逐段念清单：
# 「09:55 那轮：用户那段加了…；怎么说话加了…；习惯那段删了…；要那段删了…」——
# 一段六个分号，全是过程，用户问的只是「有没有」。人格里已经加了「不报过程」，
# 这里做确定性兜底：一段里三个以上分号、且同时命中两个以上清单词，整段删掉。
# 只处理长文本，且不碰代码块；删过头（剩不到 40 字）就退回原文。
_P61_SEMI_SEG_RE = re.compile(
    r"[^\n。！？]*[；;][^\n。！？]*[；;][^\n。！？]*[；;][^\n。！？]*[。！？]?"
)
_P61_KEYS = ("段", "加", "删", "改", "备份", "调成", "换成")
# [astrbot-purge:P62] 词表补全。11-07 12:00 后漏网的 5 条全是这些形态：
# 「语法过了，插件重载成功，挑图的逻辑拿现有文件试过」(14:13)、
# 「加好了，重载过了。」(14:27)、「两个文件都留了备份，`.bak-20261107-214502`」(21:44)、
# 「备份在 data_v4.db.bak-tools-all-20261107-222505。」(22:25)、
# 「旧配置备份在 cmd_config.json.bak-20261107-233425。」(23:33)。
# 原来的词表只有「语法检查通过/编译通过/备份在/备份到」，这五条一条都盖不住。
# [astrbot-purge:P70] 11-08 09:36 那条一条都没盖住：
# 「重启确实起来了，清单最后一条勾掉。后端新进程 09:33:45 起，09:33:59 加载新代码…」
# journal 里另有「## 完成摘要 | 步骤 | 状态 |」「测试链路完整」「已完成」。
# 这几种是清单体换了个壳，词表按「勾 / 完成 / 链路」三族补。
_P61_LINE_RE = re.compile(
    r"[^\n。！？]*(?:备份在|备份到|留了备份|都留了备份|原文件备份|"
    r"语法过了|语法检查通过|编译通过|重载过了|重载成功|"
    r"改了三处|改了两处|改了一处|都在这一个文件里|"
    r"(?:清单|todo)[^\n。！？]{0,12}(?:勾掉|勾了|勾上|勾完)|"
    r"都勾了|全勾了|勾完了|链条完整|链路完整|测试链路|"
    r"已完成|已处理|处理完了|完成摘要|"
    r"\.bak-[0-9a-z])[^\n。！？]*[。！？]?"
)


# [astrbot-purge:P70c] 表格不算「过程句」，算结构。gate>=60 那条管不到它：
# 11-08 journal 里有 48 字的「## 完成摘要 / | 步骤 | 状态 | / | 读配置 | 已完成 |」，
# 短得让 P62 整条跳过。这里不看长度，只看「这是不是一张汇报表」。
_P70_PROC_KEYS = (
    "完成摘要", "已完成", "已处理", "勾掉", "勾完", "链路完整", "测试链路",
    "状态", "步骤",
)
_P70_HEAD_RE = re.compile(r"^\s*#{1,4}\s*(?:完成摘要|摘要|进度|结果|状态|清单)\s*$")
_P70_ROW_RE = re.compile(r"^\s*\|")


def _p70_strip_tables(t: str) -> str:
    if "|" not in t:
        return t
    lines = t.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if _P70_HEAD_RE.match(ln):
            i += 1
            continue
        if _P70_ROW_RE.match(ln):
            j = i
            while j < len(lines) and _P70_ROW_RE.match(lines[j]):
                j += 1
            block = lines[i:j]
            if any(k in "".join(block) for k in _P70_PROC_KEYS):
                i = j  # 整块表扔了，表头和分隔行也不留
                continue
        out.append(ln)
        i += 1
    return "\n".join(out)


def _p61_trim_report(t: str, total_len: int | None = None) -> str:
    """删掉「逐段念清单」的汇报段和「备份在 xxx」这类过程句。

    [P62] 门槛算错了。60/160 字这两个阈值是照「整条回复」定的，可这个函数是在
    分段之后逐段调用的（stage.py:635），而 QQ/网聊的回复早就按换行和 ||| 切成了
    20–50 字一条。于是 len(t)>=60 几乎永远不成立——11-07 12:00 后 6 条过程句里
    5 条原样发出去，段长 36/43/45/58。现在由调用方把整条回复的总长传进来当门限，
    段长只用于判断「删完是不是空了」。
    """
    if not t or "```" in t:
        return t
    orig = t
    t = _p70_strip_tables(t)
    if not t.strip():
        return orig  # 整条就是一张汇报表，删干净就成空消息了，宁可留着
    gate = len(t) if total_len is None else total_len

    def _seg(m: re.Match) -> str:
        s = m.group(0)
        if sum(1 for k in _P61_KEYS if k in s) >= 2:
            return ""
        return s

    def _line(m: re.Match) -> str:
        s = m.group(0)
        # [P62c] 短句整句都是过程，整句删（「备份在 xxx。」「语法过了，重载成功。」）。
        # 长句里往往只是夹了个备份名，真正的内容在旁边：15:30 那条
        # 「顺手把另外 10 份也带 BOM 的配置一起清了，每份都有 .bak-… 备份，
        # 清之前先验能解析：anysearch、livingmemory…」整句删会把清过哪几份也删掉。
        # 超过 60 字的句子只掐掉命中的那个分句。
        if len(s) <= 60:
            return ""
        _bits = re.split(r"(?<=[，,])", s)
        _keep = [b for b in _bits if not _P61_LINE_RE.search(b)]
        _out = "".join(_keep)
        return _out if _out.strip() and _out != s else ""

    if gate >= 60:
        t = _P61_LINE_RE.sub(_line, t)
    # [P62b] 整条都是过程话的短段（「加好了，重载过了。」12 字）会被整句删空。
    # 按逗号分句，只留下不是过程的那半截：「加好了」。
    if not t.strip() and len(orig) <= 40:
        _bits = re.split(r"(?<=[，,])", orig)
        _keep = [
            b
            for b in _bits
            if b.strip()
            and len(b.strip("，, ")) <= 8
            and not _P61_LINE_RE.search(b)
        ]
        _cand = "".join(_keep).strip().rstrip("，,").strip()
        if _cand and _cand != orig:
            t = _cand
    if gate >= 160:
        _before_semi = t
        t = _P61_SEMI_SEG_RE.sub(_seg, t)
        # 只有整段规则真的删了东西、又删过头（剩不到 40 字）才退回。
        # 退回到「分句兜底之后」的版本，不能退回到原文——否则
        # 「备份在 xxx。||| 人格是缓存的…」这种长回复又把备份名放回去了。
        if t != _before_semi and len(t.strip()) < 40:
            t = _before_semi
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    # 整段删掉后会留下空的 ||| 段，收拢掉
    if "|||" in t:
        _parts = [p.strip() for p in t.split("|||")]
        _parts = [p for p in _parts if p]
        if _parts:
            t = "\n|||\n".join(_parts)
    # [P70b] 一圈删下来什么都不剩，就退回原文。宁可留一句过程话，
    # 也不能发出一段空消息——那是「已读不回」，比 AI 腔更难解释。
    return t if t.strip() else orig


# [astrbot-purge:P51] 模型不肯用 ||| 时的兜底分条。真人在手机上很少一口气打完
# 60 字才按发送：先一句短的，再补一句。这里只在「纯单段口语回复」上动手，
# 代码块 / 多行结构 / 已含 ||| 的一律不碰。
_P51_SENT_RE = re.compile(r"[^。！？…~\n]+[。！？…~]*")


_P52_EMPTY_TOKENS = {"none", "null", "nil", "[think]", "think", "(empty)", "empty"}
_P51_RNG = random.Random()


def _p51_soft_split(text: str) -> str:
    t = text.strip()
    if not t or "|||" in t or "```" in t or "\n" in t:
        return text
    if len(t) < 34:
        return text
    sents = [s.strip() for s in _P51_SENT_RE.findall(t) if s.strip()]
    if len(sents) < 2:
        return text
    # [P51b] 不再固定「第一句单独一条」。之前每条都是「两字反应 + 一长段」，
    # 五轮下来又成了新节拍器。这里按比例换切点，也允许偶尔不切。
    roll = _P51_RNG.random()
    if roll < 0.40 and len(t) < 72:
        return text
    cut = 2 if (roll >= 0.62 and len(sents) >= 3) else 1
    parts = ["".join(sents[:cut])]
    rest = sents[cut:]
    tail = "".join(rest)
    if len(tail) > 64 and len(rest) >= 2:
        mid = len(rest) // 2
        parts.append("".join(rest[:mid]))
        parts.append("".join(rest[mid:]))
    else:
        parts.append(tail)
    parts = [p for p in parts if p.strip()]
    if len(parts) < 2:
        return text
    return " ||| ".join(parts)


# [astrbot-purge:P71] 正文里的工具调用/结果剥离。webchat 的 call_00 弧是 UI 卡片帧，
# 但模型也会把调用 JSON 降级成正文。这里把能还原的还原成代码块，纯工具结果直接丢。
_P71_CALL_RE = re.compile(
    r'\{\s*"id"\s*:\s*"call_[^"]{0,64}"\s*,\s*"(?:name|ts)"\s*:\s*'
)
_P71_BODY_KEYS = ("content", "command", "code", "script", "text")


def _p71_unleak(t: str) -> str:
    """[P71] 把「被写成正文的工具调用 / 工具结果」剥掉。

    2026-11-08 实测两种形态：
    1) 调用 —— {"id": "call_00_...", "name": "astrbot_file_write_tool",
       "args": {"content": "..."}}，14401 字。这个 call id 在 tool_loop_agent_runner
       日志里查不到，框架从头到尾没把它当工具调用，它只是文本。
    2) 结果 —— {"id": "call_00_...", "ts": 1794107198.0, "result": "{\"results\": [...]}"}，
       整坨搜索返回被粘进正文，22975 字里 22k 是它。
    core 段第 56 行明令禁止也没用：参数一长，模型就把调用降级成文本。
    出站口兜底：调用有 content/command 就还原成代码块，没正文键、或是结果，整段丢。
    """
    if not t or '"call_' not in t:
        return t
    dec = json.JSONDecoder()
    out: list[str] = []
    i = 0
    while True:
        m = _P71_CALL_RE.search(t, i)
        if not m:
            out.append(t[i:])
            break
        out.append(t[i:m.start()])
        try:
            obj, end = dec.raw_decode(t[m.start():])
        except ValueError:
            # 截断的 JSON（长脚本常见）：从这个位置切到结尾，别把半截 JSON 发出去
            i = len(t)
            break
        body = None
        args = obj.get("args")
        if isinstance(args, dict):
            for k in _P71_BODY_KEYS:
                v = args.get(k)
                if isinstance(v, str) and len(v.strip()) >= 40:
                    body = v
                    break
        if body:
            b = body.rstrip()
            out.append(b if b.lstrip().startswith("```") else "```\n" + b + "\n```")
        # 没有可回收的正文（纯工具结果 / 无 content 的调用）：整段丢，别发出去
        i = m.start() + end
    res = re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()
    if res != t:
        logger.info("[P71] 正文里的工具调用/结果已剥离 %d → %d 字", len(t), len(res))
    return res


# [P72] 技术轮的首段太长。她的技术回复习惯「先抛结论、再解释为什么」，两句
# 黏在同一段里，发出去就是一条 100+ 字的长消息。11-07 12:00 后 82 条技术轮，
# 整条中位 120.5、p90 399；首段中位 48.5、p90 105。这里只在「本段像技术内容」
# 时，把首段第一个句号后面切开，让结论独立成一条。一个字都不改，只加换行。
_P72_TECH_RE = re.compile(
    r"```|\.py\b|\.json\b|\.yaml\b|\.db\b|traceback|Traceback|Error|error|报错|日志|"
    r"脚本|插件|配置|端口|接口|路径|目录|重启|命令|数据库|进程|依赖|版本|编译|安装了"
)
_P72_TECH_SENT_RE = re.compile(r"[。！？]")


def _p72_lead_split(text: str, is_first: bool = False) -> str:
    """只给技术轮的第一段加切点，别的段一律不动。"""
    if not is_first or not text:
        return text
    if "\n" in text or "```" in text:
        return text
    if len(text) < 46:
        return text
    if not _P72_TECH_RE.search(text):
        return text
    m = _P72_TECH_SENT_RE.search(text)
    if m and 2 <= m.end() <= 50:
        return text[: m.end()] + "\n" + text[m.end() :].lstrip()
    # 没有落在合适位置的句号：退到逗号，但前半段不能太短，否则切出来只剩个「嗯，」。
    for i, ch in enumerate(text):
        if ch in "，," and 12 <= i + 1 <= 42:
            return text[: i + 1] + "\n" + text[i + 1 :].lstrip()
        if i > 50:
            break
    return text


def _p48_clean(text: str, total_len: int | None = None, is_first: bool = False) -> str:
    """只做表层清洗，不改语义，不碰正文里的动作/身体描写。"""
    if not text:
        return text
    # [P71] 先剥「被写成正文的工具调用」，再谈心语气；顺序反了会把代码块当正文洗
    text = _p71_unleak(text)
    if not text:
        return text
    t = _p50_strip_reasoning(text)
    # [P62] total_len 是整条回复的总字数（分段前），P61 的 60/160 门槛要用它，
    # 用段自己的长度永远打不开。
    t = _p61_trim_report(t, total_len)
    t = _P48_EMOJI_RE.sub("", t)
    t = _P48_SENTENCE_RE.sub("", t)
    # [P49] 不再删除句尾软问句：真人会用「行吗」「你看呢」收尾，
    # 一律删掉会让所有回复变成陈述句，反而更假。_P48_TAIL_Q_RE 停用。
    for w in _P48_LEAD_WORDS:
        t = t.replace(w, "")
    # [P53] 「我这边」是老版本的口头禅，人格里禁了但压不住先验概率。
    # 这里做确定性替换：我这边→我；你那边→你。
    t = t.replace("我这边", "我").replace("你那边", "你")
    t = re.sub(r"^[，、,。！？…~\s]+", "", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = _p72_lead_split(t.strip(), is_first)
    return t.strip()


@register_stage
class ResultDecorateStage(Stage):
    async def initialize(self, ctx: PipelineContext) -> None:
        self.ctx = ctx
        self.reply_prefix = ctx.astrbot_config["platform_settings"]["reply_prefix"]
        self.reply_with_mention = ctx.astrbot_config["platform_settings"][
            "reply_with_mention"
        ]
        self.reply_with_quote = ctx.astrbot_config["platform_settings"][
            "reply_with_quote"
        ]
        self.t2i_word_threshold = ctx.astrbot_config["t2i_word_threshold"]
        try:
            self.t2i_word_threshold = int(self.t2i_word_threshold)
            self.t2i_word_threshold = max(self.t2i_word_threshold, 50)
        except BaseException:
            self.t2i_word_threshold = 150
        self.t2i_strategy = ctx.astrbot_config["t2i_strategy"]
        self.t2i_use_network = self.t2i_strategy == "remote"
        self.t2i_active_template = ctx.astrbot_config["t2i_active_template"]

        self.forward_threshold = ctx.astrbot_config["platform_settings"][
            "forward_threshold"
        ]

        trigger_probability = ctx.astrbot_config["provider_tts_settings"].get(
            "trigger_probability",
            1,
        )
        try:
            self.tts_trigger_probability = max(
                0.0,
                min(float(trigger_probability), 1.0),
            )
        except (TypeError, ValueError):
            self.tts_trigger_probability = 1.0

        # 分段回复
        self.words_count_threshold = int(
            ctx.astrbot_config["platform_settings"]["segmented_reply"][
                "words_count_threshold"
            ],
        )
        self.enable_segmented_reply = ctx.astrbot_config["platform_settings"][
            "segmented_reply"
        ]["enable"]
        self.only_llm_result = ctx.astrbot_config["platform_settings"][
            "segmented_reply"
        ]["only_llm_result"]
        self.split_mode = ctx.astrbot_config["platform_settings"][
            "segmented_reply"
        ].get("split_mode", "regex")
        self.regex = ctx.astrbot_config["platform_settings"]["segmented_reply"]["regex"]
        self.split_words = ctx.astrbot_config["platform_settings"][
            "segmented_reply"
        ].get("split_words", ["。", "？", "！", "~", "…"])
        if self.split_words:
            escaped_words = sorted(
                [re.escape(word) for word in self.split_words], key=len, reverse=True
            )
            self.split_words_pattern = re.compile(
                f"(.*?({'|'.join(escaped_words)})|.+$)", re.DOTALL
            )
        else:
            self.split_words_pattern = None
        self.content_cleanup_rule = ctx.astrbot_config["platform_settings"][
            "segmented_reply"
        ]["content_cleanup_rule"]

        # exception
        self.content_safe_check_reply = ctx.astrbot_config["content_safety"][
            "also_use_in_response"
        ]
        self.content_safe_check_stage = None
        if self.content_safe_check_reply:
            for stage_cls in registered_stages:
                if stage_cls.__name__ == "ContentSafetyCheckStage":
                    self.content_safe_check_stage = stage_cls()
                    await self.content_safe_check_stage.initialize(ctx)

        provider_cfg = ctx.astrbot_config.get("provider_settings", {})
        self.show_reasoning = provider_cfg.get("display_reasoning_text", False)

    def _split_text_by_words(self, text: str) -> list[str]:
        """使用分段词列表分段文本"""
        if not self.split_words_pattern:
            return [text]

        segments = self.split_words_pattern.findall(text)
        result = []
        for seg in segments:
            if isinstance(seg, tuple):
                content = seg[0]
                if not isinstance(content, str):
                    continue
                for word in self.split_words:
                    if content.endswith(word):
                        content = content[: -len(word)]
                        break
                if content.strip():
                    result.append(content)
            elif seg and seg.strip():
                result.append(seg)
        return result if result else [text]

    async def process(
        self,
        event: AstrMessageEvent,
    ) -> None | AsyncGenerator[None, None]:
        result = event.get_result()
        if result is None or not result.chain:
            return
        # [astrbot-purge:P13] strip tool scaffolding before anything else sees it
        try:
            _purge_sanitize_result(result)
        except Exception:
            pass

        if result.result_content_type == ResultContentType.STREAMING_RESULT:
            return

        # [P71] 曾经以为要在分段前剥，实测这条路径上没有 JSON：webchat 那份
        # 「call_00_xxx」是 UI 的工具调用卡片帧（webchat_event.py:74-84 把 Json
        # 组件也打成 type=plain），不是要发给用户的正文。真正的兜底放在
        # _p48_clean 里，只对真的写进正文的情况生效。这里不再重复处理。

        is_stream = result.result_content_type == ResultContentType.STREAMING_FINISH

        # 回复时检查内容安全
        if (
            self.content_safe_check_reply
            and self.content_safe_check_stage
            and result.is_llm_result()
            and not is_stream  # 流式输出不检查内容安全
        ):
            text = ""
            for comp in result.chain:
                if isinstance(comp, Plain):
                    text += comp.text

            if isinstance(self.content_safe_check_stage, ContentSafetyCheckStage):
                async for _ in self.content_safe_check_stage.process(
                    event,
                    check_text=text,
                ):
                    yield

        # 发送消息前事件钩子
        handlers = star_handlers_registry.get_handlers_by_event_type(
            EventType.OnDecoratingResultEvent,
            plugins_name=event.plugins_name,
        )
        for handler in handlers:
            try:
                logger.debug(
                    f"hook(on_decorating_result) -> {star_map[handler.handler_module_path].name} - {handler.handler_name}",
                )
                if is_stream:
                    logger.warning(
                        "Plugins that depend on the pre-send event hook may not work "
                        "correctly when streaming output is enabled.",
                    )
                await handler.handler(event)

                if (result := event.get_result()) is None or not result.chain:
                    logger.debug(
                        "hook(on_decorating_result) -> "
                        f"{star_map[handler.handler_module_path].name} - "
                        f"{handler.handler_name} cleared the message result.",
                    )
            except BaseException:
                logger.error(traceback.format_exc())

            if event.is_stopped():
                logger.info(
                    f"{star_map[handler.handler_module_path].name} - "
                    f"{handler.handler_name} stopped event propagation.",
                )
                return

        # 流式输出不执行下面的逻辑
        if is_stream:
            return

        # 需要再获取一次。插件可能直接对 chain 进行了替换。
        result = event.get_result()
        if result is None:
            return

        if len(result.chain) > 0:
            # 回复前缀
            if self.reply_prefix:
                for comp in result.chain:
                    if isinstance(comp, Plain):
                        comp.text = self.reply_prefix + comp.text
                        break

            # 当 TTS 未启用时，移除模型直接生成的 Record（语音）组件，
            # 避免语音与文字内容重复发送。
            tts_enabled = bool(
                self.ctx.astrbot_config["provider_tts_settings"]["enable"]
            )
            if not tts_enabled:
                result.chain = [
                    comp for comp in result.chain
                    if not isinstance(comp, Record)
                ]

            # 分段回复
            if self.enable_segmented_reply and event.get_platform_name() not in [
                "qq_official_webhook",
                "weixin_official_account",
                "dingtalk",
            ]:
                if (
                    self.only_llm_result and result.is_model_result()
                ) or not self.only_llm_result:
                    new_chain = []
                    for comp in result.chain:
                        if isinstance(comp, Plain):
                            if len(comp.text) > self.words_count_threshold:
                                # 不分段回复
                                new_chain.append(comp)
                                continue

                            # [P51] 单段长回复先做一次软分条，补上模型不写 ||| 的情况
                            comp.text = _p51_soft_split(comp.text)

                            # 先按行切分，避免 DOTALL 贪婪匹配把多行并成一大段
                            line_parts = []
                            for _ln in comp.text.split("\n"):
                                _ln = _ln.strip()
                                if not _ln:
                                    continue
                                # [fix:sep] ||| 是人格约定的语音分段符，当硬分段点，
                                # 切完这一段就把它从正文里丢掉。
                                line_parts.extend(_split_on_segment_sep(_ln))
                            if not line_parts:
                                new_chain.append(comp)
                                continue

                            for line in line_parts:
                                # 根据 split_mode 选择分段方式
                                if self.split_mode == "words":
                                    split_response = self._split_text_by_words(line)
                                else:  # regex 模式
                                    try:
                                        split_response = re.findall(
                                            self.regex,
                                            line,
                                            re.DOTALL | re.MULTILINE,
                                        )
                                    except re.error:
                                        logger.error(
                                            "Invalid segmented-reply regular expression; "
                                            "using the default segmentation method: "
                                            f"{traceback.format_exc()}",
                                        )
                                        split_response = re.findall(
                                            r".*?[。？！~…]+|.+$",
                                            line,
                                            re.DOTALL | re.MULTILINE,
                                        )

                                if not split_response:
                                    new_chain.append(Plain(line))
                                    continue
                                for seg in split_response:
                                    if self.content_cleanup_rule:
                                        try:
                                            seg = re.sub(
                                                self.content_cleanup_rule, "", seg
                                            )
                                        except re.error:
                                            logger.error(
                                                "The segmented-reply filter expression "
                                                "failed, so the text could not be filtered: "
                                                f"{traceback.format_exc()}"
                                            )
                                            self.content_cleanup_rule = None
                                    seg = seg.strip()
                                    if seg:
                                        new_chain.append(Plain(seg))
                        else:
                            # 非 Plain 类型的消息段不分段
                            new_chain.append(comp)
                    result.chain = new_chain
                    # [P49] 去重停用：真人会把同一句再说一遍，尤其在床上和撒娇时。
                    # 全局去重会把这些重复整段抹掉，回复变得干净得不像人。
                    # [P49] 短段合并停用（原 P46）：把 <10 字的分段并进相邻段、
                    # 总段数压到 5 段以内，会把「嗯」「烫…」「啊？」这些真人短条
                    # 系统性消灭，只剩成段的书面回复。现在原样保留模型的切分。

            # [astrbot-purge:P48b] 表层签名清洗：在分段之后、TTS 之前，
            # 对模型回复做确定性处理（命令/插件结果不动）。
            if result.is_llm_result():
                # [P62] 整条回复的总字数，给 P61 当门槛用。必须在逐个清洗前算。
                _p48_total = sum(
                    len(c.text) for c in result.chain if isinstance(c, Plain)
                )
                _p48_chain = []
                _p48_plain_i = 0
                for comp in result.chain:
                    if isinstance(comp, Plain):
                        comp.text = _p48_clean(
                            _strip_segment_sep(comp.text),
                            _p48_total,
                            is_first=(_p48_plain_i == 0),
                        )
                        _p48_plain_i += 1
                        if not comp.text.strip():
                            continue
                        # [P52] 模型偶尔把占位符当正文吐出来（历史里 8 条字面 None，
                        # 还有整条只有 [think]）。这类不发给用户。
                        if comp.text.strip().lower() in _P52_EMPTY_TOKENS:
                            continue
                    _p48_chain.append(comp)
                result.chain = _p48_chain
                if not result.chain:
                    logger.warning("[P52] empty llm result dropped (None/[think])")
                    return

            # [fix:sep] 兜底：分段回复被关掉、或文本超过 words_count_threshold
            # 而跳过分段时，分隔符不能留在正文里被平台显示成竖线。
            for comp in result.chain:
                if isinstance(comp, Plain):
                    comp.text = _strip_segment_sep(comp.text)

            # TTS
            tts_provider = (
                await self.ctx.plugin_manager.context.get_using_tts_provider_async(
                    event.unified_msg_origin,
                )
            )

            # [astrbot-purge:P18a] 长文不转语音：TTS 只读短句，长正文留文字。
            # [astrbot-purge:P44] 过短的回复（≤4 字，如"好""来""嗯"）也不念，
            # QQ 侧这类短句只出文字，避免每条都弹一个语音条。
            _tts_text = "".join(
                c.text for c in result.chain if isinstance(c, Plain)
            ).strip()
            # [nsfw-voice] 露骨内容不看概率，必念；日常仍按 trigger_probability 抽。
            _tts_is_adult = _p47_is_adult(_tts_text)
            # [voice-only] 只留语音条模式：短句也念，否则"嗯""行"会掉成文字。
            # [nsfw-voice-len] 色色那几轮正文常超 600 字，按日常阈值会被整条
            # 判成"太长不念"掉回纯文字。露骨内容单独放到 3000 字。
            _tts_len_ok = (
                0 < len(_tts_text) <= 3000
                if _tts_is_adult
                else 0 < len(_tts_text) <= 600
            )
            # [astrbot-purge:P41] 网页聊天只出文字，语音条只留给 QQ。
            _tts_platform_ok = event.get_platform_name() != "webchat"
            # [fix:tts] 只给模型回复配语音。命令/插件结果（/provider 这类）本来是
            # 英文提示语，念出来又长又怪，而且 dual_output 会多出一条语音条。
            _tts_result_ok = result.is_llm_result()
            _tts_prob_ok = (
                True if _tts_is_adult else random.random() <= self.tts_trigger_probability
            )
            should_tts = (
                bool(self.ctx.astrbot_config["provider_tts_settings"]["enable"])
                # [astrbot-purge:P19] webchat 直连的结果类型不是 LLM_RESULT
                and True
                and _tts_result_ok
                and await SessionServiceManager.should_process_tts_request(event)
                and _tts_prob_ok
                and tts_provider
                and _tts_len_ok
                and _tts_platform_ok
            )
            if should_tts and not tts_provider:
                logger.warning(
                    f"Session {event.unified_msg_origin} has no text-to-speech "
                    "provider configured.",
                )

            show_reasoning = self.show_reasoning
            if (enable_reasoning := event.get_extra("enable_reasoning")) is not None:
                show_reasoning = bool(enable_reasoning)

            if (
                not should_tts
                and show_reasoning
                and event.get_extra("_llm_reasoning_content")
            ):
                # inject reasoning content to chain
                reasoning_content = str(event.get_extra("_llm_reasoning_content"))
                if event.get_platform_name() == "lark":
                    result.chain.insert(
                        0,
                        Json(
                            data={
                                "type": "lark_collapsible_panel_reasoning",
                                "title": "💭 Thinking",
                                "expanded": False,
                                "content": reasoning_content,
                            },
                        ),
                    )
                else:
                    result.chain.insert(
                        0, Plain(f"🤔 思考: {reasoning_content}\n\n────\n")
                    )

            if should_tts and tts_provider:
                new_chain = []
                _daily_voiced = 0
                for comp in result.chain:
                    # [astrbot-purge:P47b] 露骨分段必念；日常分段按低概率抽一条念，
                    # 一轮最多一条，长度 6–100 字。以前只有身体内容才出声，
                    # 日常几乎不发声，人味少了一半。
                    _voice_ok = False
                    if isinstance(comp, Plain) and len(comp.text) > 0:
                        # [voice-only] 开了只留语音条就每条都念，长短都念。
                        if self.ctx.astrbot_config["provider_tts_settings"][
                            "dual_output"
                        ] is False:
                            _voice_ok = True
                        elif _p47_is_adult(comp.text):
                            _voice_ok = True
                        elif (
                            6 <= len(comp.text) <= 100
                            and _daily_voiced < 1
                            and random.random() < 0.28
                        ):
                            _voice_ok = True
                            _daily_voiced += 1
                    if _voice_ok:
                        try:
                            logger.info(f"TTS request: {comp.text}")
                            audio_path = await tts_provider.get_audio(comp.text)
                            logger.info(f"TTS result: {audio_path}")
                            if not audio_path:
                                logger.error(
                                    "Failed to convert the message segment to speech "
                                    f"because no TTS audio file was found: {comp.text}",
                                )
                                new_chain.append(comp)
                                continue

                            use_file_service = self.ctx.astrbot_config[
                                "provider_tts_settings"
                            ]["use_file_service"]
                            callback_api_base = self.ctx.astrbot_config[
                                "callback_api_base"
                            ]
                            dual_output = self.ctx.astrbot_config[
                                "provider_tts_settings"
                            ]["dual_output"]

                            url = None
                            if use_file_service and callback_api_base:
                                token = await file_token_service.register_file(
                                    audio_path,
                                )
                                url = f"{callback_api_base}/api/file/{token}"
                                logger.debug(f"Registered: {url}")

                            new_chain.append(
                                Record(
                                    file=url or audio_path,
                                    url=url or audio_path,
                                    text=comp.text,
                                ),
                            )
                            if dual_output:
                                new_chain.append(comp)
                        except Exception:
                            logger.error(traceback.format_exc())
                            logger.error("TTS failed; sending text instead.")
                            new_chain.append(comp)
                    else:
                        new_chain.append(comp)
                result.chain = new_chain

            # 文本转图片
            elif (
                result.use_t2i_ is None and self.ctx.astrbot_config["t2i"]
            ) or result.use_t2i_:
                parts = []
                for comp in result.chain:
                    if isinstance(comp, Plain):
                        parts.append("\n\n" + comp.text)
                    else:
                        break
                plain_str = "".join(parts)
                if plain_str and len(plain_str) > self.t2i_word_threshold:
                    render_start = time.time()
                    try:
                        url = await html_renderer.render_t2i(
                            plain_str,
                            return_url=True,
                            use_network=self.t2i_use_network,
                            template_name=self.t2i_active_template,
                            endpoint=self.ctx.astrbot_config.get("t2i_endpoint")
                            or None,
                        )
                    except BaseException:
                        logger.error(
                            "Text-to-image rendering failed; sending text instead."
                        )
                        return
                    if time.time() - render_start > 3:
                        logger.warning(
                            "Text-to-image rendering took more than 3 seconds. Disable "
                            "text-to-image mode in the WebUI if it is too slow.",
                        )
                    if url:
                        if url.startswith("http"):
                            result.chain = [Image.fromURL(url)]
                        elif (
                            self.ctx.astrbot_config["t2i_use_file_service"]
                            and self.ctx.astrbot_config["callback_api_base"]
                        ):
                            token = await file_token_service.register_file(url)
                            url = f"{self.ctx.astrbot_config['callback_api_base']}/api/file/{token}"
                            logger.debug(f"Registered: {url}")
                            result.chain = [Image.fromURL(url)]
                        else:
                            result.chain = [Image.fromFileSystem(url)]

            # 触发转发消息
            if event.get_platform_name() == "aiocqhttp":
                word_cnt = 0
                for comp in result.chain:
                    if isinstance(comp, Plain):
                        word_cnt += len(comp.text)
                if word_cnt > self.forward_threshold:
                    node = Node(
                        uin=event.get_self_id(),
                        name="AstrBot",
                        content=[*result.chain],
                    )
                    result.chain = [node]

            # at 回复 / 引用回复仅适用于纯文本或图文消息
            can_decorate = all(
                isinstance(item, (Plain, Image)) for item in result.chain
            )
            if can_decorate:
                # at 回复
                if (
                    self.reply_with_mention
                    and event.get_message_type() != MessageType.FRIEND_MESSAGE
                ):
                    result.chain.insert(
                        0,
                        At(qq=event.get_sender_id(), name=event.get_sender_name()),
                    )
                    if len(result.chain) > 1 and isinstance(result.chain[1], Plain):
                        result.chain[1].text = "\n" + result.chain[1].text

                # 引用回复
                if self.reply_with_quote:
                    result.chain.insert(0, Reply(id=event.message_obj.message_id))
