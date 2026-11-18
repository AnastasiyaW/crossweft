"""ui_actions docs: each control's help section is attested together with the code
behind the control; a change to either side stays red until re-attested."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ui_actions import ShopCopy, run  # noqa: E402

HELP = """# Admin help

## Load order {#load-order}

Fetches one order by its id and shows the server's answer.

## Place order {#place-order}

Creates an order with one item.
"""

MODEL = "seams/model/10-system.json"


class UiDocs(ShopCopy):
    def setUp(self) -> None:
        super().setUp()
        (self.root / "docs").mkdir()
        (self.root / "docs" / "help.md").write_bytes(HELP.encode("utf-8"))
        path = self.root / MODEL
        model = json.loads(path.read_text(encoding="utf-8"))
        model["ui_actions"][0]["docs"] = {"label": "admin help", "paths": ["docs/help.md"],
                                          "regex": "^##.*\\{#([a-z-]+)\\}"}
        path.write_bytes(json.dumps(model, indent=2).encode("utf-8"))

    def attest(self, *args: str) -> tuple[int, str]:
        return run(["--root", str(self.root), "attest", "--ui", "admin-buttons", *args])

    def green(self) -> None:
        code, output = run(["--root", str(self.root), "check"])
        self.assertEqual(code, 0, output)

    def test_unattested_then_green(self) -> None:
        self.assertEqual(self.keys(), {"ui:admin-buttons:doc-unattested:load-order",
                                       "ui:admin-buttons:doc-unattested:place-order"})
        code, output = self.attest("--all", "--reason", "read both")
        self.assertEqual(code, 0, output)
        self.green()

    def test_handler_change_makes_help_stale(self) -> None:
        self.attest("--all", "--reason", "read both")
        self.edit("web/public/app.js", "items: [\"sku-1\"]", "items: [\"sku-1\", \"sku-2\"]")
        keys = self.keys()
        self.assertEqual(len(keys), 1, keys)
        self.assertTrue(next(iter(keys)).startswith("ui:admin-buttons:doc-stale:place-order:"))
        # code moved, help did not: attest refuses without the explicit statement
        code, output = self.attest("place-order", "--reason", "re-read")
        self.assertEqual(code, 1, output)
        self.assertIn("REFUSED", output)
        code, output = self.attest("place-order", "--reason", "re-read, still true",
                                   "--other-side-unchanged")
        self.assertEqual(code, 0, output)
        self.green()

    def test_help_change_alone_is_reported(self) -> None:
        self.attest("--all", "--reason", "read both")
        self.edit("docs/help.md", "one item.", "two items.")
        keys = self.keys()
        self.assertEqual(len(keys), 1, keys)
        self.assertTrue(next(iter(keys)).startswith("ui:admin-buttons:doc-stale:place-order:"))

    def test_crlf_checkout_is_not_a_change(self) -> None:
        self.attest("--all", "--reason", "read both")
        for rel in ("docs/help.md", "web/public/app.js"):
            path = self.root / rel
            path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        self.green()

    def test_missing_and_orphan_sections(self) -> None:
        self.attest("--all", "--reason", "read both")
        self.edit("docs/help.md", "{#place-order}", "{#cancel-order}")
        self.assertEqual(self.keys(), {"ui:admin-buttons:undocumented:place-order",
                                       "ui:admin-buttons:doc-orphan:cancel-order",
                                       "ui:admin-buttons:doc-stale-lock:place-order"})
        code, output = run(["--root", str(self.root), "attest", "--prune"])
        self.assertEqual(code, 0, output)
        self.assertNotIn("doc-stale-lock", " ".join(self.keys()))

    def test_unknown_ui_and_missing_reason(self) -> None:
        code, output = run(["--root", str(self.root), "attest", "--ui", "nope", "--all",
                            "--reason", "x"])
        self.assertEqual(code, 2, output)
        code, output = self.attest("--all")
        self.assertEqual(code, 2, output)


if __name__ == "__main__":
    unittest.main()
