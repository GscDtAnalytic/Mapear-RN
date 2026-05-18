#!/usr/bin/env python3
"""Pre-commit check: fail if staged ``src/`` modules contain real print() calls.

AST-based on purpose — a ``print(...)`` that appears inside a docstring or a
comment (a usage example) is not a violation; only an actual call expression
is. The previous implementation grepped raw text and false-positived on
docstring examples.
"""

from __future__ import annotations

import ast
import subprocess
import sys


def _staged_src_files() -> list[str]:
    """Staged Python files living under any package's ``src/`` tree."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [
        line
        for line in result.stdout.split()
        if "/src/" in line and line.endswith(".py")
    ]


def _has_print_call(path: str) -> bool:
    """True if the module has at least one ``print(...)`` call expression."""
    try:
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
    except (OSError, SyntaxError):
        return False
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
        for node in ast.walk(tree)
    )


def main() -> int:
    offenders = [path for path in _staged_src_files() if _has_print_call(path)]
    if offenders:
        sys.stderr.write("print() call found in production code:\n")
        for path in offenders:
            sys.stderr.write(f"  {path}\n")
        sys.stderr.write(
            "Use a logger instead, or move the call to a CLI entrypoint.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
