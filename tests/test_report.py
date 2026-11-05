"""`crossweft report`: the map with what the check found on it.

A copy of the demo gets one breakage of each kind the report draws: a route the
server serves that is not on the map, a mapped route nobody serves, a client
literal the map does not explain, and a button without a handler. Each must land
on the right link or block, in the right routes row and chain; `render` must stay
the model alone.

Run: python -m unittest discover -s tests -v   (stdlib only)
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from crossweft import engine, report

DEMO = Path(__file__).resolve().parent.parent / "examples" / "polyglot-shop"


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, (path, old)
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


class ModelTextInThePage(unittest.TestCase):
    def test_comment_and_script_text_cannot_end_the_data_script(self) -> None:
        with tempfile.TemporaryDirectory(prefix="crossweft-escape-") as tmp:
            root = Path(tmp) / "shop"
            shutil.copytree(DEMO, root)
            system = root / "seams" / "model" / "10-system.json"
            data = json.loads(system.read_text(encoding="utf-8"))
            data["blocks"][0]["notes"] = "x <!--<script> y </script> z"
            system.write_text(json.dumps(data), encoding="utf-8")
            result = engine.evaluate(engine.load_config(root))
            template = engine.load_config(root).template_path.read_text(encoding="utf-8")
            page = engine.Renderer(result.checker.model).viewer(template)
            self.assertNotIn("<!--<script", page)
            self.assertEqual(page.count("</script>"), template.count("</script>"))
            self.assertIn("x \\u003c!--\\u003cscript> y", page)


class PluginHookErrors(unittest.TestCase):
    def test_a_bad_probe_timeout_is_one_stderr_line_not_a_traceback(self) -> None:
        from unittest import mock
        from crossweft import harness, hooks
        err = io.StringIO()
        with mock.patch.object(hooks, "project_hooks_run",
                               side_effect=harness.HarnessError("CROSSWEFT_LAUNCHER_PROBE_TIMEOUT='abc': "
                                                                "must be a number of seconds")), \
                contextlib.redirect_stderr(err):
            code = hooks.run_hook("claude-code", "stop", json.dumps({"cwd": str(DEMO)}),
                                  from_plugin=True)
        self.assertEqual(code, 1)
        self.assertEqual(len(err.getvalue().splitlines()), 1, err.getvalue())
        self.assertIn("NOT checked", err.getvalue())


class Report(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="crossweft-report-")
        self.root = Path(self._tmp.name) / "shop"
        shutil.copytree(DEMO, self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def cfg(self) -> engine.Config:
        return engine.load_config(self.root)

    def run_report(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = report.run_report(self.cfg(), *args)
        return code, out.getvalue()

    def break_demo(self) -> None:
        edit(self.root / "server" / "main.go", 'v1.Post("/orders", createOrder)',
             'v1.Get("/admin/stats", getOrder)')
        edit(self.root / "web" / "public" / "app.js", "show(`HTTP ${res.status}: ${await res.text()}`);\n}",
             "show(`HTTP ${res.status}: ${await res.text()}`);\n  await fetch(\"/v1/ghost\");\n}")
        edit(self.root / "web" / "public" / "index.html", '<button data-action="place-order">',
             '<button data-action="refund">Refund</button>\n  <button data-action="place-order">')

    def test_each_breakage_is_placed_on_the_map(self) -> None:
        self.break_demo()
        data = report.payload(engine.evaluate(self.cfg()))
        placed = {p["key"]: p["refs"] for p in data["problems"]}
        self.assertEqual(placed, {
            "route:unmapped:GET /v1/admin/stats": ["block:api"],
            "route:unserved:POST /v1/orders": ["link:web-orders"],
            "route:client-unexplained:/v1/ghost": ["block:web"],
            "ui:admin-buttons:dead:refund": ["link:web-orders"]})
        self.assertFalse(data["ok"])

    def test_routes_table_has_the_three_sources_and_the_verdict(self) -> None:
        self.break_demo()
        rows = {(r["method"], r["path"]): r for r in
                report.payload(engine.evaluate(self.cfg()))["routes"]["rows"]}
        self.assertEqual(rows[("GET", "/v1/admin/stats")]["status"], "unmapped")
        self.assertEqual(rows[("GET", "/v1/admin/stats")]["links"], [])
        self.assertTrue(rows[("GET", "/v1/admin/stats")]["served"])
        unserved = rows[("POST", "/v1/orders")]
        self.assertEqual((unserved["status"], unserved["served"]), ("unserved", []))
        self.assertEqual([l["id"] for l in unserved["links"]], ["web-orders"])
        self.assertTrue(unserved["called"])
        self.assertEqual(rows[("", "/v1/ghost")]["status"], "client-unexplained")
        healthy = rows[("GET", "/healthz")]
        self.assertEqual((healthy["status"], [l["id"] for l in healthy["links"]]), ("ok", ["probe-health"]))
        self.assertTrue(healthy["served"])

    def test_button_chains_show_where_they_break(self) -> None:
        self.break_demo()
        controls = {c["control"]: c for a in report.payload(engine.evaluate(self.cfg()))["actions"]
                    for c in a["controls"]}
        self.assertEqual(controls["refund"]["status"], "dead")
        self.assertEqual(controls["place-order"]["status"], "ok")
        self.assertEqual([s["kind"] for s in controls["place-order"]["steps"]][-1], "route")

    def test_the_page_carries_the_report_and_render_does_not(self) -> None:
        self.break_demo()
        target = Path(self._tmp.name) / "out" / "report.html"
        code, text = self.run_report(str(target), False)
        self.assertEqual(code, 1, text)
        page = target.read_text(encoding="utf-8")
        self.assertIn('"report":{', page)
        self.assertIn("route:unserved:POST /v1/orders", page)
        viewer = engine.generated_pages(engine.load_model(self.cfg()))["viewer.html"]
        self.assertNotIn('"report":{', viewer)

    def test_a_clean_demo_reports_no_breakage(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            engine.run_render(self.cfg(), check_only=False)
        code, text = self.run_report(None, True)
        self.assertEqual(code, 0, text)
        data = json.loads(text)
        self.assertEqual((data["ok"], data["problems"]), (True, []))
        self.assertTrue(all(r["status"] == "ok" for r in data["routes"]["rows"]))


if __name__ == "__main__":
    unittest.main()
