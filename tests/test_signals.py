"""Signals on the map: `check` fails when a link, a running block
or a flow has no alarm, and `crossweft signals` prints the catalog.

Each test breaks a copy of the demo in one way and expects exactly that key.

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

from crossweft import engine, report, signals

DEMO = Path(__file__).resolve().parent.parent / "examples" / "polyglot-shop"
SYSTEM = Path("seams") / "model" / "10-system.json"
META = Path("seams") / "model" / "00-meta.json"


class Signals(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="crossweft-signals-")
        self.root = Path(self._tmp.name) / "shop"
        shutil.copytree(DEMO, self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def cfg(self) -> engine.Config:
        return engine.load_config(self.root)

    def edit(self, rel: str, old: str, new: str) -> None:
        path = self.root / rel
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def model(self, change) -> None:
        path = self.root / SYSTEM
        data = json.loads(path.read_text(encoding="utf-8"))
        change({kind: {e["id"]: e for e in data[kind]} for kind in ("blocks", "links", "flows")},
               data)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def keys(self) -> set[str]:
        result = engine.evaluate(self.cfg())
        self.assertFalse(result.schema_errors, result.schema_errors)
        return {p.key for p in result.new if p.category in ("signals", "anchors")}

    def run_signals(self) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = signals.run_signals(self.cfg(), None)
        return code, json.loads(out.getvalue())

    def test_the_demo_is_fully_watched(self) -> None:
        self.assertEqual(self.keys(), set())
        code, data = self.run_signals()
        self.assertEqual(code, 0)
        self.assertEqual([r["id"] for r in data["rules"]],
                         ["health:web", "health:api", "heartbeat:worker", "red:web-orders",
                          "probe:read-order", "watchdog"])
        api = data["rules"][1]
        self.assertEqual((api["url"], api["severity"]), ("https://shop.example.com/healthz", "crit"))
        beat = data["rules"][2]
        self.assertEqual((beat["warn_after"], beat["crit_after"]), ("90s", "3m"))
        self.assertEqual([s["ref"] for s in data["skipped"]], ["link:probe-health"])

    def test_a_link_without_a_signal_fails_check_and_the_catalog(self) -> None:
        self.model(lambda e, _: e["links"]["web-orders"].pop("signals"))
        self.assertEqual(self.keys(), {"signal:missing:link:web-orders"})
        code, data = self.run_signals()
        self.assertEqual((code, [m["key"] for m in data["missing"]]),
                         (1, ["signal:missing:link:web-orders"]))

    def test_a_flow_without_a_probe_is_placed_on_the_flow(self) -> None:
        self.model(lambda e, _: e["flows"]["read-order"].pop("signals"))
        result = engine.evaluate(self.cfg())
        placed = {p["key"]: p["refs"] for p in report.payload(result)["problems"]}
        self.assertEqual(placed, {"signal:missing:flow:read-order": ["flow:read-order"]})

    def test_a_service_without_health_or_heartbeat_fails(self) -> None:
        self.model(lambda e, _: e["blocks"]["worker"].pop("signals"))
        self.assertEqual(self.keys(), {"signal:missing:block:worker"})

    def test_the_heartbeat_sender_must_exist_in_code(self) -> None:
        self.edit("worker/worker.py", "def beat(", "def pulse(")
        self.assertEqual(self.keys(), {"anchor:block:worker.signals.heartbeat:worker/worker.py"})

    def test_a_health_route_the_server_does_not_register_fails(self) -> None:
        self.model(lambda e, _: e["blocks"]["api"]["signals"].update(health="GET /health"))
        self.assertEqual(self.keys(), {"signal:health-unserved:block:api:GET /health"})

    def test_the_trace_header_must_reach_both_ends(self) -> None:
        self.edit("web/src/api.ts", ", traceparent: traceparent()", "")
        self.edit("web/src/api.ts", "function traceparent()", "function requestId()")
        self.edit("web/src/api.ts", "// W3C trace context", "// request id")
        self.assertEqual(self.keys(), {"signal:trace:link:web-orders:from"})

    def test_a_queue_needs_depth_age_and_its_dead_letters(self) -> None:
        def add_queue(e, data):
            data["links"].append({"id": "api-worker-jobs", "from": "api", "to": "worker",
                                  "transport": "queue", "status": "current",
                                  "summary": "Paid orders to ship.",
                                  "contract": {"name": "ship job", "enforcement": "none"}})
        self.model(add_queue)
        result = engine.evaluate(self.cfg())
        self.assertIn("signal:missing:link:api-worker-jobs", {p.key for p in result.new})
        self.model(lambda e, _: e["links"]["api-worker-jobs"].update(
            signals={"queue": {"name": "ship-jobs"}}))
        self.assertTrue(any("missing 'dlq'" in err
                            for err in engine.evaluate(self.cfg()).schema_errors))
        self.model(lambda e, _: e["links"]["api-worker-jobs"].update(
            signals={"queue": {"name": "ship-jobs", "dlq": "ship-jobs-dead"}}))
        code, data = self.run_signals()
        kinds = {r["id"]: r for r in data["rules"]}
        self.assertEqual(code, 0, data["missing"])
        self.assertEqual(kinds["queue:api-worker-jobs"]["max_age"], {"warn": "10m", "crit": "30m"})
        self.assertEqual((kinds["dlq:api-worker-jobs"]["queue"], kinds["dlq:api-worker-jobs"]["severity"]),
                         ("ship-jobs-dead", "crit"))

    def test_rules_are_opt_in_and_signals_need_the_policy(self) -> None:
        meta = self.root / META
        data = json.loads(meta.read_text(encoding="utf-8"))
        data["meta"].pop("signals")
        meta.write_text(json.dumps(data), encoding="utf-8")
        errors = engine.evaluate(self.cfg()).schema_errors
        self.assertTrue(any("signals need meta.signals" in e for e in errors), errors)

        def strip(e, _):
            for kind in e.values():
                for entry in kind.values():
                    entry.pop("signals", None)
        self.model(strip)
        result = engine.evaluate(self.cfg())
        self.assertEqual((result.schema_errors, [p.key for p in result.new
                                                 if p.category == "signals"]), ([], []))

    def test_skip_cannot_hide_a_declared_signal(self) -> None:
        self.model(lambda e, _: e["links"]["web-orders"]["signals"].update(skip="later"))
        errors = engine.evaluate(self.cfg()).schema_errors
        self.assertTrue(any("'skip' says the entry has no signal" in e for e in errors), errors)

    def test_a_skipped_entry_may_still_name_its_owner(self) -> None:
        self.model(lambda e, _: e["blocks"]["web"].update(
            signals={"skip": "static site behind a CDN", "owner": "web-team"}))
        self.assertEqual(engine.evaluate(self.cfg()).schema_errors, [])

    def schema_errors(self, change) -> list[str]:
        self.model(change)
        return engine.evaluate(self.cfg()).schema_errors

    def test_a_list_where_a_name_belongs_is_a_model_error_not_a_bug(self) -> None:
        errors = self.schema_errors(lambda e, _: e["blocks"]["api"]["signals"].update(
            audience=["customer"]))
        self.assertTrue(any("audience" in err for err in errors), errors)
        self.assertFalse(any("crossweft bug" in err for err in errors), errors)

    def test_notify_takes_channel_names(self) -> None:
        def notify(names):
            meta = self.root / META
            data = json.loads(meta.read_text(encoding="utf-8"))
            data["meta"]["signals"]["notify"] = names
            meta.write_text(json.dumps(data), encoding="utf-8")
            return engine.evaluate(self.cfg()).schema_errors
        self.assertEqual(notify(["pager", "on-call"]), [])
        for bad in ([], ["Chat"], ["chat", "chat"], [["chat"]], [""]):
            self.assertTrue(any("notify" in e for e in notify(bad)), bad)

    def test_red_belongs_on_http_links_only(self) -> None:
        def red_on_sql(e, data):
            link = next(l for l in data["links"] if l["transport"] not in signals.HTTP_TRANSPORTS)
            link["signals"] = {"red": True}
        errors = self.schema_errors(red_on_sql)
        self.assertTrue(any("signals.red" in err and "HTTP links" in err for err in errors), errors)

    def test_a_health_route_without_a_route_scan_is_not_green(self) -> None:
        meta = self.root / META
        data = json.loads(meta.read_text(encoding="utf-8"))
        data["meta"].pop("route_scan")
        meta.write_text(json.dumps(data), encoding="utf-8")
        result = engine.evaluate(self.cfg())
        self.assertIn("signal:health-unserved:block:api:GET /healthz", {p.key for p in result.new})

    def test_an_unwritable_out_path_is_exit_2(self) -> None:
        (self.root / "blocker").write_text("x", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = signals.run_signals(self.cfg(), str(self.root / "blocker" / "s.json"))
        self.assertEqual(code, 2, out.getvalue())
        self.assertIn("cannot write", out.getvalue())


if __name__ == "__main__":
    unittest.main()
