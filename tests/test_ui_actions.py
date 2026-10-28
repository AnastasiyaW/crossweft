"""ui_actions: each planted break of the polyglot-shop admin page is red with
its own key, and the intact example is green."""

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

from crossweft import cli, ui_actions  # noqa: E402


def run(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli.main(argv)
    return code, buffer.getvalue()


class ShopCopy(unittest.TestCase):
    """A fresh copy of the example per test, and the keys `check` raises on it."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cw-ui-"))
        self.root = self.tmp / "shop"
        shutil.copytree(DEMO, self.root)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def edit(self, rel: str, old: str, new: str) -> None:
        path = self.root / rel
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text, f"fixture drifted: {old!r} not in {rel}")
        path.write_bytes(text.replace(old, new, 1).encode("utf-8"))  # write_text(newline=) is 3.10+

    def keys(self) -> set[str]:
        code, output = run(["--root", str(self.root), "check", "--json"])
        self.assertEqual(code, 1, output)
        return {p["key"] for p in json.loads(output)["new"]}


class UiActionBreaks(ShopCopy):
    def test_intact_example_traces_every_button(self) -> None:
        code, output = run(["--root", str(self.root), "actions", "--json"])
        self.assertEqual(code, 0, output)
        rows = json.loads(output)["ui_actions"][0]["controls"]
        self.assertEqual({r["control"] for r in rows}, {"load-order", "place-order"})
        self.assertTrue(all(r["steps"][-1]["kind"] == "route" for r in rows))

    def test_button_renamed(self) -> None:
        self.edit("web/public/index.html", 'data-action="place-order"', 'data-action="place-ordr"')
        keys = self.keys()
        self.assertIn("ui:admin-buttons:dead:place-ordr", keys)
        self.assertIn("ui:admin-buttons:orphan:place-order", keys)

    def test_handler_dropped_from_table(self) -> None:
        self.edit("web/public/app.js", '  "place-order": placeOrder,\n', "")
        self.assertIn("ui:admin-buttons:dead:place-order", self.keys())

    def test_handler_calls_unmapped_route(self) -> None:
        self.edit("web/public/app.js", 'fetch("/v1/orders", {', 'fetch("/v1/refunds", {')
        keys = self.keys()
        self.assertIn("ui:admin-buttons:unrouted:place-order", keys)
        self.assertIn("route:client-unexplained:/v1/refunds", keys)

    def test_button_removed_leaves_orphan_handler(self) -> None:
        self.edit("web/public/index.html",
                  '  <button data-action="load-order">Load order</button>\n', "")
        self.assertIn("ui:admin-buttons:orphan:load-order", self.keys())

    def test_action_bound_twice(self) -> None:
        self.edit("web/public/app.js", '  "place-order": placeOrder,\n',
                  '  "place-order": placeOrder,\n  "place-order": loadOrder,\n')
        self.assertIn("ui:admin-buttons:conflict:ACTIONS table:place-order", self.keys())

    def test_controls_that_extract_nothing_are_loud(self) -> None:
        self.edit("web/public/index.html", 'data-action="load-order"', 'data-act="load-order"')
        self.edit("web/public/index.html", 'data-action="place-order"', 'data-act="place-order"')
        keys = self.keys()
        self.assertTrue(any(k.startswith("anchor:ui:admin-buttons:items:") for k in keys), keys)
        self.assertEqual(run(["--root", str(self.root), "actions"])[0], 2)


class UiActionMisconfigurations(ShopCopy):
    """Settings that once ended green although nothing proved the controls handled."""

    def entry(self) -> tuple[Path, dict]:
        path = self.root / "seams/model/10-system.json"
        return path, json.loads(path.read_text(encoding="utf-8"))

    def save(self, path: Path, model: dict) -> None:
        path.write_bytes(json.dumps(model, indent=2).encode("utf-8"))

    def test_misspelt_regex_is_loud_even_when_prefix_regex_matches(self) -> None:
        path, model = self.entry()
        controls = model["ui_actions"][0]["controls"]
        controls["regex"] = "data-actoin=\"([a-z-]+)\""
        controls["prefix_regex"] = "data-action=\"(load-)"
        model["ui_actions"][0]["hops"][0]["prefix_regex"] = "\"(load-)"
        model["ui_actions"][0]["orphans"] = False
        self.save(path, model)
        self.assertIn("anchor:ui:admin-buttons:items:buttons in index.html:regex", self.keys())

    def test_fully_computed_control_is_dead(self) -> None:
        self.edit("web/public/app.js", "function show(text) {",
                  "function addButton(kind) {\n  const b = document.createElement('button');\n"
                  "  b.dataset.action = `${kind}`;\n}\n\nfunction show(text) {")
        path, model = self.entry()
        model["ui_actions"][0]["controls"]["paths"].append("web/public/app.js")
        model["ui_actions"][0]["controls"]["prefix_regex"] = "dataset\\.action = `([^`$]*)\\$\\{"
        self.save(path, model)
        self.assertIn("ui:admin-buttons:dead:*", self.keys())

    def test_chain_ending_at_a_rule_is_not_route_verified(self) -> None:
        path, model = self.entry()
        model["ui_actions"][0]["hops"][0]["prefix_regex"] = "\"(place-)"
        self.save(path, model)
        self.edit("web/public/index.html", 'data-action="place-order"', 'data-action="place-other"')
        self.assertIn("ui:admin-buttons:unrouted:place-other", self.keys())

    def test_actions_agrees_with_check(self) -> None:
        path, model = self.entry()
        model["ui_actions"][0]["allow"] = [{"item": "place-order", "reason": "test"}]
        self.save(path, model)
        self.edit("web/public/app.js", '  "place-order": placeOrder,\n', "")
        self.assertEqual(run(["--root", str(self.root), "check"])[0], 0)
        code, output = run(["--root", str(self.root), "actions", "--json"])
        self.assertEqual(code, 0, output)
        statuses = {c["control"]: c["status"]
                    for c in json.loads(output)["ui_actions"][0]["controls"]}
        self.assertEqual(statuses["place-order"], "allowed")

    def test_orphan_alone_fails_actions_too(self) -> None:
        self.edit("web/public/index.html",
                  '  <button data-action="load-order">Load order</button>\n', "")
        self.assertEqual(run(["--root", str(self.root), "actions"])[0], 1)

    def test_actions_for_one_entry_ignores_the_others(self) -> None:
        path, model = self.entry()
        other = json.loads(json.dumps(model["ui_actions"][0]))
        other["id"] = "other"
        other["controls"]["regex"] = "data-action=\"(place-order)\""
        other["hops"][0]["within"] = "const NOTHING = \\{([^}]*)\\}"
        model["ui_actions"].append(other)
        self.save(path, model)
        self.assertEqual(run(["--root", str(self.root), "actions", "admin-buttons"])[0], 0)
        self.assertNotEqual(run(["--root", str(self.root), "actions", "other"])[0], 0)

    def test_two_hops_with_one_label_are_a_schema_error(self) -> None:
        path, model = self.entry()
        model["ui_actions"][0]["hops"][1]["label"] = "ACTIONS table"
        self.save(path, model)
        code, output = run(["--root", str(self.root), "check"])
        self.assertEqual(code, 2, output)
        self.assertIn("names two sides", output)

    def test_template_route_is_a_parameter_not_a_cut(self) -> None:
        self.edit("web/public/app.js", 'fetch("/v1/orders", {', 'fetch(`/v1/${kind}`, {')
        self.assertIn("ui:admin-buttons:unrouted:place-order", self.keys())

    def test_concatenated_path_ending_in_slash_is_not_every_route_below(self) -> None:
        self.edit("web/public/app.js", 'fetch("/v1/orders", {', 'fetch("/v1/" + kind, {')
        self.assertIn("ui:admin-buttons:unrouted:place-order", self.keys())

    def test_undeclared_path_ending_in_slash_is_unrouted(self) -> None:
        self.edit("web/public/app.js", 'fetch("/v1/orders", {', 'fetch("/v1/orders/", {')
        self.assertIn("ui:admin-buttons:unrouted:place-order", self.keys())

    def test_query_string_is_not_part_of_the_route(self) -> None:
        self.edit("web/public/app.js", 'fetch("/v1/orders", {', 'fetch(`/v1/orders?src=${kind}`, {')
        code, output = run(["--root", str(self.root), "actions", "--json"])
        self.assertEqual(code, 0, output)
        steps = {c["control"]: c["steps"][-1]["key"]
                 for c in json.loads(output)["ui_actions"][0]["controls"]}
        self.assertEqual(steps["place-order"], "/v1/orders")


class UiActionResolution(unittest.TestCase):
    def test_module_self_test(self) -> None:
        self.assertEqual(ui_actions.self_test(), [])


if __name__ == "__main__":
    unittest.main()
