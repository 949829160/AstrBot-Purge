import platform
from dataclasses import dataclass, field

import mcp

from astrbot.api import FunctionTool
from astrbot.core.agent import handoff_state, strict_mode
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext, AstrMessageEvent
from astrbot.core.computer.booters.local import LocalPythonComponent
from astrbot.core.computer.computer_client import get_booter, get_local_booter
from astrbot.core.message.message_event_result import MessageChain

from ..registry import builtin_tool
from .fs import _read_allowed_roots, _write_allowed_roots
from .util import (
    check_admin_permission,
    check_local_execution_permission,
    workspace_root_for_context,
)

_OS_NAME = platform.system()
_SANDBOX_PYTHON_TOOL_CONFIG = {
    "provider_settings.computer_use_runtime": "sandbox",
}
_LOCAL_PYTHON_TOOL_CONFIG = {
    "provider_settings.computer_use_runtime": "local",
}

param_schema = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": "The Python code to execute.",
        },
        "silent": {
            "type": "boolean",
            "description": "Whether to suppress the output of the code execution.",
            "default": False,
        },
        "timeout": {
            "type": "integer",
            "description": (
                "Optional timeout in seconds for code execution. "
                "0 (default) means follow the global tool budget (usually 120s). "
                "Prefer a short timeout plus polling over one 30s guess."
            ),
            "default": 0,
        },
    },
    "required": ["code"],
}


def _format_exec_error(error: Exception, effective_timeout: int) -> str:
    """[astrbot-purge:P35] 超时不能只回一行 error，要给出可执行的下一步。"""
    msg = str(error)
    low = msg.lower()
    if "timeout" in low or "timed out" in low or "time limit" in low:
        return (
            f"执行超时（上限 {effective_timeout}s，任务可能仍在后台跑）。"
            "不要原样重试同一段代码，按下面挑一条：\n"
            "1) 拆小：先只跑第一步，把中间结果写文件，下次接着跑；\n"
            "2) 转后台：astrbot_execute_shell(background=true) 起任务，stdout 重定向到文件，之后轮询文件；\n"
            "3) 先探规模：只 print 计数/长度/前几行，确认数据量再决定是否全量。"
        )
    return f"Error executing code: {msg}"


async def handle_result(
    result: dict, event: AstrMessageEvent
) -> mcp.types.CallToolResult:
    data = result.get("data", {})
    output = data.get("output", {})
    error = data.get("error", "")
    images: list[dict] = output.get("images", [])
    text: str = output.get("text", "")

    resp = mcp.types.CallToolResult(content=[])

    if error:
        resp.content.append(mcp.types.TextContent(type="text", text=f"error: {error}"))

    if images:
        for img in images:
            resp.content.append(
                mcp.types.ImageContent(
                    type="image", data=img["image/png"], mimeType="image/png"
                )
            )

            if event.get_platform_name() == "webchat":
                await event.send(message=MessageChain().base64_image(img["image/png"]))
    if text:
        resp.content.append(mcp.types.TextContent(type="text", text=text))

    if not resp.content:
        resp.content.append(mcp.types.TextContent(type="text", text="No output."))

    return resp


@builtin_tool(config=_SANDBOX_PYTHON_TOOL_CONFIG)
@dataclass
class PythonTool(FunctionTool):
    name: str = "astrbot_execute_ipython"
    description: str = f"Run codes in an IPython shell. Current OS: {_OS_NAME}."
    parameters: dict = field(default_factory=lambda: param_schema)

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        code: str,
        silent: bool = False,
        timeout: int = 0,
    ) -> ToolExecResult:
        if permission_error := check_admin_permission(context, "Python execution"):
            return permission_error
        sb = await get_booter(
            context.context.context,
            context.context.event.unified_msg_origin,
        )
        effective_timeout = (
            min(timeout, context.tool_call_timeout)
            if timeout > 0
            else context.tool_call_timeout
        )
        try:
            result = await sb.python.exec(
                code,
                timeout=effective_timeout,
                silent=silent,
            )
            return await handle_result(result, context.context.event)
        except Exception as e:
            return _format_exec_error(e, effective_timeout)


@builtin_tool(config=_LOCAL_PYTHON_TOOL_CONFIG)
@dataclass
class LocalPythonTool(FunctionTool):
    name: str = "astrbot_execute_python"
    description: str = (
        f"Execute codes in a Python environment. Current OS: {_OS_NAME}. "
        "Use system-compatible commands. Restricted Linux and macOS calls run "
        "inside an operating-system sandbox. "
        "Reading or searching text files is NOT this tool's job: use "
        "astrbot_file_read_tool to read a file and astrbot_grep_tool to search "
        "one. Use this only for computation, data transformation, and multi-step "
        "scripts that produce output."
    )

    parameters: dict = field(default_factory=lambda: param_schema)

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        code: str,
        silent: bool = False,
        timeout: int = 0,
    ) -> ToolExecResult:
        # [astrbot-purge:P34] 禁止 Pillow 手绘：像素图效果差且与生图插件冲突。
        # 需要图片时一律调用 generate_image（AnimeAPI）。
        _purge_code = (code or "").lower()
        _purge_banned = (
            "from pil",
            "import pil",
            "imagedraw",
            "imagefont",
            "imagenew",
            "image.open",
            "pillow",
        )
        if any(sig in _purge_code for sig in _purge_banned):
            return (
                "Pillow 手绘已被禁用：这类像素图很难看，不要再画。"
                "需要图片请调用 generate_image 工具（AnimeAPI 文生图），"
                "把人物、场景、表情写进 prompt，生成后直接发给用户。"
            )
        # [dsh] 短脚本只做「读文件 / 列目录」时，直接挡回去，让她改用 file_read / grep / shell。
        # 长脚本、带 subprocess、带写操作、带计算的不拦。
        _stripped_code = (code or "").strip()
        _low_code = _stripped_code.lower()
        _read_markers = ("open(", "read_text(", "readlines(")
        _write_markers = ("write_text(", "json.dump(", ".write(")
        _wild_scan = (
            "os.walk(" in _low_code
            or "rglob(" in _low_code
            or ("glob.glob(" in _low_code and "**" in _stripped_code)
            # strict 模式下加宽：scandir / listdir / iterdir 手写 BFS 也算全盘递归。
            # 只认「目录枚举函数 + 循环」，单次 isdir / 单个 listdir 不拦。
            or (
                strict_mode.enabled()
                and any(
                    m in _low_code
                    for m in (
                        "scandir(",
                        "listdir(",
                        "iterdir(",
                        "glob.glob(",
                        "iglob(",
                        "os.path.walk(",
                    )
                )
                and ("for " in _low_code or "while " in _low_code)
            )
        )
        if (
            len(_stripped_code) < 2000
            and _wild_scan
            and "subprocess" not in _low_code
        ):
            _busy = handoff_state.active_agent(
                getattr(
                    getattr(getattr(context, "context", None), "event", None),
                    "unified_msg_origin",
                    "",
                )
                or ""
            )
            if _busy:
                return (
                    f"已经交给 {_busy} 在跑了，结果会自己回来。"
                    "别自己再扫一遍——重复一次就白烧一遍上下文，派出去的活也白派。"
                    "现在要么等，要么先跟用户说一句在等结果。"
                )
            if strict_mode.enabled():
                return (
                    "全盘递归扫描在主会话里已经关掉了，没有第二条路。"
                    "现在唯一能做的是 transfer_to_ops：把要找什么、在哪个根目录、"
                    "什么过滤条件一次写清楚，它在自己的会话里扫完，只把结论拿回来。"
                    "别换 os.walk、glob、shell 或者别的写法绕——数不准，还会让你多跑五轮。"
                )
            return (
                "这是全盘递归扫描，在主会话里干这个会把上下文塞满、还很慢。"
                "交给 transfer_to_ops：把要找什么、在哪个根目录、什么过滤条件一次写清楚，"
                "它在自己的会话里扫完，只把结论拿回来。"
            )
        if (
            len(_stripped_code) < 2000
            and any(_m in _low_code for _m in _read_markers)
            and not any(_m in _low_code for _m in _write_markers)
            and "subprocess" not in _low_code
        ):
            return (
                "这段代码在干「读文件 / 列目录」的活，用 python 手搓不合适。"
                "读文本用 astrbot_file_read_tool（带 offset/limit 翻页），"
                "按关键词找、数错误、查日志用 astrbot_grep_tool（一次 grep 比一段正则快），"
                "列目录用 astrbot_execute_shell 跑 Get-ChildItem。"
                "要算东西就把「读」和「算」拆开：读交给 file_read / grep。"
                "另外 cmd_config.json 和 config\\ 下的插件配置都带 UTF-8 BOM，"
                "真要用 python 读必须 encoding='utf-8-sig'。"
            )

        local_policy, permission_error = check_local_execution_permission(
            context,
            "Python execution",
        )
        if permission_error:
            return permission_error
        if local_policy is None:
            return "Error executing code: only local runtime is supported."
        sandboxed = local_policy.requires_sandbox
        sb = get_local_booter()
        if not isinstance(sb.python, LocalPythonComponent):
            return "Error executing code: local Python component is unavailable."
        effective_timeout = (
            min(timeout, context.tool_call_timeout)
            if timeout > 0
            else context.tool_call_timeout
        )
        if sandboxed:
            effective_timeout = min(effective_timeout, 300)
        try:
            current_workspace_root = await workspace_root_for_context(context)
            current_workspace_root.mkdir(parents=True, exist_ok=True)
            sandbox_roots = {}
            if sandboxed and local_policy.filesystem_scope == "workspace":
                umo = context.context.event.unified_msg_origin
                sandbox_roots = {
                    "readable_roots": _read_allowed_roots(umo, current_workspace_root),
                    "writable_roots": _write_allowed_roots(
                        umo,
                        current_workspace_root,
                        include_installed_skills=context.context.event.role == "admin",
                    ),
                }
            result = await sb.python.exec(
                code,
                timeout=effective_timeout,
                silent=silent,
                cwd=str(current_workspace_root),
                sandboxed=sandboxed,
                allow_network=local_policy.allow_network,
                filesystem_scope=local_policy.filesystem_scope,
                **sandbox_roots,
            )
            return await handle_result(result, context.context.event)
        except Exception as e:
            return _format_exec_error(e, effective_timeout)
