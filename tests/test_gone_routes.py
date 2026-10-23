"""`gone` on a legacy link: a retired route the server still answers on purpose.

Run: python -m unittest tests.test_gone_routes -v   (stdlib only)
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from crossweft import engine  # noqa: E402

SERVER = """package main

import "github.com/go-chi/chi/v5"

func routes() {
	r := chi.NewRouter()
	r.Get("/v1/orders", listOrders)
	r.Post("/v1/old-checkout", retiredCheckout) // answers 410 checkout_retired
}
"""
CLIENT = 'export const orders = "/v1/orders";\n'

ANCHOR_WEB = [{"path": "web/api.ts", "find": "export"}]
MODEL = {
    "meta": {"schema": engine.SCHEMA_ID, "title": "t", "lanes": [{"id": "app", "name": "App"}],
             "route_scan": {"routers": [{"path": "server/main.go", "scanner": "go-chi"}],
                            "client_globs": ["web/**/*.ts"],
                            "client_literal_regexes": ["\"(/v1/[^\"]*)\""]}},
    "blocks": [
        {"id": "web", "name": "Web", "kind": "site", "lane": "app", "status": "current",
         "summary": "s", "code": ["web/"]},
        {"id": "api", "name": "API", "kind": "service", "lane": "app", "status": "current",
         "summary": "s", "code": ["server/"], "route_server": True}],
    "links": [
        {"id": "web-api", "from": "web", "to": "api", "transport": "https", "status": "current",
         "summary": "s", "from_anchors": ANCHOR_WEB,
         "contract": {"name": "API", "enforcement": "shared-code", "defined_in": ANCHOR_WEB},
         "to_anchors": [{"path": "server/main.go", "find": "r.Get(\"/v1/orders\""}],
         "identifiers": {"route": ["GET /v1/orders"]}},
        {"id": "old-checkout", "from": "web", "to": "api", "transport": "https",
         "status": "legacy", "summary": "retired; the server answers 410",
         "to_anchors": [{"path": "server/main.go", "find": "retiredCheckout"}],
         "identifiers": {"route": ["POST /v1/old-checkout"]},
         "gone": {"status": 410, "replaced_by": "web-api"}}],
}


class GoneRoutes(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="crossweft-gone-")
        self.root = Path(self._tmp.name)
        (self.root / "seams" / "model").mkdir(parents=True)
        (self.root / "crossweft.json").write_text("{}", encoding="utf-8")
        self.write("server/main.go", SERVER)
        self.write("web/api.ts", CLIENT)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def run_model(self, model: dict) -> engine.CheckResult:
        self.write("seams/model/00.json", json.dumps(model))
        return engine.evaluate(engine.load_config(self.root))

    def keys(self, result: engine.CheckResult) -> list[str]:
        self.assertIsNone(result.load_error)
        self.assertEqual(result.schema_errors, [])
        return [p.key for p in result.new]

    def test_a_gone_route_is_mapped(self) -> None:
        self.assertEqual(self.keys(self.run_model(MODEL)), [])
        # negative control: without `gone` the same tombstone is an unmapped route
        plain = copy.deepcopy(MODEL)
        del plain["links"][1]["gone"]
        self.assertIn("route:unmapped:POST /v1/old-checkout", self.keys(self.run_model(plain)))

    def test_a_removed_tombstone_is_reported(self) -> None:
        self.write("server/main.go", SERVER.replace(
            '\tr.Post("/v1/old-checkout", retiredCheckout) // answers 410 checkout_retired\n',
            "\t// retiredCheckout removed\n"))
        self.assertIn("route:gone-unserved:POST /v1/old-checkout",
                      self.keys(self.run_model(MODEL)))

    def test_a_client_calling_a_gone_route_is_reported(self) -> None:
        self.write("web/api.ts", CLIENT + 'export const pay = "/v1/old-checkout";\n')
        keys = self.keys(self.run_model(MODEL))
        self.assertIn("route:gone-called:/v1/old-checkout", keys)
        self.assertNotIn("route:client-unexplained:/v1/old-checkout", keys)

    def test_schema_rules(self) -> None:
        cases = {
            "only a legacy link can be gone": lambda m: m["links"][1].update(status="planned"),
            "needs identifiers.route": lambda m: m["links"][1].pop("identifiers"),
            "needs to_anchors": lambda m: m["links"][1].pop("to_anchors"),
            "status: the HTTP status": lambda m: m["links"][1]["gone"].update(status=200),
            "unknown link": lambda m: m["links"][1]["gone"].update(replaced_by="nope"),
            "unknown key": lambda m: m["links"][1]["gone"].update(when="2026"),
            "no glob, no ANY": lambda m: m["links"][1]["identifiers"].update(route=["POST /v1/*"]),
            "no ANY, no MOUNT": lambda m: m["links"][1]["identifiers"].update(route=["ANY /v1/old-checkout"]),
        }
        for expected, mutate in cases.items():
            model = copy.deepcopy(MODEL)
            mutate(model)
            result = self.run_model(model)
            self.assertTrue(any(expected in e for e in result.schema_errors),
                            f"{expected!r} not in {result.schema_errors}")

    def test_replaced_by_must_be_current(self) -> None:
        model = copy.deepcopy(MODEL)
        model["links"][0]["status"] = "planned"
        result = self.run_model(model)
        self.assertTrue(any("not current" in e for e in result.schema_errors), result.schema_errors)


if __name__ == "__main__":
    unittest.main()
