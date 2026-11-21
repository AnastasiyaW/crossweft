"""Turn a `crossweft.signals.v1` catalog into rules a log store can evaluate.

`crossweft signals --format loki` writes Loki ruler rule groups and `--format grafana`
writes Grafana alerting provisioning. Both read the same logs, so only the kinds a
log query can answer become rules:

  heartbeat  the beat line is absent for warn_after / crit_after
  red        error rate (status >= 500) over the window, with the minimum error
             count, and p95 latency when the logs carry a duration field
  watchdog   fires always; the router raises an alarm when it stops arriving

`health`, `probe`, `queue` and `dlq` need an HTTP call or a queue broker, not a log
query: they are listed on stderr and stay with the alert router.

The output is JSON, which is valid YAML, so both files load as they are and the
tool keeps no YAML dependency.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from .signals import seconds

LOG_KINDS = {"heartbeat", "red", "watchdog"}
ROUTER_KINDS = {"health": "an HTTP check", "probe": "a synthetic run",
                "queue": "a queue broker", "dlq": "a queue broker"}
LATENCY_SCALE = {"ns": 1e9, "us": 1e6, "ms": 1e3, "s": 1.0}
FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ALERT_NAME = {"heartbeat": "CrossweftHeartbeatMissing", "red_errors": "CrossweftErrorRate",
              "red_p95": "CrossweftLatencyP95", "watchdog": "CrossweftWatchdog"}


class ExportError(ValueError):
    """Options that cannot produce a correct rule file."""


@dataclass
class LogOptions:
    selectors: dict[str, str] = field(default_factory=dict)   # service -> LogQL stream selector
    selector_default: str | None = None                       # template with {service}
    parser: str = "json"                                      # json | logfmt
    status_field: str = "status"
    route_field: str = "route"
    latency_field: str | None = None                          # p95 rules only when given
    latency_unit: str = "ms"
    datasource_uid: str | None = None                         # grafana only
    folder: str = "Crossweft"
    interval: str = "1m"

    def validate(self, fmt: str) -> None:
        if self.parser not in ("json", "logfmt"):
            raise ExportError(f"--parser must be json or logfmt, not '{self.parser}'")
        for name in ("status_field", "route_field", "latency_field"):
            value = getattr(self, name)
            if value is not None and not FIELD_RE.match(value):
                raise ExportError(f"--{name.replace('_', '-')} '{value}' is not a field name")
        if self.latency_unit not in LATENCY_SCALE:
            raise ExportError(f"--latency-unit must be one of {sorted(LATENCY_SCALE)}")
        for service, sel in self.selectors.items():
            if not (sel.startswith("{") and sel.endswith("}")):
                raise ExportError(f"--selector {service}: '{sel}' is not a LogQL stream selector")
        if self.selector_default is not None and "{service}" not in self.selector_default:
            raise ExportError("--selector-default needs the {service} placeholder")
        if fmt == "grafana" and not self.datasource_uid:
            raise ExportError("--format grafana needs --datasource-uid (the Loki data source)")

    def selector(self, service: str) -> str | None:
        if service in self.selectors:
            return self.selectors[service]
        if self.selector_default:
            return self.selector_default.replace("{service}", service)
        return None


def _quote(text: str) -> str:
    return json.dumps(text)


def _route_filter(routes: list[str], opts: LogOptions) -> str:
    """Routes are 'METHOD /path' in the map; the log's route field holds the path pattern."""
    paths = sorted({r.split(" ", 1)[1] if " " in r else r for r in routes})
    if not paths:
        return ""
    alternation = "|".join(re.escape(p) for p in paths)
    # LogQL strings are Go strings: the regex backslashes must be doubled inside the quotes
    return f' | {opts.route_field}=~{_quote("^(" + alternation + ")$")}'


def _base(selector: str, routes: list[str], opts: LogOptions) -> str:
    return f'{selector} | {opts.parser} | __error__=""{_route_filter(routes, opts)}'


def _red_rules(rule: dict, selector: str, opts: LogOptions) -> list[tuple[str, str, str, str]]:
    """(alert key, severity, expression, human summary) for one red rule."""
    base = _base(selector, rule.get("routes", []), opts)
    rate = rule["error_rate"]
    window = rate["window"]
    errors = f"sum(count_over_time({base} | {opts.status_field} >= 500 [{window}]))"
    total = f"sum(count_over_time({base} | {opts.status_field} > 0 [{window}]))"
    # warn stops where crit starts, so one breach is one alert, not two
    ratio = f"{errors} / {total}"
    enough = f"({errors} >= {rate['min_errors']})"
    out = [("red_errors", "warn",
            f"({ratio} > {rate['warn']}) and ({ratio} <= {rate['crit']}) and {enough}",
            f"{rule['ref']}: error rate above {rate['warn']:.0%} over {window}"),
           ("red_errors", "crit", f"({ratio} > {rate['crit']}) and {enough}",
            f"{rule['ref']}: error rate above {rate['crit']:.0%} over {window}")]
    if opts.latency_field:
        p95 = rule["p95"]
        scale = LATENCY_SCALE[opts.latency_unit]
        q = (f"max(quantile_over_time(0.95, {base} | unwrap {opts.latency_field} "
             f"[{p95['window']}]))")
        warn, crit = (seconds(p95[s]) * scale for s in ("warn", "crit"))
        out += [("red_p95", "warn", f"({q} > {warn:g}) and ({q} <= {crit:g})",
                 f"{rule['ref']}: p95 latency above {p95['warn']} over {p95['window']}"),
                ("red_p95", "crit", f"{q} > {crit:g}",
                 f"{rule['ref']}: p95 latency above {p95['crit']} over {p95['window']}")]
    return out


def _heartbeat_rules(rule: dict, selector: str) -> list[tuple[str, str, str, str]]:
    beat = f"{selector} |= {_quote(rule['key'])}"
    warn = f"absent_over_time({beat} [{rule['warn_after']}])"
    crit = f"absent_over_time({beat} [{rule['crit_after']}])"
    return [("heartbeat", "warn", f"{warn} unless {crit}",
             f"{rule['ref']}: no {rule['key']} for {rule['warn_after']}"),
            ("heartbeat", "crit", crit,
             f"{rule['ref']}: no {rule['key']} for {rule['crit_after']}")]


def expand(catalog: dict, opts: LogOptions, via: str) -> tuple[list[dict], list[str]]:
    """Flat list of alert definitions plus the reasons for every rule left to the router."""
    alerts: list[dict] = []
    left: list[str] = []
    for rule in catalog["rules"]:
        kind = rule["kind"]
        if kind in ROUTER_KINDS:
            left.append(f"{rule['id']}: needs {ROUTER_KINDS[kind]}, not a log query -- the "
                        "alert router watches it")
            continue
        if kind not in LOG_KINDS:
            left.append(f"{rule['id']}: unknown kind '{kind}'")
            continue
        if kind == "watchdog":
            parts = [("watchdog", "crit", "vector(1)",
                      f"watchdog of {catalog['product']}: fires always, its absence is the alarm")]
        else:
            selector = opts.selector(rule["service"])
            if selector is None:
                left.append(f"{rule['id']}: no log selector for service '{rule['service']}' "
                            f"(add --selector {rule['service']}=<LogQL selector>)")
                continue
            parts = (_heartbeat_rules(rule, selector) if kind == "heartbeat"
                     else _red_rules(rule, selector, opts))
        for key, severity, expr, summary in parts:
            labels = {"severity": severity, "product": rule.get("product") or catalog["product"],
                      "crossweft_rule": rule["id"], "crossweft_kind": kind,
                      "crossweft_via": via}
            for name in ("service", "owner"):
                if rule.get(name):
                    labels[name] = rule[name]
            annotations = {"summary": summary}
            if rule.get("ref"):
                annotations["crossweft_ref"] = rule["ref"]
            if rule.get("runbook"):
                annotations["runbook_url"] = rule["runbook"]
            if kind == "watchdog":
                annotations["stale_after"] = rule["stale_after"]
            alerts.append({"name": ALERT_NAME[key], "key": f"{rule['id']}:{key}:{severity}",
                           "expr": expr, "labels": labels, "annotations": annotations})
    return alerts, left


def to_loki(catalog: dict, opts: LogOptions) -> tuple[dict, list[str]]:
    alerts, left = expand(catalog, opts, "loki")
    rules = [{"alert": a["name"], "expr": a["expr"], "for": "0m", "labels": a["labels"],
              "annotations": a["annotations"]} for a in alerts]
    group = {"name": f"crossweft-{catalog['product']}", "interval": opts.interval, "rules": rules}
    return {"groups": [group]}, left


def _uid(key: str) -> str:
    """Grafana rule uids are at most 40 characters; the key is longer, so it is hashed."""
    return "cw-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def _window(expr: str) -> int:
    """The widest range in the query, so Grafana fetches enough history."""
    ranges = [seconds(m) for m in re.findall(r"\[(\d+[smh])\]", expr)]
    return max(ranges, default=60)


def to_grafana(catalog: dict, opts: LogOptions) -> tuple[dict, list[str]]:
    alerts, left = expand(catalog, opts, "grafana")
    rules = []
    for a in alerts:
        query = {"refId": "A", "expr": a["expr"], "queryType": "instant", "editorMode": "code"}
        threshold = {"refId": "C", "type": "threshold", "expression": "A",
                     "conditions": [{"evaluator": {"type": "gt", "params": [0]}}]}
        rules.append({
            "uid": _uid(a["key"]), "title": f"{a['name']} {a['labels']['crossweft_rule']} "
                                            f"{a['labels']['severity']}",
            "condition": "C",
            "data": [{"refId": "A", "relativeTimeRange": {"from": _window(a["expr"]), "to": 0},
                      "datasourceUid": opts.datasource_uid, "model": query},
                     {"refId": "C", "datasourceUid": "__expr__", "model": threshold}],
            # no result means the condition is false (the query keeps only breaches).
            # A failing watchdog query must go quiet, not raise DatasourceError: that
            # alert copies the rule's labels and would pass for a live watchdog.
            "noDataState": "OK",
            "execErrState": "OK" if a["labels"]["crossweft_kind"] == "watchdog" else "Error",
            "for": "0s",
            "labels": a["labels"], "annotations": a["annotations"]})
    group = {"orgId": 1, "name": f"crossweft-{catalog['product']}", "folder": opts.folder,
             "interval": opts.interval, "rules": rules}
    return {"apiVersion": 1, "groups": [group]}, left
