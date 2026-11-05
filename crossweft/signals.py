"""Signals on the map: every link, service and flow carries an alarm.

The map already knows which service talks to which, over which route or queue.
`meta.signals` switches the rules on; then `check` fails when a current

  link     over HTTP has no request/error/latency signal (`red`),
  link     over a queue has no depth/age signal (`queue`, with its dead-letter queue),
  block    that runs (service, site) has neither a `health` check nor a `heartbeat`,
  flow     has no synthetic `probe` that walks the whole chain,

unless the entry says why not (`skip`). A heartbeat names the code that sends it
(anchors, checked like any anchor); a `GET /path` health check must be a route
the server registers; with `trace_header`, both ends of every HTTP link must
mention that header in their code.

`crossweft signals` turns the same model into a catalog of alarm rules for the
ops alert router (see docs/signals.md): thresholds default to the ops alert plan
and can be overridden per entry. The always-firing watchdog is part of the
catalog, so a silent monitor is itself an alarm.
"""

from __future__ import annotations

import re
import sys

SCHEMA = "crossweft.signals.v1"
HTTP_TRANSPORTS = {"https", "http-loopback", "webhook", "mtls"}
QUEUE_TRANSPORTS = {"queue"}
RUNNING_KINDS = {"service", "site"}
# a channel name the alert router knows ("chat", "email", "pager"); delivery is the router's job
CHANNEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
AUDIENCE = {"customer", "internal"}
DURATION_RE = re.compile(r"^([1-9][0-9]*)(s|m|h)$")
HEALTH_ROUTE_RE = re.compile(r"^(GET|HEAD) (/\S*)$")
UNIT = {"s": 1, "m": 60, "h": 3600}

# Defaults (the table in docs/signals.md, from our ops alert plan): availability
# opens after 3 failed checks a minute apart and closes after 2 successes; error
# rate warn 2 % / crit 10 % over 10 min with at least 10 errors; p95 latency warn
# 3 s / crit 10 s over 5 min; oldest queued message warn 10 min / crit 30 min; a
# heartbeat is missing after 3 intervals (warn) and 6 (crit); any dead letter is crit.
DEFAULTS = {
    "health": {"every": "1m", "timeout": "5s", "open_after": 3, "close_after": 2},
    "error_rate": [0.02, 0.10], "error_window": "10m", "min_errors": 10,
    "p95": ["3s", "10s"], "p95_window": "5m",
    "max_age": ["10m", "30m"],
    "heartbeat_missed": [3, 6],
    "watchdog_stale": 2,
}

BLOCK_KEYS = {"health", "url", "heartbeat", "audience", "owner", "product", "runbook", "skip"}
LINK_KEYS = {"red", "queue", "skip"}
FLOW_KEYS = {"probe", "skip"}
META_KEYS = {"notify", "watchdog", "trace_header", "product", "owner", "runbook"}


def seconds(text: str) -> int:
    match = DURATION_RE.match(text)
    return int(match.group(1)) * UNIT[match.group(2)]


def duration(total: int) -> str:
    for unit in ("h", "m"):
        if total % UNIT[unit] == 0:
            return f"{total // UNIT[unit]}{unit}"
    return f"{total}s"


# ------------------------------------------------------------------ schema
def _str(value: object, label: str) -> list[str]:
    return [] if isinstance(value, str) and value.strip() else [f"{label}: must be a non-empty string"]


def _dur(value: object, label: str) -> list[str]:
    if isinstance(value, str) and DURATION_RE.match(value):
        return []
    return [f"{label}: '{value}' must be a duration like 30s, 10m or 1h"]


def _pair(value: object, label: str, item) -> list[str]:
    """[warn, crit]: two values of one kind, crit not below warn."""
    if not isinstance(value, list) or len(value) != 2:
        return [f"{label}: must be [warn, crit]"]
    errors = [e for index, v in enumerate(value) for e in item(v, f"{label}[{index}]")]
    if not errors:
        warn, crit = (seconds(v) if isinstance(v, str) else v for v in value)
        if crit < warn:
            errors.append(f"{label}: crit {value[1]} is below warn {value[0]}")
    return errors


def _rate(value: object, label: str) -> list[str]:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 1
    return [] if ok else [f"{label}: must be a fraction in (0, 1]"]


def _count(value: object, label: str) -> list[str]:
    ok = isinstance(value, int) and not isinstance(value, bool) and value > 0
    return [] if ok else [f"{label}: must be a positive integer"]


def _object(value: object, label: str, allowed: set[str], required: tuple[str, ...] = ()
            ) -> list[str]:
    if not isinstance(value, dict):
        return [f"{label}: must be an object"]
    errors = [f"{label}: unknown key '{k}'" for k in sorted(set(value) - allowed)]
    errors += [f"{label}: missing '{k}'" for k in required if k not in value]
    return errors


def health_targets(sig: dict) -> list:
    health = sig.get("health")
    return health if isinstance(health, list) else [health] if health is not None else []


ROUTING_KEYS = {"audience", "owner", "product", "runbook"}   # who owns the entry, not a signal


def _skip_alone(value: dict, label: str) -> list[str]:
    signals_too = set(value) - {"skip"} - ROUTING_KEYS
    if "skip" in value and signals_too:
        return [f"{label}: 'skip' says the entry has no signal; it cannot also declare "
                f"{sorted(signals_too)}"]
    return _str(value["skip"], f"{label}.skip") if "skip" in value else []


def validate(kind: str, value: object, label: str, anchor_errors) -> list[str]:
    """Shape errors of one entity's `signals` (kind: blocks, links or flows)."""
    allowed = {"blocks": BLOCK_KEYS, "links": LINK_KEYS, "flows": FLOW_KEYS}[kind]
    errors = _object(value, label, allowed)
    if errors or not isinstance(value, dict):
        return errors
    errors += _skip_alone(value, label)
    if not value:
        errors.append(f"{label}: declare a signal or 'skip' with the reason")
    if kind == "blocks":
        if "health" in value:
            targets = health_targets(value)
            if not targets or not all(isinstance(t, str) and (HEALTH_ROUTE_RE.match(t)
                                      or t.startswith(("https://", "http://"))) for t in targets):
                errors.append(f"{label}.health: '{value['health']}' must be 'GET /path' (a route "
                              "of this block), an http(s) URL, or a list of them (one per "
                              "instance)")
        if "url" in value and not (isinstance(value["url"], str)
                                   and value["url"].startswith(("https://", "http://"))):
            errors.append(f"{label}.url: must be an http(s) URL")
        if "heartbeat" in value:
            beat = value["heartbeat"]
            errors += _object(beat, f"{label}.heartbeat", {"every", "anchors"}, ("every", "anchors"))
            if isinstance(beat, dict):
                if "every" in beat:
                    errors += _dur(beat["every"], f"{label}.heartbeat.every")
                if "anchors" in beat:
                    if not beat["anchors"]:
                        errors.append(f"{label}.heartbeat.anchors: name the code that sends "
                                      "the heartbeat")
                    errors += anchor_errors(beat["anchors"], f"{label}.heartbeat.anchors")
        if "audience" in value and not (isinstance(value["audience"], str)
                                        and value["audience"] in AUDIENCE):
            errors.append(f"{label}.audience: '{value['audience']}' not one of {sorted(AUDIENCE)}")
        for key in ("owner", "product", "runbook"):
            if key in value:
                errors += _str(value[key], f"{label}.{key}")
    elif kind == "links":
        if "red" in value and value["red"] is not True:
            red = value["red"]
            errors += _object(red, f"{label}.red", {"error_rate", "p95"})
            if isinstance(red, dict):
                if "error_rate" in red:
                    errors += _pair(red["error_rate"], f"{label}.red.error_rate", _rate)
                if "p95" in red:
                    errors += _pair(red["p95"], f"{label}.red.p95", _dur)
        if "queue" in value:
            queue = value["queue"]
            errors += _object(queue, f"{label}.queue", {"name", "dlq", "max_age", "depth"},
                              ("name", "dlq"))
            if isinstance(queue, dict):
                for key in ("name", "dlq"):
                    if key in queue:
                        errors += _str(queue[key], f"{label}.queue.{key}")
                if "max_age" in queue:
                    errors += _pair(queue["max_age"], f"{label}.queue.max_age", _dur)
                if "depth" in queue:
                    errors += _pair(queue["depth"], f"{label}.queue.depth", _count)
    else:
        if "probe" in value:
            probe = value["probe"]
            keys = ("every", "creates", "expects", "cleanup")
            errors += _object(probe, f"{label}.probe", set(keys), keys)
            if isinstance(probe, dict):
                if "every" in probe:
                    errors += _dur(probe["every"], f"{label}.probe.every")
                for key in keys[1:]:
                    if key in probe:
                        errors += _str(probe[key], f"{label}.probe.{key}")
    return errors


def validate_meta(value: object) -> list[str]:
    label = "meta.signals"
    errors = _object(value, label, META_KEYS, ("notify", "watchdog"))
    if not isinstance(value, dict):
        return errors
    notify = value.get("notify")
    if "notify" in value and (not isinstance(notify, list) or not notify
                              or any(not isinstance(n, str) or not CHANNEL_RE.match(n) for n in notify)
                              or len(set(map(str, notify))) != len(notify)):
        errors.append(f"{label}.notify: a non-empty list of distinct channel names the alert "
                      "router knows, lowercase like 'chat' or 'email'")
    if "watchdog" in value:
        watchdog = value["watchdog"]
        errors += _object(watchdog, f"{label}.watchdog", {"every"}, ("every",))
        if isinstance(watchdog, dict) and "every" in watchdog:
            errors += _dur(watchdog["every"], f"{label}.watchdog.every")
    for key in ("trace_header", "product", "owner", "runbook"):
        if key in value:
            errors += _str(value[key], f"{label}.{key}")
    return errors


def structure_errors(model) -> list[str]:
    """Signals on an entity need the policy in meta.signals: who gets the alarm;
    `red` belongs on an HTTP link and `queue` on a queue link."""
    if "signals" in model.meta:
        errors = []
        for link in model.entities["links"]:
            sig = link.get("signals")
            if not isinstance(sig, dict) or not isinstance(link.get("id"), str):
                continue
            transport = link.get("transport")
            if "red" in sig and transport not in HTTP_TRANSPORTS:
                errors.append(f"links:{link['id']}.signals.red: requests/errors/latency are for "
                              f"HTTP links {sorted(HTTP_TRANSPORTS)}, this one is '{transport}'")
            if "queue" in sig and transport not in QUEUE_TRANSPORTS:
                errors.append(f"links:{link['id']}.signals.queue: only a 'queue' link has a "
                              f"queue, this one is '{transport}'")
        return errors
    users = [f"{kind[:-1]}:{item['id']}" for kind in ("blocks", "links", "flows")
             for item in model.entities[kind] if "signals" in item and isinstance(item.get("id"), str)]
    if not users:
        return []
    return [f"{', '.join(users[:5])}{' ...' if len(users) > 5 else ''}: signals need "
            "meta.signals (notify, watchdog) -- who gets the alarm and what watches the monitor"]


# ------------------------------------------------------------------- rules
def missing(model) -> list[tuple[str, str, str]]:
    """(problem key, ref, message) for every current entry without a signal it needs.
    Model only: `crossweft signals` and `check` give the same list."""
    if "signals" not in model.meta:
        return []
    found = []
    for link in model.entities["links"]:
        if link["status"] != "current":
            continue
        sig = link.get("signals") or {}
        if "skip" in sig:
            continue
        ref = f"link:{link['id']}"
        if link["transport"] in HTTP_TRANSPORTS and "red" not in sig:
            found.append((f"signal:missing:{ref}", ref,
                          f"{ref} ({link['transport']}) has no signal -- add signals.red "
                          "(requests, errors, latency) or signals.skip with the reason"))
        elif link["transport"] in QUEUE_TRANSPORTS and "queue" not in sig:
            found.append((f"signal:missing:{ref}", ref,
                          f"{ref} is a queue without a signal -- add signals.queue (name, dlq; "
                          "depth, age of the oldest message, rate) or signals.skip with the reason"))
    for block in model.entities["blocks"]:
        if block["status"] != "current" or block["kind"] not in RUNNING_KINDS:
            continue
        sig = block.get("signals") or {}
        if not ({"health", "heartbeat", "skip"} & set(sig)):
            ref = f"block:{block['id']}"
            found.append((f"signal:missing:{ref}", ref,
                          f"{ref} ({block['kind']}) runs without a signal -- add signals.health "
                          "(GET /path or URL), signals.heartbeat for a background worker, or "
                          "signals.skip with the reason"))
    for flow in model.entities["flows"]:
        if flow["status"] != "current":
            continue
        sig = flow.get("signals") or {}
        if not ({"probe", "skip"} & set(sig)):
            ref = f"flow:{flow['id']}"
            found.append((f"signal:missing:{ref}", ref,
                          f"{ref} has no synthetic probe -- add signals.probe (every, creates, "
                          "expects, cleanup) that walks the chain from outside, or signals.skip"))
    return found


def _block_files(checker, block_id: str) -> list[str]:
    code = checker._code_block(block_id)
    if code is None:
        return []
    patterns = [p.rstrip("/") + "/**/*" if (checker.root / p).is_dir() else p
                for p in checker.model.blocks[code].get("code", [])]
    return checker.expand(patterns)


def check(checker) -> None:
    model = checker.model
    policy = model.meta.get("signals")
    if policy is None:
        return
    for key, _ref, message in missing(model):
        checker.items_checked += 1
        checker.add(key, "signals", message)
    registered = checker.route_facts.get("registered")
    norm = checker._norm_route_path
    for block in model.entities["blocks"]:
        if block["status"] != "current" or not block.get("route_server"):
            continue
        for health in health_targets(block.get("signals") or {}):
            match = HEALTH_ROUTE_RE.match(health)
            if not match:
                continue
            key = f"signal:health-unserved:block:{block['id']}:{health}"
            if registered is None:
                if checker.scope is None:
                    checker.items_checked += 1
                    checker.add(key, "signals", f"block:{block['id']} signals.health is {health}, "
                                "but the map has no meta.route_scan, so nothing can tell whether "
                                "the server serves it -- add meta.route_scan or give the health "
                                "check as a URL")
                else:
                    checker.skipped_keys.add(key)
                continue
            checker.items_checked += 1
            method, path = match.groups()
            methods = {method, "ANY"} | ({"GET"} if method == "HEAD" else set())
            if not any(m in methods and norm(p) == norm(path) for m, p, _r, _l in registered):
                checker.add(key, "signals", f"block:{block['id']} signals.health is {health}, but "
                            "the server registers no such route -- the health check would always "
                            "fail")
    header = policy.get("trace_header")
    if not header:
        return
    needle = header.lower()
    for link in model.entities["links"]:
        if link["status"] != "current" or link["transport"] not in HTTP_TRANSPORTS:
            continue
        for side in ("from", "to"):
            files = _block_files(checker, link[side])
            key = f"signal:trace:link:{link['id']}:{side}"
            if not files or not checker.guard(files, keys=(key,)):
                continue
            checker.items_checked += 1
            if not any(needle in (checker.text(rel) or "").lower() for rel in files):
                checker.add(key, "signals",
                            f"link:{link['id']}: the {side} end ({link[side]}) never mentions the "
                            f"trace header {header} -- one request id must pass through HTTP "
                            "headers, message metadata and logs")


# ----------------------------------------------------------------- catalog
def _severity(block: dict) -> str:
    return "crit" if (block.get("signals") or {}).get("audience") == "customer" else "warn"


def catalog(model) -> dict:
    """The alarm rules the map implies, for the ops alert router."""
    policy = model.meta["signals"]
    product = policy.get("product") or model.meta.get("title")
    rules: list[dict] = []
    skipped: list[dict] = []

    def common(block: dict) -> dict:
        sig = block.get("signals") or {}
        entry = {"service": block["id"], "product": sig.get("product", product),
                 "owner": sig.get("owner") or block.get("owner") or policy.get("owner"),
                 "severity": _severity(block)}
        runbook = sig.get("runbook") or policy.get("runbook")
        if runbook:
            entry["runbook"] = runbook
        return {k: v for k, v in entry.items() if v is not None}

    for block in model.entities["blocks"]:
        sig = block.get("signals")
        if block["status"] != "current" or not sig:
            continue
        ref = f"block:{block['id']}"
        if "skip" in sig:
            skipped.append({"ref": ref, "reason": sig["skip"]})
            continue
        targets = health_targets(sig)
        for index, health in enumerate(targets):
            url = health if health.startswith("http") else (
                sig["url"].rstrip("/") + health.split(" ", 1)[1] if "url" in sig else None)
            suffix = f":{index + 1}" if len(targets) > 1 else ""
            rules.append({"id": f"health:{block['id']}{suffix}", "ref": ref, "kind": "health",
                          "check": health, "url": url, "expect": [200],
                          **DEFAULTS["health"], **common(block)})
        if "heartbeat" in sig:
            every = seconds(sig["heartbeat"]["every"])
            warn, crit = DEFAULTS["heartbeat_missed"]
            rules.append({"id": f"heartbeat:{block['id']}", "ref": ref, "kind": "heartbeat",
                          "key": f"heartbeat:{block['id']}", "every": duration(every),
                          "warn_after": duration(every * warn), "crit_after": duration(every * crit),
                          "sent_by": [f"{a['path']}" for a in sig["heartbeat"]["anchors"]],
                          **common(block)})
    for link in model.entities["links"]:
        sig = link.get("signals")
        if link["status"] != "current" or not sig:
            continue
        ref = f"link:{link['id']}"
        server = model.blocks[link["to"]]
        if "skip" in sig:
            skipped.append({"ref": ref, "reason": sig["skip"]})
            continue
        if "red" in sig:
            red = sig["red"] if isinstance(sig["red"], dict) else {}
            rate = red.get("error_rate", DEFAULTS["error_rate"])
            p95 = red.get("p95", DEFAULTS["p95"])
            rules.append({"id": f"red:{link['id']}", "ref": ref, "kind": "red",
                          "client": link["from"], "transport": link["transport"],
                          "routes": (link.get("identifiers") or {}).get("route", []),
                          "error_rate": {"warn": rate[0], "crit": rate[1],
                                         "window": DEFAULTS["error_window"],
                                         "min_errors": DEFAULTS["min_errors"]},
                          "p95": {"warn": p95[0], "crit": p95[1], "window": DEFAULTS["p95_window"]},
                          **common(server)})
        if "queue" in sig:
            queue = sig["queue"]
            age = queue.get("max_age", DEFAULTS["max_age"])
            entry = {"id": f"queue:{link['id']}", "ref": ref, "kind": "queue",
                     "queue": queue["name"], "producer": link["from"], "consumer": link["to"],
                     "max_age": {"warn": age[0], "crit": age[1]}, "rate": True, **common(server)}
            if "depth" in queue:
                entry["depth"] = {"warn": queue["depth"][0], "crit": queue["depth"][1]}
            rules.append(entry)
            rules.append({"id": f"dlq:{link['id']}", "ref": ref, "kind": "dlq",
                          "queue": queue["dlq"], "threshold": 1, **common(server),
                          "severity": "crit"})
    for flow in model.entities["flows"]:
        sig = flow.get("signals")
        if flow["status"] != "current" or not sig:
            continue
        ref = f"flow:{flow['id']}"
        if "skip" in sig:
            skipped.append({"ref": ref, "reason": sig["skip"]})
            continue
        probe = sig["probe"]
        rules.append({"id": f"probe:{flow['id']}", "ref": ref, "kind": "probe",
                      "name": flow["name"], **probe,
                      "steps": [{"link": s["link"], "action": s.get("action", "")}
                                for s in flow["steps"]],
                      "product": product, "severity": "crit"})
    every = seconds(policy["watchdog"]["every"])
    rules.append({"id": "watchdog", "kind": "watchdog", "fires": "always",
                  "every": duration(every),
                  "stale_after": duration(every * DEFAULTS["watchdog_stale"]),
                  "product": product, "severity": "crit"})
    return {"schema": SCHEMA, "product": product, "notify": policy["notify"],
            "trace_header": policy.get("trace_header"), "rules": rules, "skipped": skipped,
            "missing": [{"key": k, "ref": r, "message": m} for k, r, m in missing(model)]}


def run_signals(cfg, out_path: str | None) -> int:
    """Print or write the catalog: 0 complete, 1 an entry lacks a signal it
    needs (listed under "missing"), 2 the model does not load or has no
    meta.signals."""
    import json
    from pathlib import Path

    from . import engine
    try:
        model = engine.load_model(cfg)
    except engine.ModelError as exc:
        engine.out(f"[ERR] {exc}")
        return 2
    errors = engine.validate_schema(model)
    if errors:
        for error in errors[:20]:
            engine.out(f"[ERR] {error}")
        return 2
    if "signals" not in model.meta:
        engine.out("[ERR] the map has no meta.signals: nothing says who gets the alarm "
                   "(see docs/signals.md)")
        return 2
    data = catalog(model)
    text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
    if out_path:
        target = Path(out_path).resolve()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(text.encode("utf-8"))
        except OSError as exc:
            engine.out(f"[ERR] signals: cannot write {target}: {exc}")
            return 2
        engine.out(f"signals: {target} ({len(data['rules'])} rules, {len(data['skipped'])} "
                   f"skipped, {len(data['missing'])} missing)")
    else:
        print(text, end="")
    for entry in data["missing"]:
        # stdout carries the JSON when no --out is given: the verdict goes to stderr
        print(f"[FAIL] {entry['key']}: {entry['message']}", file=sys.stderr)
    return 1 if data["missing"] else 0
