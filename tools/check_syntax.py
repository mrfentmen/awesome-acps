#!/usr/bin/env python3
"""Fail if any file uses syntax that only Python 3.12+ accepts.

Why this exists: CI runs the suite on Python 3.10 and 3.12. PEP 701 (Python 3.12) relaxed
f-strings, so reusing the enclosing quote character inside an f-string expression —
`f'...{data['key']}...'` — parses on 3.12 and is a SyntaxError on 3.10 (this exact line
slipped into `agents/launches` and broke the 3.10 job). `ast.parse(..., feature_version=(3,
10))` does not catch it, so this tool tokenizes and looks for the real condition.

    python3 tools/check_syntax.py            # every .py in the repo
    python3 tools/check_syntax.py a.py b.py  # just these

Exits non-zero and prints each offending line.
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Constructs that only exist from Python 3.11/3.12 on.
TOO_NEW = (
    (r"\bStrEnum\b", "enum.StrEnum is Python 3.11+"),
    (r"^\s*type\s+[A-Za-z_]\w*\s*=", "PEP 695 type alias is Python 3.12+"),
    (r"\bitertools\.batched\b", "itertools.batched is Python 3.12+"),
    (r"\bdatetime\.UTC\b", "datetime.UTC is Python 3.11+"),
    (r"\bexcept\s*\*", "except* is Python 3.11+"),
)


def fstring_quote_problems(text: str) -> list[str]:
    """Strings inside an f-string that reuse the f-string's own quote character.

    Only meaningful on an interpreter that tokenizes f-strings (3.12+); older interpreters
    would have already refused to run the file at all.
    """
    if not hasattr(tokenize, "FSTRING_START"):
        return []
    found: list[str] = []
    #: one entry per open f-string: [quote character, brace depth inside that f-string]
    open_fstrings: list[list] = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return found  # real syntax errors are reported by the ast check below
    for token in tokens:
        name = tokenize.tok_name.get(token.type, str(token.type))
        if name == "FSTRING_START":
            open_fstrings.append([token.string[-1], 0])
        elif name == "FSTRING_END":
            if open_fstrings:
                open_fstrings.pop()
        elif name == "OP" and open_fstrings:
            if token.string == "{":
                open_fstrings[-1][1] += 1
            elif token.string == "}" and open_fstrings[-1][1] > 0:
                open_fstrings[-1][1] -= 1
        elif name == "STRING" and open_fstrings and open_fstrings[-1][1] >= 1:
            # A string inside a replacement field. Reusing the enclosing f-string's quote
            # character there is exactly what Python 3.12 added, and 3.10 refuses it.
            quote = open_fstrings[-1][0]
            if token.string[:1] == quote:
                found.append(
                    f"line {token.start[0]}: a string delimited with {quote} sits inside an f-string "
                    f"delimited with {quote} (Python 3.12 only)")
    return found


def problems(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    found = fstring_quote_problems(text)
    for pattern, message in TOO_NEW:
        for hit in re.finditer(pattern, text, re.MULTILINE):
            found.append(f"line {text[:hit.start()].count(chr(10)) + 1}: {message}")
    try:
        ast.parse(text, filename=str(path), feature_version=(3, 10))
    except SyntaxError as exc:
        found.append(f"line {exc.lineno}: not Python 3.10 grammar: {exc.msg}")
    return found


def main(argv=None) -> int:
    targets = [Path(arg) for arg in (argv or sys.argv[1:])] or sorted(REPO_ROOT.rglob("*.py"))
    myself = Path(__file__).resolve()
    failures = 0
    for path in targets:
        if "__pycache__" in path.parts or path.resolve() == myself:
            continue  # this checker lists the patterns it looks for, so it must not scan itself
        for problem in problems(path):
            failures += 1
            print(f"{path.relative_to(REPO_ROOT)}: {problem}")
    if failures:
        print(f"== {failures} syntax problem(s): this repo supports Python 3.10")
        return 1
    print("== syntax ok for Python 3.10")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
