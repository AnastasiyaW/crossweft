"""Built-in hazard rules (crossweft/hazards.py): every planted unsafe fixture
fires exactly its rule, every safe rewrite of it stays quiet, and `check`
reports them through `hazards.include` -- red with a stable key, excusable by a
finding, and red when a glob names no file.

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
FIXTURES = REPO / "tests" / "fixtures" / "hazards"
sys.path.insert(0, str(REPO))

from crossweft import cli, hazards  # noqa: E402

EXPECTED = {
    "write_status.cmd": "cmd-digit-redirect",
    "make_runner.ps1": "cmd-digit-redirect",
    "read_status.ps1": "empty-to-zero",
    "read_status.py": "empty-to-zero",
    "build.cmd": "toolchain-env-name",
    "build.sh": "toolchain-env-name",
    "check_revision.sh": "glob-length-guard",
}


def scan(path: Path) -> list:
    return hazards.scan(path.name, path.read_text(encoding="utf-8"))


def run(argv: list) -> tuple:
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
        code = cli.main(argv)
    return code, stdout.getvalue()


class Fixtures(unittest.TestCase):
    def test_every_rule_has_a_planted_fixture(self):
        self.assertEqual(set(EXPECTED.values()), set(hazards.RULES))
        self.assertEqual({p.name for p in (FIXTURES / "unsafe").iterdir()}, set(EXPECTED))
        self.assertEqual({p.name for p in (FIXTURES / "safe").iterdir()}, set(EXPECTED))

    def test_unsafe_fires_exactly_its_rule(self):
        for name, rule in EXPECTED.items():
            with self.subTest(name=name):
                self.assertEqual([h.rule for h in scan(FIXTURES / "unsafe" / name)], [rule])

    def test_safe_rewrite_is_quiet(self):
        for name in EXPECTED:
            with self.subTest(name=name):
                self.assertEqual(scan(FIXTURES / "safe" / name), [])

    def test_planted_self_test_is_green(self):
        self.assertEqual(hazards.self_test(), [])

    def test_keys_follow_text_not_line_numbers(self):
        text = (FIXTURES / "unsafe" / "build.cmd").read_text(encoding="utf-8")
        before = hazards.keys("b.cmd", hazards.scan("b.cmd", text))
        moved = hazards.keys("b.cmd", hazards.scan("b.cmd", "rem moved\n" + text))
        changed = hazards.keys("b.cmd", hazards.scan("b.cmd", text.replace("RC=", "RC= ")))
        self.assertEqual(before, moved)
        self.assertNotEqual(before, changed)

    def test_rule_subset(self):
        text = (FIXTURES / "unsafe" / "build.cmd").read_text(encoding="utf-8")
        self.assertEqual(hazards.scan("b.cmd", text, {"glob-length-guard"}), [])


class CheckCommand(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="crossweft-hazards-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        run(["--root", str(self.root), "init"])

    def configure(self, hazards_cfg: dict, source: str = "unsafe") -> None:
        shutil.copytree(FIXTURES / source, self.root / "scripts", dirs_exist_ok=True)
        path = self.root / "crossweft.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg["hazards"] = hazards_cfg
        path.write_text(json.dumps(cfg), encoding="utf-8")

    def check(self) -> tuple:
        return run(["--root", str(self.root), "check", "--format", "json"])

    def hazard_keys(self, output: str) -> list:
        doc = json.loads(output)
        return sorted(p["key"] for p in doc["new"] if p["category"] == "hazards")

    def test_unsafe_scripts_fail_with_one_key_per_hazard(self):
        self.configure({"include": ["scripts/*"]})
        code, output = self.check()
        self.assertEqual(code, 1)
        rules = sorted(key.split(":")[2] for key in self.hazard_keys(output))
        self.assertEqual(rules, sorted(EXPECTED.values()))

    def test_safe_scripts_add_nothing(self):
        self.configure({"include": ["scripts/*"]}, source="safe")
        code, output = self.check()
        self.assertEqual(self.hazard_keys(output), [], output)

    def test_rules_and_exclude_narrow_the_scan(self):
        self.configure({"include": ["scripts/*"], "exclude": ["scripts/*.sh"],
                        "rules": ["toolchain-env-name"]})
        _, output = self.check()
        self.assertEqual([k.split(":")[1] for k in self.hazard_keys(output)], ["scripts/build.cmd"])

    def test_glob_naming_no_file_is_red(self):
        self.configure({"include": ["scripts/*.cmd", "nowhere/*.bat"]}, source="safe")
        code, output = self.check()
        self.assertEqual(code, 1)
        self.assertIn("hazard-config:empty:nowhere/*.bat", output)

    def test_unknown_rule_is_a_config_error(self):
        self.configure({"include": ["scripts/*"], "rules": ["no-such-rule"]})
        code, _ = self.check()
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
