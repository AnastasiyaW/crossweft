"""UI actions: every control must reach the code that handles it.

A control (a button, a menu command, a keyboard callback) names an action by a
string written by hand: `data-action="place-order"`, `callback_data="export"`.
The code that runs it is found through hand-written tables: a dispatch map, a
registry, a handler's body that calls a route. Each table is one hop of the
chain, and each hop is a place where the control can lose its handler.

The engine extracts every side from the files (regex, like a set side) and
hands the values here; resolution itself is pure:

  controls      exact values ("place-order") and dynamic prefixes ("export:"
                from `export:${format}`); an empty prefix is a fully computed
                value, which no hop can prove handled
  hop           a map key -> next value (2 regex groups) or a set of keys
                (1 group), plus prefix rules (`startswith("export:")`) that
                handle every value they cover and end the chain there
  routes        optionally, the value that leaves the last hop must be a
                route the map declares (the route checks keep that route
                served by the server); a chain that ends at a prefix rule has
                no value to check, so with routes it is not route-verified

An exact value is taken by a hop key first, then by the longest covering
prefix rule. A dynamic control is resolved only by a prefix rule that covers
its whole static prefix: exact keys cannot prove that every value it builds is
handled.
"""

from __future__ import annotations

import dataclasses
from typing import Callable


@dataclasses.dataclass(frozen=True)
class Place:
    rel: str
    line: int


@dataclasses.dataclass
class Hop:
    label: str
    keys: dict[str, list[tuple[str | None, Place]]]   # key -> [(value, place)] in file order
    rules: dict[str, Place]                            # prefix rule -> place
    maps: bool                                         # 2 groups: the value goes on


@dataclasses.dataclass
class Step:
    hop: str
    key: str
    kind: str          # "key", "rule", "route"
    place: Place | None
    value: str | None = None


@dataclasses.dataclass
class Trace:
    control: str
    dynamic: bool
    place: Place
    steps: list[Step]
    status: str        # "ok", "dead", "unrouted"; the engine may set "allowed"
    detail: str = ""


def resolve(controls: dict[str, Place], dynamic: dict[str, Place], hops: list[Hop],
            explains: Callable[[str], bool] | None = None,
            as_route: Callable[[str], str] = lambda value: value) -> list[Trace]:
    """One trace per control, in a stable order (exact values, then prefixes).
    `as_route` turns the value leaving the last hop into the route it names
    (a template's `${id}` into `{id}`) before `explains` judges it."""
    traces = [_follow(value, False, place, hops, explains, as_route)
              for value, place in sorted(controls.items())]
    traces += [_follow(prefix, True, place, hops, explains, as_route)
               for prefix, place in sorted(dynamic.items())]
    return traces


def _covering_rule(hop: Hop, value: str) -> str | None:
    hits = [rule for rule in hop.rules if value.startswith(rule)]
    return max(hits, key=len) if hits else None


def _follow(control: str, dynamic: bool, place: Place, hops: list[Hop],
            explains: Callable[[str], bool] | None, as_route: Callable[[str], str]) -> Trace:
    trace = Trace(control, dynamic, place, [], "ok")
    current = control
    for hop in hops:
        if not dynamic and current in hop.keys:
            # the last binding is the one a dict assignment or a later
            # registration leaves in force
            value, where = hop.keys[current][-1]
            trace.steps.append(Step(hop.label, current, "key", where, value))
            if hop.maps:
                if not value:
                    trace.status = "dead"
                    trace.detail = (f"{hop.label} has {current!r} but gave it no value to "
                                    "follow (its second group did not match)")
                    return trace
                current = value
            continue
        rule = _covering_rule(hop, current)
        if rule is not None:
            trace.steps.append(Step(hop.label, rule, "rule", hop.rules[rule]))
            if explains is not None:
                trace.status = "unrouted"
                trace.detail = (f"the chain ends at the prefix rule {rule!r} in {hop.label}, "
                                "so no route can be checked -- map the value it leads to, or "
                                "allow it with a reason")
            return trace
        trace.status = "dead"
        if dynamic and not current:
            trace.detail = ("its value is computed entirely at run time, so no hop can prove "
                            "it handled -- give it a static prefix, or allow it with a reason")
        elif dynamic:
            narrower = sorted(k for k in hop.keys if k.startswith(current))[:3]
            trace.detail = (f"no prefix rule in {hop.label} covers values starting with "
                            f"{current!r}" + (f" (exact keys {', '.join(narrower)} cover only "
                                              "some of them)" if narrower else ""))
        else:
            trace.detail = f"{current!r} is not a key of {hop.label} and no prefix rule takes it"
        return trace
    if explains is not None:
        route = as_route(current)
        if explains(route):
            trace.steps.append(Step("routes", route, "route", None))
        else:
            trace.status = "unrouted"
            trace.detail = f"the chain ends in {route!r}, which no mapped link declares as a route"
    return trace


def conflicts(hop: Hop) -> list[tuple[str, list[tuple[str | None, Place]]]]:
    """Keys bound twice to different values: the later binding silently wins."""
    return [(key, entries) for key, entries in sorted(hop.keys.items())
            if len({value for value, _ in entries}) > 1]


def orphans(first: Hop, controls: dict[str, Place], dynamic: dict[str, Place]) -> list[str]:
    """Keys and rules of the first hop that no control reaches any more. An
    exact key under a dynamic control's prefix counts as reached: the control
    may build that very value, even though only a rule can prove it handled."""
    def reached(key: str) -> bool:
        return key in controls or any(key.startswith(prefix) for prefix in dynamic if prefix)

    keys = [key for key in first.keys if not reached(key)]
    rules = [rule for rule in first.rules
             if not any(value.startswith(rule) for value in controls)
             and not any(prefix and (prefix.startswith(rule) or rule.startswith(prefix))
                         for prefix in dynamic)]
    return sorted(set(keys) | set(rules))


def self_test() -> list[str]:
    """Planted cases; returns failure descriptions (empty when all hold)."""
    here = Place("a", 1)
    hops = [Hop("table", {"place-order": [("placeOrder", here)], "gone": [("gone", here)],
                          "blank": [(None, here)]},
                {"export:": here}, True),
            Hop("registry", {"placeOrder": [("submitOrder", here)]}, {}, True),
            Hop("defined", {"submitOrder": [(None, here)]}, {}, False)]
    traces = {t.control: t for t in resolve(
        {"place-order": here, "missing": here, "export:csv": here, "blank": here},
        {"export:": here, "import:": here, "": here}, hops)}
    cases = {
        "exact value through every hop": traces["place-order"].status == "ok"
        and [s.kind for s in traces["place-order"].steps] == ["key"] * 3,
        "exact value nobody takes is dead": traces["missing"].status == "dead",
        "exact value under a prefix rule": traces["export:csv"].status == "ok"
        and traces["export:csv"].steps[0].kind == "rule",
        "dynamic prefix covered by a rule": traces["export:"].status == "ok",
        "dynamic prefix no rule covers is dead": traces["import:"].status == "dead",
        "fully computed value is dead": traces[""].status == "dead",
        "map key with no value is dead": traces["blank"].status == "dead",
        "orphan key is reported": orphans(hops[0], {"place-order": here}, {"export:": here})
        == ["blank", "gone"],
        "conflict is reported": conflicts(Hop("d", {"k": [("a", here), ("b", here)]}, {}, True))
        != [],
        "same value twice is no conflict": conflicts(Hop("d", {"k": [("a", here), ("a", here)]},
                                                         {}, True)) == [],
        "last binding is followed": resolve({"k": here}, {}, [Hop("d", {"k": [("a", here), ("b", here)]},
                                                                  {}, True)])[0].steps[0].value == "b",
    }
    routed = resolve({"/v1/x": here}, {}, [], explains=lambda v: v == "/v1/y")
    cases["undeclared route is unrouted"] = routed[0].status == "unrouted"
    formed = resolve({"/v1/o/${id}": here}, {}, [], explains=lambda v: v == "/v1/o/{id}",
                     as_route=lambda v: v.replace("${", "{"))
    cases["route form is judged and recorded"] = formed[0].status == "ok" \
        and formed[0].steps[-1].key == "/v1/o/{id}"
    by_rule = resolve({"export:csv": here}, {}, hops[:1], explains=lambda v: True)
    cases["rule end is not route-verified"] = by_rule[0].status == "unrouted"
    return [f"ui_actions: {name}" for name, ok in cases.items() if not ok]
