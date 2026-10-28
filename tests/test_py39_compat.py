"""The package supports Python 3.9; CI runs it, local machines usually do not."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def newline_write_text_calls(source: str) -> list[int]:
    """Lines calling `<x>.write_text(..., newline=...)`: the keyword is Python 3.10+."""
    return [node.lineno for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "write_text"
            and any(k.arg == "newline" for k in node.keywords)]


class Python39(unittest.TestCase):
    def test_the_detector_sees_the_call(self) -> None:
        self.assertEqual(newline_write_text_calls('p.write_text("x", newline="\\n")'), [1])
        self.assertEqual(newline_write_text_calls('open(p, "w", newline="\\n")'), [])

    def test_no_write_text_with_newline_anywhere(self) -> None:
        found = [f"{path.relative_to(REPO)}:{line}"
                 for path in sorted(REPO.rglob("*.py")) if ".git" not in path.parts
                 for line in newline_write_text_calls(path.read_text(encoding="utf-8"))]
        self.assertEqual(found, [], "Path.write_text(newline=) needs Python 3.10; "
                                    "use write_bytes(text.encode()) or open(newline=)")


if __name__ == "__main__":
    unittest.main()
