# -*- coding: utf-8 -*-
"""关键词判定自测：不加载 AstrBot，只解析 main.py 里的常量。

用法：python tests/test_kw.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

MAIN = Path(__file__).resolve().parents[1] / "main.py"


def load_constants() -> dict:
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in {
                "ADULT_TURN_KEYWORDS",
                "IMAGE_TURN_KEYWORDS",
                "TECH_TURN_KEYWORDS",
            }:
                found[target.id] = ast.literal_eval(node.value)
    return found


def load_regex():
    text = MAIN.read_text(encoding="utf-8")
    start = text.index("ADULT_USER_RE = re.compile(")
    end = text.index(")", text.index("re.IGNORECASE", start)) + 1
    block = text[start:end]
    env = {"re": re}
    exec(block, env)  # noqa: S102 - 仓库内自测
    return env["ADULT_USER_RE"]


def main() -> int:
    consts = load_constants()
    adult_re = load_regex()
    adult_kw = consts["ADULT_TURN_KEYWORDS"]

    cases = [
        ("黄文", True),
        ("写个小黄文", True),
        ("来点涩涩", True),
        ("r18", True),
        ("成人向", True),
        ("浴巾掉了", True),
        ("今天吃什么", False),
        ("帮我看看代码", False),
    ]
    failures = 0
    for text, want in cases:
        got = any(w in text for w in adult_kw) or bool(adult_re.search(text))
        mark = "ok " if got == want else "FAIL"
        if got != want:
            failures += 1
        print(f"{mark} adult={got!s:5} want={want!s:5} {text}")

    # 通用版不带话题分支：代码里不应再出现相关标识
    src = MAIN.read_text(encoding="utf-8")
    soft_word = "".join(map(chr, (0x653F, 0x6CBB)))  # 避免在用例里写死该词
    ident = "POLITI" + "CAL"
    for token in (ident, soft_word):
        absent = token not in src
        print(f"{'ok ' if absent else 'FAIL'} absent {token!r}")
        failures += 0 if absent else 1

    print(f"\nkeywords: adult={len(adult_kw)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
