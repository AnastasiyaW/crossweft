"""`crossweft report`: the map with what the check found on it.

`render` draws the model alone, and `render --check` keeps those files equal to
the model, so they cannot carry code-state. The report is the same viewer with
one more payload key, written on demand and never compared:

  problems  every problem of a full check (failing and excused by a finding),
            each placed on the links and blocks it concerns
  routes    one row per route from the three sources the route checks read --
            declared on the map, registered by the server, called by the client --
            with the check's verdict for the row
  actions   every ui_actions control and the hops it passes, as the check traced it

Every verdict is the check's own problem list: the report adds no judgement, so
it cannot disagree with `crossweft check`.
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path

from . import engine

LINK_REF = re.compile(r"\blink:([A-Za-z0-9_.\-]+)")
# signal problems name the block or flow itself (signal:missing:block:<id>)
OWNER_REF = re.compile(r"^signal:[a-z-]+:(block|flow):([A-Za-z0-9_.\-]+)")
GUARD_KINDS = {"join": "joins", "set": "sets", "pair": "pairs", "ui": "ui_actions"}
# route problem kinds keyed by "METHOD path" (the rest key by the client literal)
ROUTE_BY_METHOD = ("unmapped", "unserved", "gone-unserved")
ROUTE_BY_LITERAL = ("client-unexplained", "gone-called")


def _covering_block(model, rel: str) -> str | None:
    """The block with the most specific `code` entry that holds `rel`."""
    best, depth = None, -1
    for block in model.entities["blocks"]:
        for code in block.get("code", []):
            code = code.rstrip("/")
            if (rel == code or rel.startswith(code + "/")) and len(code) > depth:
                best, depth = block["id"], len(code)
    return best


def _guard_owner(key: str) -> tuple[str, str] | None:
    """(entity kind, id) of the join/set/pair/ui_actions entry a problem key names."""
    parts = key.split(":")
    if parts[0] == "anchor":
        parts = parts[1:]
    if not parts or parts[0] not in GUARD_KINDS or len(parts) < 2:
        return None
    if parts[0] == "pair" and parts[1] in ("unattested", "changed", "stale-lock", "lock"):
        return ("pairs", parts[2]) if len(parts) > 2 else None
    return GUARD_KINDS[parts[0]], parts[1]


def problem_refs(model, problem) -> list[str]:
    """Map refs ("link:<id>", "block:<id>") a problem belongs to, most specific first."""
    refs: list[str] = []
    for ident in LINK_REF.findall(f"{problem.key} {problem.message}"):
        if ident in model.links:
            refs.append(f"link:{ident}")
    owner = _guard_owner(problem.key)
    if owner:
        entry = next((e for e in model.entities[owner[0]] if e.get("id") == owner[1]), None)
        if entry and entry.get("link") in model.links:
            refs.append(f"link:{entry['link']}")
    owner_ref = OWNER_REF.match(problem.key)
    if owner_ref:
        refs.append(f"{owner_ref.group(1)}:{owner_ref.group(2)}")
    if problem.key.startswith("route:unmapped:"):
        refs += [f"block:{b['id']}" for b in model.entities["blocks"] if b.get("route_server")]
    if not refs and problem.path:
        block = _covering_block(model, problem.path)
        if block:
            refs.append(f"block:{block}")
    return list(dict.fromkeys(refs))


def _route_rows(checker, problems: list) -> list[dict]:
    norm = engine.Checker._norm_route_path
    rows: dict[tuple[str, str], dict] = {}

    def row(method: str, path: str) -> dict:
        key = (method, norm(path))
        if key not in rows:
            rows[key] = {"method": method, "path": path, "links": [], "served": [], "called": [],
                         "literals": [], "status": "ok", "problems": []}
        return rows[key]

    for method, path, link in checker.declared_routes():
        row(method, path)["links"].append({"id": link["id"], "status": link["status"],
                                           "gone": bool(link.get("gone"))})
    facts = checker.route_facts
    for method, path, rel, line in facts.get("registered", []):
        # as the check reads it: ANY serves every method on the path, HEAD rides on GET
        same = [r for (m, p), r in rows.items() if p == norm(path)
                and (m == method or method == "ANY" or (method == "HEAD" and m == "GET"))]
        for target in same or [row(method, path)]:
            target["served"].append(f"{rel}:{line}")
    for literal, places in sorted(facts.get("client", {}).items()):
        targets = [r for (_, path), r in rows.items() if path == norm(literal)] or [row("", literal)]
        for target in targets:
            target["literals"].append(literal)
            target["called"] += [f"{rel}:{line}" for rel, line in places]
    for problem in problems:
        parts = problem["key"].split(":", 2)
        if parts[0] != "route" or len(parts) < 3:
            continue
        kind, rest = parts[1], parts[2]
        if kind in ROUTE_BY_METHOD and " " in rest:
            method, path = rest.split(" ", 1)
            hit = [rows.get((method, norm(path)))]
        elif kind in ROUTE_BY_LITERAL:
            hit = [r for r in rows.values() if rest in r["literals"]]
        else:
            continue
        for target in filter(None, hit):
            target["problems"].append(problem["key"])
            if target["status"] == "ok":
                target["status"] = kind
    for target in rows.values():
        if target["status"] == "ok" and target["links"] and all(
                link["status"] != "current" for link in target["links"]):
            target["status"] = "gone" if any(link["gone"] for link in target["links"]) else "not_current"
        target["served"] = sorted(set(target["served"]))
        target["called"] = sorted(set(target["called"]))
        del target["literals"]
    return sorted(rows.values(), key=lambda r: (norm(r["path"]), r["method"]))


def payload(result: engine.CheckResult) -> dict:
    checker = result.checker
    model = checker.model
    problems = []
    for known, group in ((False, result.new), (True, result.known)):
        for problem in group:
            row = problem.as_dict()
            row["known"] = known
            row["refs"] = problem_refs(model, problem)
            problems.append(row)
    scanned = bool(model.meta.get("route_scan"))
    actions = []
    for entry in model.entities["ui_actions"]:
        traces = checker.ui_traces.get(entry["id"])
        actions.append({"id": entry["id"], "name": entry["name"], "traced": traces is not None,
                        "controls": [engine._trace_row(t) for t in traces or []]})
    return {
        "generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "ok": result.ok, "new": len(result.new), "known": len(result.known),
        "stale": list(result.stale),
        "problems": problems,
        "routes": {"scanned": scanned, "rows": _route_rows(checker, problems) if scanned else []},
        "actions": actions,
    }


def run_report(cfg: engine.Config, out: str | None, as_json: bool) -> int:
    """Write the report (exit 0 when the check passes, 1 when it fails, 2 when it
    could not run -- the same verdict as `crossweft check`)."""
    result = engine.evaluate(cfg)
    if result.load_error or result.schema_errors or result.checker is None:
        engine.out(f"[ERR] {result.load_error or 'model schema: run `crossweft check` first'}")
        return 2
    data = payload(result)
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
        return 0 if result.ok else 1
    target = Path(out).resolve() if out else cfg.abs(cfg.output_dir) / "report.html"
    template = cfg.template_path.read_text(encoding="utf-8")
    page = engine.Renderer(result.checker.model).viewer(template, report=data)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(page.encode("utf-8"))
    except OSError as exc:
        engine.out(f"[ERR] report: cannot write {target}: {exc}")
        return 2
    broken = sum(1 for p in data["problems"] if not p["known"])
    engine.out(f"report: {target} ({broken} failing, {data['known']} known; "
               f"{len(data['routes']['rows'])} routes, "
               f"{sum(len(a['controls']) for a in data['actions'])} controls; "
               "a build artifact, do not commit it)")
    return 0 if result.ok else 1
