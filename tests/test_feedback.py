"""`feedback` in crossweft.json: a failing check, actions and every agent hook name the
repository's feedback channel and the part of crossweft involved; without the key the
output is unchanged.

Run: python -m unittest discover -s tests -v   (stdlib only)
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "examples" / "polyglot-shop"
sys.path.insert(0, str(REPO))

from crossweft import cli, hooks  # noqa: E402
from crossweft.engine import ModelError, load_config  # noqa: E402

CHANNEL = "python report.py file --kind bug --title ..."


def run(argv: list[str]) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            code = cli.main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return code, buf.getvalue()


class Feedback(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cw-fb-"))
        self.root = self.tmp / "shop"
        shutil.copytree(DEMO, self.root)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def configure(self, value) -> None:
        path = self.root / "crossweft.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["feedback"] = value
        path.write_bytes(json.dumps(data, indent=2).encode("utf-8"))

    def break_seam(self) -> None:
        path = self.root / "web/public/index.html"
        text = path.read_text(encoding="utf-8")
        path.write_bytes(text.replace('data-action="place-order"', 'data-action="place-ordr"', 1)
                         .encode("utf-8"))

    def hook(self, event: str, payload: dict) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            hooks.run_hook("claude-code", event, json.dumps(payload))
        return buf.getvalue()

    def test_without_the_key_nothing_is_added(self) -> None:
        self.break_seam()
        code, output = run(["--root", str(self.root), "check"])
        self.assertEqual(code, 1, output)
        self.assertNotIn("crossweft feedback", output)

    def test_failing_check_and_actions_name_the_channel_and_the_command(self) -> None:
        self.configure(CHANNEL)
        self.break_seam()
        code, output = run(["--root", str(self.root), "check"])
        self.assertEqual(code, 1, output)
        self.assertIn(CHANNEL, output)
        self.assertIn("`crossweft check`", output)
        code, output = run(["--root", str(self.root), "actions"])
        self.assertEqual(code, 1, output)
        self.assertIn("`crossweft actions`", output)

    def test_a_passing_check_stays_quiet(self) -> None:
        self.configure(CHANNEL)
        code, output = run(["--root", str(self.root), "check"])
        self.assertEqual(code, 0, output)
        self.assertNotIn("crossweft feedback", output)

    def test_every_hook_message_names_its_part(self) -> None:
        self.configure(CHANNEL)
        start = self.hook("session-start", {"cwd": str(self.root)})
        self.assertIn("hook session-start", start)
        edit = self.hook("post-edit", {"cwd": str(self.root), "tool_input": {
            "file_path": str(self.root / "web/public/index.html")}})
        self.assertIn("hook post-edit", edit)
        self.break_seam()
        stop = self.hook("stop", {"cwd": str(self.root), "session_id": "t"})
        self.assertIn("hook stop", stop)
        self.assertIn(CHANNEL.replace('"', '\\"'), stop)

    def test_a_bad_value_is_a_config_error(self) -> None:
        for bad in ("", "   ", 7, "x" * 301, "x\n::error::forged", "x\rRESULT: PASS", "x\ty"):
            with self.subTest(bad=bad):
                self.configure(bad)
                with self.assertRaises(ModelError):
                    load_config(self.root)


if __name__ == "__main__":
    unittest.main()
