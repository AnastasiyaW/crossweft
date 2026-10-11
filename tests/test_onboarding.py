"""First-run experience: `init --example`, discover with nothing to suggest,
and the messages a newcomer meets first.

Run: python -m unittest discover -s tests -v   (stdlib only)
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from crossweft import cli, engine  # noqa: E402

PY_SIDE = "crossweft-example/api/version.py"
TS_SIDE = "crossweft-example/web/version.ts"


def run(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli.main(argv)
    return code, buffer.getvalue()


class OnboardingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="crossweft-onboarding-")
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def cmd(self, *argv: str) -> tuple[int, str]:
        return run(["--root", str(self.root), *argv])

    def edit(self, rel: str, old: str, new: str) -> None:
        path = self.root / rel
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new), encoding="utf-8")

    def test_init_example_is_green_then_red_naming_the_other_file(self) -> None:
        code, output = self.cmd("init", "--example")
        self.assertEqual(code, 0, output)
        self.assertIn(TS_SIDE, output)
        code, output = self.cmd("check")
        self.assertEqual(code, 0, output)
        self.assertIn("RESULT: PASS", output)
        self.edit(PY_SIDE, '"2026-10-01"', '"2026-11-01"')
        code, output = self.cmd("check")
        self.assertEqual(code, 1, output)
        failing = [line for line in output.splitlines() if "disagrees" in line]
        self.assertEqual(len(failing), 1, output)
        self.assertIn(TS_SIDE, failing[0])

    def test_init_example_without_agents_is_green_too(self) -> None:
        self.assertEqual(self.cmd("init", "--example", "--no-agents")[0], 0)
        code, output = self.cmd("check")
        self.assertEqual(code, 0, output)

    def test_init_example_refuses_to_overwrite(self) -> None:
        (self.root / "crossweft-example").mkdir()
        (self.root / "crossweft-example" / "mine.txt").write_text("keep", encoding="utf-8")
        code, output = self.cmd("init", "--example")
        self.assertEqual(code, 2, output)
        self.assertIn("[ERR]", output)
        self.assertFalse((self.root / engine.CONFIG_FILE).exists(), "nothing may be written")
        self.assertEqual((self.root / "crossweft-example" / "mine.txt").read_text(
            encoding="utf-8"), "keep")

    def test_example_files_are_lf_only(self) -> None:
        self.cmd("init", "--example", "--no-agents")
        for rel in (PY_SIDE, TS_SIDE):
            self.assertNotIn(b"\r", (self.root / rel).read_bytes(), rel)

    def test_model_comment_points_at_a_url(self) -> None:
        self.cmd("init", "--no-agents")
        body = json.loads((self.root / "seams/model/10-system.json").read_text(encoding="utf-8"))
        self.assertIn("https://github.com/AnastasiyaW/crossweft/", body["$comment"])
        self.assertIn("docs/model-reference.md", body["$comment"])

    def test_discover_with_nothing_found_prints_a_starter_snippet(self) -> None:
        self.cmd("init", "--no-agents")
        code, output = self.cmd("discover")
        self.assertEqual(code, 0, output)
        self.assertNotIn("confirm each candidate", output)
        self.assertIn('"from_anchors"', output)
        self.assertIn('"joins"', output)

    def test_join_regex_matching_nothing_is_a_join_problem_with_the_raw_regex(self) -> None:
        self.cmd("init", "--example", "--no-agents")
        self.edit(PY_SIDE, "API_VERSION =", "API_REVISION =")
        code, output = self.cmd("check", "--format", "json")
        self.assertEqual(code, 1, output)
        rows = [p for p in json.loads(output)["new"]
                if p["key"] == f"anchor:join:example-api-version:{PY_SIDE}"]
        self.assertEqual(len(rows), 1, output)   # key unchanged
        self.assertEqual(rows[0]["category"], "joins")
        self.assertIn('API_VERSION = "([^"]+)"', rows[0]["message"])   # not JSON-escaped

    def test_missing_link_anchors_show_an_example_shape(self) -> None:
        self.cmd("init", "--example", "--no-agents")
        path = self.root / "seams/model/20-example.json"
        model = json.loads(path.read_text(encoding="utf-8"))
        del model["links"][0]["from_anchors"]
        path.write_text(json.dumps(model), encoding="utf-8")
        code, output = self.cmd("check", "--format", "json")
        self.assertEqual(code, 1, output)
        rows = [p for p in json.loads(output)["new"]
                if p["key"] == "evidence:example-web-api:from_anchors"]
        self.assertEqual(len(rows), 1, output)
        self.assertIn('"from_anchors": [{"path": ', rows[0]["message"])


if __name__ == "__main__":
    unittest.main()
