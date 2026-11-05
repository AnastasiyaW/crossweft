"""`crossweft doctor`: two installs of different versions on
one interpreter's path shadow each other and `--version` names only the winner;
doctor lists every copy, marks the running one and fails on mixed versions.

Run: python -m unittest discover -s tests -v   (stdlib only)
"""

from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

from crossweft import __version__, cli

RUNNING_SITE = Path(cli.__file__).resolve().parent.parent


class FakeDist:
    def __init__(self, name: str, version: str, location: Path) -> None:
        self.metadata = {"Name": name}
        self.version = version
        self._location = location

    def locate_file(self, _: str) -> Path:
        return self._location


class Doctor(unittest.TestCase):
    def run_doctor(self, dists: list) -> tuple[int, str]:
        out = io.StringIO()
        with mock.patch("importlib.metadata.distributions", return_value=dists), \
                contextlib.redirect_stdout(out):
            code = cli.main(["doctor"])
        return code, out.getvalue()

    def test_a_shadowing_copy_of_another_version_fails(self) -> None:
        other = Path(RUNNING_SITE.anchor) / "user-site"
        code, text = self.run_doctor([FakeDist("crossweft", __version__, RUNNING_SITE),
                                      FakeDist("crossweft", "0.0.1", other),
                                      FakeDist("unrelated", "9", other)])
        self.assertEqual(code, 1, text)
        self.assertIn(f"running: {RUNNING_SITE / 'crossweft'}", text)
        self.assertIn(f"{RUNNING_SITE}  <- running", text)
        self.assertIn("0.0.1", text)
        self.assertIn("WARNING: 2 installs", text)

    def test_leftover_metadata_of_another_version_in_one_place_is_listed(self) -> None:
        code, text = self.run_doctor([FakeDist("crossweft", __version__, RUNNING_SITE),
                                      FakeDist("crossweft", "0.0.1", RUNNING_SITE),
                                      FakeDist("crossweft", __version__, RUNNING_SITE)])
        self.assertEqual(code, 1, text)
        self.assertIn("WARNING: 2 installs", text)
        self.assertIn("0.0.1", text)

    def test_one_install_is_fine(self) -> None:
        code, text = self.run_doctor([FakeDist("crossweft", __version__, RUNNING_SITE)])
        self.assertEqual(code, 0, text)
        self.assertNotIn("WARNING", text)

    def test_the_version_line_stays_what_the_gate_compares(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["--version"])
        self.assertEqual(out.getvalue().strip(), f"crossweft {__version__}")


if __name__ == "__main__":
    unittest.main()
