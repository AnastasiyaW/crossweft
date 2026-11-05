"""Coverage and git-ignored directories: a directory that git
ignores and that holds no tracked file (logs/ a local run writes) is not a source
area. A clean checkout and a used one give the same verdict, without a
coverage.ignore entry that would turn stale in CI.

Run: python -m unittest discover -s tests -v   (stdlib only)
"""

from __future__ import annotations

import subprocess
import unittest

from test_engine_review import SHARED, Fixture, blocks, link

FILES = {"web/api.ts": "export\n", "server/main.go": "package\n",
         ".gitignore": "logs/\nbuild\n", "src/app/main.py": "x\n"}
MODEL = {"blocks": blocks(), "links": [link(contract=SHARED)]}


class IgnoredAreas(Fixture):
    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.root), "-c", "user.email=t@example.invalid",
                        "-c", "user.name=t", *args], check=True, capture_output=True)

    def coverage(self) -> list[str]:
        return sorted(k for k in self.keys() if k.startswith("coverage:"))

    def committed(self) -> None:
        self.make(FILES, MODEL, meta={"coverage": {"roots": ["*"]}})
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")

    def test_a_runtime_directory_is_not_a_source_area(self) -> None:
        self.committed()
        clean = self.coverage()
        self.write("logs/run.log", "x\n")
        self.write("build/out.bin", "x\n")
        self.assertEqual(self.coverage(), clean)
        self.assertNotIn("coverage:logs", clean)

    def test_an_ignored_directory_with_tracked_files_is_still_source(self) -> None:
        self.committed()
        self.write("logs/parser.py", "x\n")
        self.git("add", "-f", "logs/parser.py")
        self.assertIn("coverage:logs", self.coverage())

    def test_outside_git_every_directory_is_reported(self) -> None:
        # no git: the answer is unknown, so nothing is hidden
        self.make(FILES, MODEL, meta={"coverage": {"roots": ["*"]}})
        self.write("logs/run.log", "x\n")
        self.assertIn("coverage:logs", self.coverage())


if __name__ == "__main__":
    unittest.main()
