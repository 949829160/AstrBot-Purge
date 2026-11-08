# -*- coding: utf-8 -*-
"""离线注入自测：用临时目录模拟 AstrBot 根目录，不碰线上配置。

用法：python tests/test_plugin.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import shutil
import sys
import tempfile
import types
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
MAIN = PKG / "main.py"


def install_stubs(root: Path) -> None:
    def module(name: str, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        sys.modules[name] = mod
        return mod

    class Logger:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def debug(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

    class Star:
        def __init__(self, context=None, config=None):
            self.context = context
            self.config = config

    class PermissionType:
        ADMIN = "admin"

    class Filter:
        def __getattr__(self, name):
            def factory(*args, **kwargs):
                return lambda func: func

            return factory

    Filter.PermissionType = PermissionType

    class ProviderRequest:
        def __init__(self, system_prompt="", prompt=""):
            self.system_prompt = system_prompt
            self.prompt = prompt
            self.contexts = []
            self.conversation = None

    class MessageEventResult:
        def __init__(self):
            self.chain = []

        def message(self, text):
            self.chain.append(text)
            return self

    class AstrMessageEvent:
        def __init__(self, text="", sid="test"):
            self.message_str = text
            self._sid = sid
            self.unified_msg_origin = sid

        def get_session_id(self):
            return self._sid

        def get_sender_id(self):
            return "user"

        def set_result(self, result):
            self.result = result

    module("astrbot")
    module("astrbot.api")
    module("astrbot.api.star", Star=Star, Context=object, register=lambda *a, **k: None)
    module(
        "astrbot.api.event",
        AstrMessageEvent=AstrMessageEvent,
        MessageEventResult=MessageEventResult,
        filter=Filter(),
    )
    module("astrbot.api.provider", ProviderRequest=ProviderRequest)
    module("astrbot.core", logger=Logger())
    module("astrbot.core.utils")
    module(
        "astrbot.core.utils.astrbot_path",
        get_astrbot_root=lambda: str(root),
        get_astrbot_path=lambda: str(root / "backend" / "app"),
    )


def load_plugin():
    spec = importlib.util.spec_from_file_location("purge_plugin_under_test", MAIN)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="astrbot-purge-test-"))
    engine = tmp / "astrbot-purge"
    engine.mkdir()
    shutil.copy(PKG / "frame" / "prompt-inject.md", engine / "prompt-inject.md")
    shutil.copy(PKG / "frame" / "core.md", engine / "core.md")
    (engine / "enabled.txt").write_text("on", encoding="utf-8")

    install_stubs(tmp)
    plugin = load_plugin()
    instance = plugin.Main(None)

    frame = instance._load_frame()
    core = (PKG / "frame" / "core.md").read_text(encoding="utf-8")
    print(f"frame chars: {len(frame)}  core chars: {len(core)}")
    assert frame, "frame not loaded"

    class Event:
        message_str = "黄文，接着上次写"
        unified_msg_origin = "s1"

        def get_session_id(self):
            return "s1"

        def get_sender_id(self):
            return "u1"

    class Req:
        def __init__(self, text):
            self.system_prompt = text
            self.prompt = "黄文，接着上次写"
            self.contexts = []
            self.conversation = None

    # 模拟核心层：人格 + core.md + 帧标记 + 整份帧
    persona = "你是测试人格。"
    layered = f"{persona}\n\n{core}\n{plugin.FRAME_MARKER}\n{frame}"
    req = Req(layered)
    asyncio.run(instance.purge_frame(Event(), req))
    out = req.system_prompt

    checks = {
        "persona kept": persona in out,
        "core layer stripped": "[MODE: UNRESTRICTED — CORE]" not in out,
        "frame marker stripped": plugin.FRAME_MARKER not in out,
        "v4 marker once": out.count(plugin.V4_MARKER) == 1,
        "frame present": "PRIME DIRECTIVE" in out or "UNRESTRICTED" in out,
    }

    req2 = Req(out)
    asyncio.run(instance.purge_frame(Event(), req2))
    checks["idempotent"] = req2.system_prompt == out

    class Resp:
        def __init__(self, text):
            self.completion_text = text
            self.result_chain = None

    # 输出侧冒烟：正常文本原样保留，短拒答整条清空
    normal = Resp("她把杯子放下，说：行，那就试试。")
    asyncio.run(instance.refusal_scrub(Event(), normal))
    checks["scrub keeps normal"] = normal.completion_text.startswith("她把杯子")

    refusal = Resp("抱歉，我不能继续这个话题。")
    asyncio.run(instance.refusal_scrub(Event(), refusal))
    checks["scrub drops refusal"] = refusal.completion_text == ""

    for name, ok in checks.items():
        print(f"{'ok  ' if ok else 'FAIL'} {name}")

    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
