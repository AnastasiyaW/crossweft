"""crossweft engine -- a system map of blocks, links, flows and data provenance
in ONE declarative registry that is verified against the code and rendered into
documentation.

Why this exists
---------------
A product that spans several languages and processes (a client, a service, a
backend, installers, a build plane) is held together by seams: HTTP routes,
pipe names, protocol versions, headers, DTO fields, environment variables,
generated contracts. When such a seam is described only in prose -- or not at
all -- a change on one side silently leaves the other side behind, and nothing
fails until integration or a customer install. The registry in
``<model_dir>/*.json`` answers four questions -- which blocks exist, who talks
to whom and how, who waits for what, and where each piece of data comes from --
and this engine keeps those answers TRUE:

  anchors     every block/link/data/finding claim points at an exact literal
              (or regex) in a file. A missing literal means the code or the
              map moved; it always fails (the map must be corrected).
  joins       a value both sides of a seam must agree on (pipe name, protocol
              version, header, route) is extracted from EACH side; all values
              must be identical -- including every occurrence on one side.
  sets        two sides that must expose the same members (DTO fields, env
              vars, route lists) are compared as sets.
  pairs       two hand-duplicated regions that cannot be compared by value
              (the same algorithm in two languages) are fingerprinted; a change
              to either region fails until someone re-reads the other one and
              attests the pair again, with a written reason.
  routes      every route the server registers must belong to a mapped link;
              every route a link says the server serves must be registered;
              every client route literal must be explained by a mapped link.
  provenance  data may leave a block only if that block created it or
              received it over another mapped link ("where does it come from").
  seams       every current link whose two ends are both our code says how the
              sides are kept in agreement (contract.enforcement). A single
              owner (shared code, generated file, shared schema) must be named
              in contract.defined_in; a hand-written seam (duplicated,
              convention, none) must be compared mechanically by a join, set or
              pair that reads BOTH sides -- otherwise updating one side
              silently leaves the other behind.
  status      a current link may not depend on a legacy/planned block.
  coverage    every top-level source area is placed on the map.
  agents      with agent.harness, the repository carries its own agent harness
              (hooks, AGENTS.md rules, skill), so every agent session gets the
              seams without installing anything (crossweft/harness.py).
  findings    known mismatches are recorded with owner and next step. A check
              problem that matches a non-closed finding is KNOWN (reported,
              not failing); a finding whose detector no longer fires is STALE
              and fails, so the register cannot silently rot.

Exit codes: 0 = consistent, 1 = problems found, 2 = infrastructure error.
Fail-loud rules: zero inputs is an error, never a pass; every model path stays
inside the repository; unknown config keys and fields are errors. Stdlib only;
console output is ASCII-only (Windows cp1252 safe). Generated documents are UTF-8.
"""

from __future__ import annotations

import bisect
import contextlib
import dataclasses
import datetime
import difflib
import fnmatch
import hashlib
import html
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

from . import fallbacks, harness, hazards, i18n, joints, ui_actions
from .scope import scope_hit

SCHEMA_ID = "crossweft.map.v1"
CONFIG_FILE = "crossweft.json"
GENERATED_FILES = ("README.md", "blocks.md", "flows.md", "data.md", "findings.md", "viewer.html")
PACKAGED_VIEWER = Path(__file__).with_name("viewer.template.html")
PAIR_BEGIN = "crossweft:begin"
PAIR_END = "crossweft:end"

ENTITY_KEYS = ("blocks", "links", "flows", "data", "joins", "sets", "pairs", "ui_actions",
               "findings")
TOP_LEVEL_KEYS = set(ENTITY_KEYS) | {"meta", "$comment"}
# A joint plugin's kind is a model key and a problem-key prefix: it may not be
# one of crossweft's own (crossweft/joints.py).
RESERVED_JOINT_KINDS = (TOP_LEVEL_KEYS | {key.rstrip("s") for key in ENTITY_KEYS}
                        | {"anchor", "anchors", "route", "routes", "status", "provenance", "seam",
                           "seams", "evidence", "coverage", "agents", "escape", "path_case",
                           "joints", "requirements", "freshness", "hazard",
                           "hazards", "ui"})

KINDS = {"actor", "executable", "service", "component", "library", "store", "external",
         "artifact", "tool", "contract", "site", "host", "ci", "group"}
STATUSES = ("current", "planned", "out-of-scope", "test-only", "legacy")
TRANSPORTS = {"named-pipe", "https", "http-loopback", "process-launch", "file", "registry",
              "scm", "in-process", "generated-code", "build-input", "embedded", "git", "tunnel",
              "smtp", "sql", "object-store", "cache", "webhook", "oauth", "manual", "ui",
              "mtls", "dpapi", "docker", "os-api", "ssh"}
EVIDENCE = ("none", "source", "ci", "runtime")
SEVERITIES = ("blocker", "major", "minor", "info")
FINDING_STATUSES = ("open", "accepted", "closed")
FINDING_KINDS = {"contract-mismatch", "unserved-route", "unmapped-route", "dead-code",
                 "hand-duplicated", "doc-drift", "legacy-leak", "hidden-dependency",
                 "missing-runtime-proof", "provenance-gap", "config-drift", "security-gap",
                 "product-decision", "other"}
DEFER_REASONS = {"missing-data", "missing-dep", "arch-decision", "scope-explosion",
                 "inaccessible-repo"}
ENFORCEMENT = {"shared-code", "generated", "schema-tests", "duplicated", "convention", "none"}
# One owner keeps both sides agreeing by construction (a shared header, a
# generated file, one schema with shared vectors). The claim is only worth
# something if the owner is named, so contract.defined_in is mandatory.
SINGLE_OWNER_ENFORCEMENT = {"shared-code", "generated", "schema-tests"}
# Each side writes the contract by hand. When one side is updated nothing but a
# mechanical comparison (join/set reading both sides) notices the other one.
HAND_ENFORCEMENT = {"duplicated", "convention", "none"}
# Both ends live in one binary and the compiler checks the call: the signature
# is the contract, so such links need not declare one.
COMPILER_CHECKED_TRANSPORTS = {"in-process"}
SYNC = {"request-response", "one-way", "stream", "poll", "startup", "build-time", "launch",
        "event", "read", "write"}
DATA_KINDS = {"credential", "key", "config", "artifact", "payload", "state", "identity",
              "policy", "message", "image", "evidence", "telemetry", "code", "account"}
SENSITIVITY = {"secret", "personal", "internal", "public"}
IDENTIFIER_KINDS = {"route", "pipe", "env", "file", "registry", "scm", "header", "arg",
                    "host", "frame", "resource"}
# "basename": the last path segment (/ or \), so a `files` side (repository
# paths) can be compared with a list of bare file names.
# "go-http-status": a Go net/http status constant name (StatusTooManyRequests)
# becomes its number ("429"), so a Go handler compares with numeric statuses on
# the other side. An unknown name stays as it is and so never equals a number:
# a guard reading it fails instead of matching. Table = Go's net/http/status.go.
TRANSFORMS = {"unescape-c", "lower", "strip", "csv-words", "basename", "go-http-status"}
GO_HTTP_STATUS = {
    "StatusContinue": 100, "StatusSwitchingProtocols": 101, "StatusProcessing": 102,
    "StatusEarlyHints": 103, "StatusOK": 200, "StatusCreated": 201, "StatusAccepted": 202,
    "StatusNonAuthoritativeInfo": 203, "StatusNoContent": 204, "StatusResetContent": 205,
    "StatusPartialContent": 206, "StatusMultiStatus": 207, "StatusAlreadyReported": 208,
    "StatusIMUsed": 226, "StatusMultipleChoices": 300, "StatusMovedPermanently": 301,
    "StatusFound": 302, "StatusSeeOther": 303, "StatusNotModified": 304, "StatusUseProxy": 305,
    "StatusTemporaryRedirect": 307, "StatusPermanentRedirect": 308, "StatusBadRequest": 400,
    "StatusUnauthorized": 401, "StatusPaymentRequired": 402, "StatusForbidden": 403,
    "StatusNotFound": 404, "StatusMethodNotAllowed": 405, "StatusNotAcceptable": 406,
    "StatusProxyAuthRequired": 407, "StatusRequestTimeout": 408, "StatusConflict": 409,
    "StatusGone": 410, "StatusLengthRequired": 411, "StatusPreconditionFailed": 412,
    "StatusRequestEntityTooLarge": 413, "StatusRequestURITooLong": 414,
    "StatusUnsupportedMediaType": 415, "StatusRequestedRangeNotSatisfiable": 416,
    "StatusExpectationFailed": 417, "StatusTeapot": 418, "StatusMisdirectedRequest": 421,
    "StatusUnprocessableEntity": 422, "StatusLocked": 423, "StatusFailedDependency": 424,
    "StatusTooEarly": 425, "StatusUpgradeRequired": 426, "StatusPreconditionRequired": 428,
    "StatusTooManyRequests": 429, "StatusRequestHeaderFieldsTooLarge": 431,
    "StatusUnavailableForLegalReasons": 451, "StatusInternalServerError": 500,
    "StatusNotImplemented": 501, "StatusBadGateway": 502, "StatusServiceUnavailable": 503,
    "StatusGatewayTimeout": 504, "StatusHTTPVersionNotSupported": 505,
    "StatusVariantAlsoNegotiates": 506, "StatusInsufficientStorage": 507,
    "StatusLoopDetected": 508, "StatusNotExtended": 510,
    "StatusNetworkAuthenticationRequired": 511,
}
ROUTE_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "ANY", "MOUNT")
ROUTE_SCANNERS = {"go-chi", "regex", "express", "fastapi", "flask", "gin", "echo"}
AGENT_STOP_MODES = ("block", "warn")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$")
FINDING_ID_RE = re.compile(r"^[A-Z]{2,4}-\d{2,4}$")
DEFAULT_REQUIREMENT_ID = r"^[A-Z][A-Z0-9]*-?\d+$"
ROUTE_DECL_RE = re.compile(r"^(" + "|".join(ROUTE_METHODS) + r") (/\S*)$")
# route parameters in any common spelling -- {id}, <int:id>, :id -- normalise to {}
ROUTE_PARAM_RES = (re.compile(r"\{[^}]*\}"), re.compile(r"<[^>]*>"),
                   re.compile(r"(?<=/):[A-Za-z_][A-Za-z0-9_]*"))
REF_RE = re.compile(r"^(block|link|flow|data|join|set|pair|finding):([A-Za-z0-9-]+)$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

STATUS_RANK = {"current": 0, "planned": 1, "out-of-scope": 1, "test-only": 1, "legacy": 2}

# Go chi route registration helpers (see Checker.go_routes). The verbs are the
# chi.Router methods that register a route (Method/MethodFunc take the HTTP
# method first); Route/Group only open a scope.
GO_ROUTE_VERBS = ("Get", "Post", "Put", "Patch", "Delete", "Head", "Options", "Connect", "Trace",
                  "Handle", "HandleFunc", "Mount", "Method", "MethodFunc")
_GO_VERB = "(" + "|".join(GO_ROUTE_VERBS) + r")\s*\("
GO_ROUTE_CALL_RE = re.compile(r"\.\s*" + _GO_VERB)
# a name or a selector -- `r`, `s.router`, `app.api.v1`; its group is the last name
_GO_SELECTOR = r"(?:[A-Za-z_]\w*\s*\.\s*)*([A-Za-z_]\w*)"
# the same call on a router variable or field, optionally through .With(...) middleware
GO_ROUTER_CALL_RE = re.compile(r"(?<![\w.])" + _GO_SELECTOR
                               + r"(?:\s*\.\s*With\s*\((?:[^()]|\([^()]*\))*\))*\s*\.\s*" + _GO_VERB)
# names (variables, parameters, struct fields) that hold a chi router:
# `r := chi.NewRouter()`, `s.router = chi.NewRouter()`, `&server{mux: chi.NewRouter()}`,
# `func(v1 chi.Router)`, `router *chi.Mux` in a struct
GO_ROUTER_VAR_RE = re.compile(r"\b([A-Za-z_]\w*)[ \t]*(?::=|=|:)[ \t]*chi\.New(?:Router|Mux)\s*\(\s*\)"
                              r"|\b([A-Za-z_]\w*)\s+(?:chi\.Router|\*chi\.Mux)\b")
# a name set from another one: `api := r.With(mw)`, `s.api = s.router.Group(nil)`,
# `&server{router: r}`, `s.router = r`
GO_ROUTER_DERIVED_RE = re.compile(r"\b([A-Za-z_]\w*)[ \t]*(?::=|=|:)[ \t]*&?[ \t]*" + _GO_SELECTOR
                                  + r"[ \t]*(?:\.\s*(?:With|Group|Route)\s*\(|(?=[,;})\n]))")
# names that hold a path: `const ordersPath = "/v1/orders"`, `p := "/x"`
GO_PATH_NAME_RE = re.compile(r"\b([A-Za-z_]\w*)(?:[ \t]+string)?[ \t]*:?=[ \t]*[\"`]/")
GO_STRING_RE = re.compile(r'"((?:[^"\\\n]|\\.)*)"|`([^`]*)`')
GO_HTTP_METHOD_RE = re.compile(r"http\.Method([A-Z][a-z]+)")
GO_ROUTE_SCOPE_RE = re.compile(r"\.\s*Route\s*\(")
GO_FUNC_LITERAL_RE = re.compile(r"\s*func\s*\(")
GO_FUNC_RE = re.compile(r"^func\s+(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.MULTILINE)
GO_NEW_ROUTER_RE = re.compile(r"chi\.NewRouter\(\)")


def _go_arg(text: str, pos: int) -> tuple[str, int]:
    """The Go expression that starts at `pos` and runs to the next top-level
    ',' or ')', and the index of that delimiter (len(text) when there is none)."""
    depth, i, n = 0, pos, len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'":
            i += 1
            while i < n and text[i] != ch and text[i] != "\n":
                i += 2 if text[i] == "\\" else 1
        elif ch == "`":
            end = text.find("`", i + 1)
            i = n if end < 0 else end
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        i += 1
    i = min(i, n)
    return text[pos:i].strip(), i


def _go_literal(expr: str) -> str | None:
    """The value of a Go string literal ("..." or `...`), or None for any other
    expression (a constant, a concatenation, a call)."""
    match = GO_STRING_RE.fullmatch(expr)
    if not match:
        return None
    return match.group(1) if match.group(1) is not None else match.group(2)



# --------------------------------------------------------------------------- #
# Native route scanners beyond go-chi: express, fastapi, flask, gin, echo.
# Each reads one file lexically (comments and strings are skipped), follows the
# router objects the file creates -- groups, mounted routers, included routers,
# blueprints -- and reports every registration it cannot resolve statically.
# --------------------------------------------------------------------------- #

NATIVE_LANG = {"express": "js", "fastapi": "py", "flask": "py", "gin": "go", "echo": "go"}
_HTTP = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
# registration name -> HTTP method; other names a scanner handles are in NATIVE_SPECIAL
NATIVE_VERBS = {
    "express": {**{v.lower(): v for v in _HTTP}, "all": "ANY"},
    "fastapi": {**{v.lower(): v for v in _HTTP}, "trace": "TRACE", "websocket": "GET"},
    "flask": {v.lower(): v for v in _HTTP[:5]},
    "gin": {**{v: v for v in _HTTP}, "Any": "ANY", "StaticFile": "GET", "StaticFileFS": "GET"},
    "echo": {**{v: v for v in _HTTP}, "CONNECT": "CONNECT", "TRACE": "TRACE", "Any": "ANY",
             "File": "GET"},
}
NATIVE_SPECIAL = {
    "express": {"route", "use", "register"},
    "fastapi": {"api_route", "add_api_route", "include_router", "mount"},
    "flask": {"route", "add_url_rule", "register_blueprint"},
    "gin": {"Handle", "Match", "Group", "Static", "StaticFS"},
    "echo": {"Add", "Match", "Group", "Static"},
}
# constructor call chains (see _NativeRoutes.signature) -> keyword holding the
# router's own prefix ("" = none)
NATIVE_ROOTS = {
    "express": {"express()": "", "express.Router()": "", "Router()": "", "fastify()": "",
                "Fastify()": ""},
    "fastapi": {"FastAPI()": "", "fastapi.FastAPI()": "", "APIRouter()": "prefix",
                "fastapi.APIRouter()": "prefix"},
    "flask": {"Flask()": "", "flask.Flask()": "", "Blueprint()": "url_prefix",
              "flask.Blueprint()": "url_prefix"},
    "gin": {"gin.Default()": "", "gin.New()": ""},
    "echo": {"echo.New()": ""},
}
# typed parameters and fields: the root type is a router at "", a group type is a
# router whose prefix only the caller knows
NATIVE_TYPED = {
    "express": (re.compile(r"(?<=[(,])\s*([A-Za-z_$][\w$]*)\s*:\s*(?:express\s*\.\s*)?"
                           r"(Express|Application|Router|FastifyInstance)\b"),
                {"Express", "Application"}),
    "fastapi": (re.compile(r"(?<=[(,])\s*([A-Za-z_]\w*)\s*:\s*(?:fastapi\s*\.\s*)?"
                           r"(FastAPI|APIRouter)\b"), {"FastAPI"}),
    "flask": (re.compile(r"(?<=[(,])\s*([A-Za-z_]\w*)\s*:\s*(?:flask\s*\.\s*)?"
                         r"(Flask|Blueprint)\b"), {"Flask"}),
    "gin": (re.compile(r"\b([A-Za-z_]\w*)\s+\*?gin\s*\.\s*(Engine|RouterGroup|IRouter|IRoutes)\b"),
            {"Engine"}),
    "echo": (re.compile(r"\b([A-Za-z_]\w*)\s+\*?echo\s*\.\s*(Echo|Group)\b"), {"Echo"}),
}
NATIVE_ASSIGN_RES = {
    "go": re.compile(r"(?<![\w.])([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)*)[ \t]*(?::=|=(?!=))[ \t]*"),
    "js": re.compile(r"(?<![\w.$])(?:(?:const|let|var)\s+)?"
                     r"([A-Za-z_$][\w$]*(?:\s*\.\s*[A-Za-z_$][\w$]*)*)[ \t]*=(?![=>])[ \t]*"),
    "py": re.compile(r"^[ \t]*([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)[ \t]*(?::[^=\n]*)?=(?!=)[ \t]*",
                     re.MULTILINE),
}
# a file that builds a router of each kind (router_search: scanned, ignored, or a problem)
ROUTER_FILE_RES = {
    "go-chi": GO_NEW_ROUTER_RE,
    "express": re.compile(r"\bexpress\s*\(\s*\)|\bexpress\s*\.\s*Router\s*\(|\b[Ff]astify\s*\("
                          r"|require\(\s*['\"]fastify['\"]\s*\)\s*\("),
    "fastapi": re.compile(r"\b(?:FastAPI|APIRouter)\s*\("),
    "flask": re.compile(r"\b(?:Flask|Blueprint)\s*\("),
    "gin": re.compile(r"\bgin\s*\.\s*(?:Default|New)\s*\(\s*\)"),
    "echo": re.compile(r"\becho\s*\.\s*New\s*\(\s*\)"),
}
_IDENT_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$")
_PY_STR_RE = re.compile(r"([rRuUbBfF]{0,2})('''|\"\"\"|'|\")(.*)\2", re.DOTALL)
_JS_STR_RE = re.compile(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"|`((?:[^`\\]|\\.)*)`",
                        re.DOTALL)


def _lex_spans(text: str, lang: str) -> list[tuple[int, int]]:
    """Half-open spans of the comments and string literals of a JavaScript /
    TypeScript ("js") or Python ("py") file. JavaScript regex literals are not
    recognised (a quote inside one would open a string)."""
    spans: list[tuple[int, int]] = []
    i, n = 0, len(text)
    while i < n:
        ch, start = text[i], i
        if lang == "js" and text.startswith("//", i) or lang == "py" and ch == "#":
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif lang == "js" and text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif lang == "py" and text.startswith(("'''", '"""'), i):
            end = text.find(text[i:i + 3], i + 3)
            while end > 0 and text[end - 1] == "\\":
                end = text.find(text[i:i + 3], end + 1)
            i = n if end < 0 else end + 3
        elif ch in "\"'" or lang == "js" and ch == "`":
            i += 1
            while i < n and text[i] != ch and (ch == "`" or text[i] != "\n"):
                i += 2 if text[i] == "\\" else 1
            i += 1
        else:
            i += 1
            continue
        spans.append((start, min(i, n)))
    return spans


def _native_path(path: str) -> str:
    """Route parameters in the `{name}` form the go-chi scanner reports:
    `:id`, `:id?`, `:id(\\d+)` (Express, gin, echo), `<int:id>` (Flask) and
    `{id:path}` (FastAPI) all become `{id}`."""
    path = re.sub(r"<(?:[^<>:]+:)?([^<>]+)>", r"{\1}", path)
    path = re.sub(r"\{([^{}:]+):[^{}]*\}", r"{\1}", path)
    return re.sub(r"(?<=/):([A-Za-z_]\w*)(?:\([^)]*\))?\??", r"{\1}", path)


class _RouterObj:
    """A router the scanned file creates (or receives): its own prefix, and the
    routers it is mounted on. `why` says why its prefix cannot be known."""

    __slots__ = ("name", "own", "mounts", "why", "reported")

    def __init__(self, name: str, own: str = "", why: str | None = None) -> None:
        self.name, self.own, self.why = name, own, why
        self.mounts: list[tuple[_RouterObj, str]] = []
        self.reported = False


_IGNORED = object()   # a `receivers` entry mapped to null: not a router


class _NativeRoutes:
    """The registrations of one file for one native scanner.

    Static reading only: a router's prefix must be a literal and the router
    must be created (or mounted) in this file, or named in the router entry's
    `receivers`. Everything else is a `route:unscannable:` problem -- or, for a
    router mounted from another file without a prefix, an [INFO] line --
    never a silently skipped route."""

    def __init__(self, checker: "Checker", rel: str, text: str, scanner: str,
                 receivers: dict[str, str | None]) -> None:
        self.c, self.rel, self.text, self.scanner = checker, rel, text, scanner
        self.lang = NATIVE_LANG[scanner]
        if self.lang == "go":
            self.spans = Checker.go_scan(text)[1]
        else:
            self.spans = _lex_spans(text, self.lang)
        self.span_starts = [start for start, _ in self.spans]
        self.span_end = dict(self.spans)
        self.verbs = NATIVE_VERBS[scanner]
        self.receivers = receivers
        self.objects: dict[object, _RouterObj] = {}
        self.pending: list[tuple[_RouterObj, list[str], str, int]] = []
        self.bindings: dict[str, list[tuple[int, int, str]]] = {}
        self.evaluating: set[int] = set()
        self.values: dict[int, object] = {}
        self.reported: set[str] = set()
        self.problems = 0

    # ------------------------------------------------------------ lexical
    def in_code(self, pos: int) -> bool:
        index = bisect.bisect_right(self.span_starts, pos) - 1
        return index < 0 or pos >= self.spans[index][1]

    def items(self, open_pos: int) -> tuple[list[tuple[str, int]], int]:
        """The top-level comma-separated items after the bracket at `open_pos`
        ((expression, start) pairs), and the index of the closing bracket."""
        text, n = self.text, len(self.text)
        found: list[tuple[str, int]] = []
        depth, start, i = 0, open_pos + 1, open_pos + 1
        while i < n:
            if i in self.span_end:
                i = self.span_end[i]
                continue
            ch = text[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif ch == "," and depth == 0:
                found.append((start, i))
                start = i + 1
            i += 1
        found.append((start, i))
        rows = []
        for a, b in found:
            expr = text[a:b]
            if expr.strip():
                rows.append((expr.strip(), a + len(expr) - len(expr.lstrip())))
        return rows, i

    def closing(self, open_pos: int) -> int:
        return self.items(open_pos)[1]

    def receiver(self, dot: int) -> tuple[str, int]:
        """The receiver expression that ends at the '.' at `dot` (`app`,
        `s.engine`, `router.route('/x').get(h)`) and where it starts."""
        text, end, i = self.text, dot, dot
        while True:
            while i > 0 and text[i - 1].isspace():
                i -= 1
            if i > 0 and text[i - 1] in ")]":
                depth, j = 0, i - 1
                while j >= 0:
                    if not self.in_code(j):
                        index = bisect.bisect_right(self.span_starts, j) - 1
                        j = self.spans[index][0] - 1
                        continue
                    if text[j] in ")]":
                        depth += 1
                    elif text[j] in "([":
                        depth -= 1
                        if depth == 0:
                            break
                    j -= 1
                i = max(j, 0)
            k = i
            while k > 0 and text[k - 1] in _IDENT_CHARS:
                k -= 1
            if k == i:
                break
            i = k
            j = i
            while j > 0 and text[j - 1].isspace():
                j -= 1
            if j > 1 and text[j - 1] == "." and text[j - 2] != ".":
                i = j - 1
                continue
            break
        return text[i:end].strip(), i

    def chain(self, start: int, end: int) -> list[tuple[str, int | None]] | None:
        """`a.b(x).c` between start and end as [(name, open paren or None)], or
        None when it is not such a chain (an operator, a literal, ...)."""
        text, segs, i = self.text, [], start
        ident = re.compile(r"\s*([A-Za-z_$][\w$]*)\s*")
        while i < end:
            match = ident.match(text, i, end)
            if not match:
                return None
            i = match.end()
            if i < end and text[i] == "(":
                segs.append((match.group(1), i))
                i = self.closing(i) + 1
                while i < end and text[i] == "(":       # require('fastify')(...)
                    segs.append(("", i))
                    i = self.closing(i) + 1
            else:
                segs.append((match.group(1), None))
            while i < end and text[i].isspace():
                i += 1
            if i < end and text[i] == "[":
                return None
            if i < end and text[i] == ".":
                i += 1
                continue
            if i < end:
                return None
        return segs or None

    # ----------------------------------------------------------- literals
    def literal(self, expr: str) -> tuple[str | None, str]:
        """(value, "") for a plain string literal, else (None, why not)."""
        if self.lang == "go":
            value = _go_literal(expr)
            return (value, "") if value is not None else (None, "not a string literal")
        if self.lang == "py":
            match = _PY_STR_RE.fullmatch(expr)
            if not match:
                return None, "not a string literal"
            if "f" in match.group(1).lower():
                return None, "an f-string"
            if "b" in match.group(1).lower():
                return None, "a bytes literal"
            body = match.group(3)
            return (body if "r" in match.group(1).lower()
                    else re.sub(r"\\(.)", r"\1", body)), ""
        match = _JS_STR_RE.fullmatch(expr)
        if not match:
            return None, "not a string literal"
        if match.group(3) is not None and "${" in match.group(3):
            return None, "a template literal with ${...}"
        body = next(g for g in match.groups() if g is not None)
        return re.sub(r"\\(.)", r"\1", body), ""

    def method_of(self, expr: str) -> str | None:
        constant = GO_HTTP_METHOD_RE.fullmatch(expr) if self.lang == "go" else None
        if constant:
            return constant.group(1).upper()
        value = self.literal(expr)[0]
        return value.upper() if value and value.isalpha() else None

    def methods(self, expr: str, start: int) -> list[str] | None:
        """HTTP methods from a literal, a list/tuple/array of them, or a Go
        `[]string{...}` composite; None when any element is not a literal."""
        single = self.method_of(expr)
        if single:
            return [single]
        offset = expr.find("{") if self.lang == "go" else 0
        if offset < 0 or expr[offset] not in "[({":
            return None
        rows, _ = self.items(start + offset)
        found = [self.method_of(item) for item, _ in rows]
        return None if not found or None in found else found  # type: ignore[return-value]

    def split_args(self, rows: list[tuple[str, int]]
                   ) -> tuple[list[tuple[str, int]], dict[str, tuple[str, int]]]:
        """Positional arguments and Python keyword arguments."""
        positional, keywords = [], {}
        for expr, start in rows:
            match = re.match(r"([A-Za-z_]\w*)\s*=(?!=)\s*", expr) if self.lang == "py" else None
            if match:
                keywords[match.group(1)] = (expr[match.end():], start + match.end())
            else:
                positional.append((expr, start))
        return positional, keywords

    def object_props(self, expr: str, start: int) -> dict[str, tuple[str, int]]:
        """`{method: 'GET', url: '/x'}` -> {name: (expression, start)}."""
        if not expr.startswith("{"):
            return {}
        rows, _ = self.items(start)
        props = {}
        for item, at in rows:
            match = re.match(r"['\"]?([A-Za-z_$][\w$]*)['\"]?\s*:\s*", item)
            if match:
                props[match.group(1)] = (item[match.end():], at + match.end())
        return props

    # ----------------------------------------------------------- problems
    def unscannable(self, name: str, expr: str, pos: int, what: str) -> None:
        expr = " ".join(expr.split())
        key = f"route:unscannable:{self.rel}:{name} {expr}"
        if key in self.reported:
            return
        self.reported.add(key)
        self.problems += 1
        line = self.c.line_at(self.rel, pos)
        self.c.add(key, "routes",
                   f"{self.rel}:{line}: {name}({expr}, ...) -- {what}, so the {self.scanner} "
                   "scanner cannot tell which route this is; write it as a literal, name the "
                   "receiver in the router's `receivers`, or record a finding with this key",
                   path=self.rel, line=line)

    def info(self, pos: int, message: str) -> None:
        self.c.info.append(f"route scan {self.rel}:{self.c.line_at(self.rel, pos)}: {message}")

    # ------------------------------------------------------------ binding
    def collect_bindings(self) -> None:
        """Every place a name is set: assignments (value read lazily) and
        parameters or fields typed as a router."""
        for match in NATIVE_ASSIGN_RES[self.lang].finditer(self.text):
            if not self.in_code(match.start(1)):
                continue
            name = re.sub(r"\s+", "", match.group(1))
            self.bindings.setdefault(name, []).append((match.start(1), match.end(), "assign"))
        typed, roots = NATIVE_TYPED[self.scanner]
        for match in typed.finditer(self.text):
            if self.in_code(match.start(1)):
                kind = "root" if match.group(2) in roots else "group:" + match.group(2)
                self.bindings.setdefault(match.group(1), []).append(
                    (match.start(1), match.start(1), kind))
        for rows in self.bindings.values():
            rows.sort()

    def rhs_end(self, start: int) -> int:
        """The end of the expression assigned at `start` (end of statement)."""
        text, n, depth, i = self.text, len(self.text), 0, start
        while i < n:
            if i in self.span_end:
                i = self.span_end[i]
                continue
            ch = text[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and ch in "\n;,":
                break
            i += 1
        return i

    def lookup(self, name: str, pos: int) -> object:
        """The router (or route context) `name` holds at `pos`: the closest
        earlier binding, else the first one (hoisted functions use names
        bound further down); None when the name is not a router."""
        if name in self.receivers:
            prefix = self.receivers[name]
            if prefix is None:
                return _IGNORED
            return (self.make(("recv", name), name, prefix), None)
        rows = self.bindings.get(name)
        if not rows:
            return None
        earlier = [row for row in rows if row[0] < pos]
        at, value_start, kind = earlier[-1] if earlier else rows[0]
        if kind == "root":
            return (self.make(("typed", at), name, ""), None)
        if kind.startswith("group:"):
            obj = self.make(("typed", at), name, "",
                            f"`{name}` is a parameter or field typed {kind[6:]}: its prefix is set "
                            "by code this scanner does not follow")
            return (obj, None)
        if at in self.values:
            return self.values[at]
        if at in self.evaluating:
            return None
        self.evaluating.add(at)
        end = self.rhs_end(value_start)
        start = value_start
        while True:
            match = re.compile(r"\s*(?:await|new|&)\s*").match(self.text, start, end)
            if not match or match.end() == start:
                break
            start = match.end()
        value = self.evaluate(start, end, name)
        self.evaluating.discard(at)
        self.values[at] = value
        return value

    def make(self, key: object, name: str, own: str, why: str | None = None) -> _RouterObj:
        if key not in self.objects:
            self.objects[key] = _RouterObj(name, own, why)
        return self.objects[key]

    @staticmethod
    def signature(segs: list[tuple[str, int | None]]) -> str:
        """`express.Router()`, `require()()`: the names and calls of a chain."""
        parts: list[str] = []
        for name, open_ in segs:
            if name:
                parts.append(name + ("()" if open_ is not None else ""))
            elif parts:
                parts[-1] += "()"
        return ".".join(parts)

    def evaluate(self, start: int, end: int, name: str) -> object:
        """The router a chain expression yields: (obj, route path or None),
        _IGNORED, or None (not a router)."""
        segs = self.chain(start, end)
        if not segs:
            return None
        roots = NATIVE_ROOTS[self.scanner]
        for cut in range(len(segs), 0, -1):
            if segs[cut - 1][1] is None:
                continue
            sig = self.signature(segs[:cut])
            fastify = (self.scanner == "express" and sig == "require()()" and re.match(
                r"require\(\s*['\"]fastify['\"]", self.text[start:end].lstrip()))
            if sig in roots or fastify:
                keyword = roots.get(sig, "")
                own, why = "", None
                if keyword:
                    _, kwargs = self.split_args(self.items(segs[cut - 1][1])[0])
                    if keyword in kwargs:
                        own, why_not = self.literal(kwargs[keyword][0])
                        if own is None:
                            why = f"its {keyword}= is {why_not}"
                            self.unscannable(sig[:-2], f"{keyword}={kwargs[keyword][0]}",
                                             kwargs[keyword][1], why)
                            own = ""
                obj = self.make(("new", segs[cut - 1][1]), name, own, why)
                if why:
                    obj.reported = True
                return self.follow((obj, None), segs[cut:])
        names = []
        for seg_name, open_ in segs:
            if open_ is not None:
                break
            names.append(seg_name)
        if not names:
            return None
        dotted = ".".join(names)
        candidates = [dotted]
        if len(names) > 1 and (self.lang == "go" or names[0] in ("self", "this")):
            candidates.append(names[-1])
        value = None
        for candidate in candidates:
            if candidate in self.receivers or candidate in self.bindings:
                value = self.lookup(candidate, start)
                break
        if value is None or value is _IGNORED:
            return value
        return self.follow(value, segs[len(names):])

    def follow(self, value: object, segs: list[tuple[str, int | None]]) -> object:
        """Apply the remaining calls of a chain: groups, `.route(path)`, and the
        registrations that return their own receiver."""
        for seg_name, open_ in segs:
            if value is None or value is _IGNORED or open_ is None:
                return None if open_ is None else value
            obj, route_path = value  # type: ignore[misc]
            if self.scanner in ("gin", "echo") and seg_name == "Group":
                value = (self.group(obj, open_), None)
            elif self.scanner == "express" and seg_name == "route" and route_path is None:
                rows, _ = self.items(open_)
                if not rows:
                    return None
                path, why = self.literal(rows[0][0])
                if path is None:
                    self.unscannable("route", rows[0][0], open_, f"the path is {why}")
                    return _IGNORED
                value = (obj, path)
            elif route_path is not None and seg_name in self.verbs:
                continue
            else:
                return None
        return value

    def group(self, parent: _RouterObj, open_: int) -> _RouterObj:
        key = ("group", open_)
        if key in self.objects:
            return self.objects[key]
        rows, _ = self.items(open_)
        prefix, why = self.literal(rows[0][0]) if rows else (None, "missing")
        obj = self.make(key, f"{parent.name}.Group({rows[0][0] if rows else ''})", "")
        if prefix is None:
            obj.why, obj.reported = f"the group prefix is {why}", True
            self.unscannable("Group", rows[0][0] if rows else "", open_,
                             f"the group prefix is {why}; the routes below it are not checked")
        else:
            obj.mounts.append((parent, prefix))
        return obj

    # --------------------------------------------------------------- scan
    def run(self) -> list[tuple[str, str, int]]:
        self.collect_bindings()
        names = sorted(set(self.verbs) | NATIVE_SPECIAL[self.scanner], key=len, reverse=True)
        call = re.compile(r"\.\s*(" + "|".join(names) + r")\s*\(")
        for match in call.finditer(self.text):
            if self.in_code(match.start()):
                self.registration(match.group(1), match.start(), match.end() - 1)
        return self.resolve()

    def registration(self, verb: str, dot: int, open_: int) -> None:
        rows, _ = self.items(open_)
        fastify_route = verb == "route" and self.scanner == "express" and bool(rows) \
            and rows[0][0].startswith("{")
        if verb == "Group" or (verb == "route" and self.scanner == "express"
                               and not fastify_route):
            return       # a scope: read through the receivers that use it
        recv_expr, recv_start = self.receiver(dot)
        positional, kwargs = self.split_args(rows)
        decorator = self.lang == "py" and self.text[:recv_start].rstrip().endswith("@")
        value = self.evaluate(recv_start, dot, recv_expr) if recv_expr else None
        if value is _IGNORED:
            return
        if value is None:
            self.unknown_receiver(verb, recv_expr, dot, positional, decorator)
            return
        obj, route_path = value  # type: ignore[misc]
        if verb in ("use", "include_router", "register_blueprint", "register", "mount",
                    "Static", "StaticFS"):
            self.mount(obj, verb, positional, kwargs, open_)
            return
        if fastify_route:                                 # fastify.route({method, url, ...})
            props = self.object_props(*positional[0])
            url, method = props.get("url") or props.get("path"), props.get("method")
            if url is None or method is None:
                self.unscannable(verb, positional[0][0][:60], dot,
                                 "it has no `method` and `url` properties")
                return
            paths, methods = self.paths(verb, url), self.methods(*method)
            if methods is None:
                self.unscannable(verb, f"method: {method[0]}", dot, "the method is not a literal")
                return
        else:
            method_expr = None
            if verb in ("Handle", "Add", "Match"):
                if len(positional) < 3:
                    return
                method_expr, positional = positional[0], positional[1:]
            if route_path is not None:
                paths = [route_path]
            else:
                path_arg = kwargs.get("path") or kwargs.get("rule") or (
                    positional[0] if positional else None)
                if path_arg is None or self.lang in ("js", "go") and len(positional) < 2:
                    return      # app.get('env'): a settings getter, not a route
                paths = self.paths(verb, path_arg)
            if method_expr is not None:
                methods = self.methods(*method_expr)
                if methods is None:
                    self.unscannable(verb, method_expr[0], dot, "the method is not a literal "
                                     "(or an http.Method constant)")
                    return
            elif verb in ("api_route", "add_api_route", "route", "add_url_rule"):
                methods = ["GET"]
                if "methods" in kwargs:
                    methods = self.methods(*kwargs["methods"])
                    if methods is None:
                        self.unscannable(verb, f"methods={kwargs['methods'][0]}", dot,
                                         "the methods are not a list of literals")
                        return
            else:
                methods = [self.verbs[verb]]
        for path in paths or []:
            self.pending.append((obj, methods, path, dot))

    def paths(self, verb: str, arg: tuple[str, int]) -> list[str] | None:
        expr, start = arg
        exprs = [arg]
        if self.lang == "js" and expr.startswith("["):
            exprs = self.items(start)[0]
        found = []
        for item, _ in exprs:
            path, why = self.literal(item)
            if path is None:
                self.unscannable(verb, expr, start, f"the path is {why}")
                return None
            if path == "*" and self.scanner == "express":
                path = "/*"
            if path and not path.startswith("/"):
                self.unscannable(verb, expr, start, "the path does not start with '/'")
                return None
            found.append(path)
        return found


    def mount(self, obj: _RouterObj, verb: str, positional: list[tuple[str, int]],
              kwargs: dict[str, tuple[str, int]], open_: int) -> None:
        """A router (or app, static directory, plugin) mounted below a prefix:
        a router this file creates is followed; anything else mounted below a
        literal prefix is a MOUNT registration (covered by a declared route
        below it); without a prefix it is an [INFO] line."""
        if not positional:
            return
        prefix_arg: tuple[str, int] | None = None
        children = positional[:1]
        if verb in ("use", "mount", "Static", "StaticFS"):
            first = positional[0]
            values = [self.child(arg) for arg in positional]
            if verb != "use" or self.literal(first[0])[0] is not None or any(
                    isinstance(v, tuple) for v in values[1:]):
                prefix_arg, children = first, positional[1:]
            elif not isinstance(values[0], tuple):
                return                                   # app.use(middleware, ...)
        elif verb == "register":                         # fastify.register(plugin, {prefix})
            options = self.object_props(*positional[1]) if len(positional) > 1 else {}
            prefix_arg = options.get("prefix")
        else:                                            # include_router / register_blueprint
            prefix_arg = kwargs.get("prefix" if verb == "include_router" else "url_prefix")
        prefix = None
        if prefix_arg is not None:
            prefix, why = self.literal(prefix_arg[0])
            if prefix is None:
                self.unscannable(verb, prefix_arg[0], prefix_arg[1],
                                 f"the mount prefix is {why}; the routes below it are not checked")
                return
        routers = [(arg, self.child(arg)) for arg in children]
        routers = [(arg, v) for arg, v in routers if isinstance(v, tuple)]
        if routers and verb in ("use", "include_router", "register_blueprint"):
            for _, (child, _) in routers:
                if self.scanner == "fastapi":
                    child.mounts.append((obj, (prefix or "") + child.own))
                elif self.scanner == "flask":
                    child.mounts.append((obj, child.own if prefix is None else prefix))
                else:
                    child.mounts.append((obj, prefix or ""))
        elif prefix is not None:
            self.pending.append((obj, ["MOUNT"], prefix, open_))
        else:
            self.info(open_, f"{verb}({children[0][0] if children else ''}) registers routes "
                             "this scanner does not follow (another file or a plugin) without a "
                             "prefix -- scan that file as its own router entry")

    def child(self, arg: tuple[str, int]) -> object:
        expr, start = arg
        return self.evaluate(start, start + len(expr), expr)

    def unknown_receiver(self, verb: str, expr: str, dot: int,
                         positional: list[tuple[str, int]], decorator: bool) -> None:
        """A registration-shaped call on a receiver this file neither creates
        nor names in `receivers`: a route in a helper that is handed the router
        (reported), or an HTTP client / map call (not route-shaped: ignored)."""
        first = positional[0][0] if positional else ""
        path_like = bool(re.match(r"[rRuU]?[\"'`]/", first))
        if self.lang == "py":
            shaped = decorator and (verb in self.verbs or verb in ("route", "api_route")) or \
                verb in ("add_api_route", "add_url_rule", "include_router", "register_blueprint")
        elif verb in ("Handle", "Add"):
            shaped = len(positional) >= 3 and self.method_of(first) is not None
        elif verb == "Match":
            shaped = len(positional) >= 3 and first.startswith("[]string")
        elif verb in ("Static", "StaticFS"):
            shaped = len(positional) >= 2 and path_like
        else:
            shaped = verb in self.verbs and len(positional) >= 2 and path_like
        if shaped and expr:
            self.unscannable(f"{expr}.{verb}", first, dot,
                             f"`{expr}` is not a router this file creates")

    def prefixes(self, obj: _RouterObj, seen: frozenset = frozenset()) -> list[str]:
        if obj.why or id(obj) in seen:
            return []
        if not obj.mounts:
            return [obj.own]
        found = []
        for parent, local in obj.mounts:
            for outer in self.prefixes(parent, seen | {id(obj)}):
                found.append(_join_route(outer, local, self.scanner))
        return found


    def resolve(self) -> list[tuple[str, str, int]]:
        routes = []
        for obj, methods, path, pos in self.pending:
            if obj.why:
                if not obj.reported:     # a router handed in: report once, at its first route
                    obj.reported = True
                    self.problems += 1
                    line = self.c.line_at(self.rel, pos)
                    self.c.add(f"route:unscannable:{self.rel}:prefix {obj.name}", "routes",
                               f"{self.rel}:{line}: routes registered on {obj.name} -- {obj.why}, "
                               f"so the {self.scanner} scanner cannot tell their paths; name it "
                               "in the router's `receivers` with its prefix, or record a finding "
                               "with this key", path=self.rel, line=line)
                continue
            for prefix in self.prefixes(obj):
                full = _native_path(_join_route(prefix, path, self.scanner) or "/")
                for method in methods:
                    routes.append((method, full, self.c.line_at(self.rel, pos)))
        return routes


def _join_route(prefix: str, path: str, scanner: str) -> str:
    """A prefix and a route path as the framework joins them: no doubled '/',
    and Express serves a router's "/" at the mount point itself."""
    if not prefix:
        return path
    if not path:
        return prefix
    if scanner == "express" and path == "/":
        return prefix
    return prefix.rstrip("/") + path


class ModelError(Exception):
    """The model files cannot be loaded at all (infrastructure-level)."""


def out(message: str = "") -> None:
    """ASCII-only console output (Windows cp1252 consoles)."""
    print(message.encode("ascii", "backslashreplace").decode("ascii"))


def path_error(value: object) -> str | None:
    """Why `value` is not an acceptable repository-relative path (or glob), or
    None when it is. Model and config paths may never leave the repository:
    a map read in CI comes from a pull request, and a join that extracts a
    value prints it, so `../../outside-repository/secret.txt` must be refused up front."""
    if not isinstance(value, str) or not value.strip():
        return "must be a non-empty repository-relative path"
    if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
        return f"'{value}' is absolute -- paths are relative to the repository root"
    if ".." in PurePosixPath(value.replace("\\", "/")).parts:
        return f"'{value}' contains '..' -- paths may not leave the repository"
    # One spelling per file: git, `impact` and the agent hooks compare paths as
    # written, so a spelling the file system also accepts would silently match nothing.
    if "\\" in value:
        return f"'{value}' uses a backslash -- write repository paths with '/'"
    if "//" in value:
        return f"'{value}' contains '//' -- write each separator once"
    if value != "." and "." in value.rstrip("/").split("/"):
        return f"'{value}' contains a '.' segment -- write it without './'"
    return None


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    seen: dict = {}
    for key, value in pairs:
        if key in seen:
            # json.loads would keep only the LAST value: a second "joins" array
            # appended by an agent would silently drop every guard in the first
            raise ValueError(f"duplicate key '{key}' in one object -- JSON keeps only the last "
                             "one, so the other would be lost silently; merge them")
        seen[key] = value
    return seen


def read_json(path: Path) -> object:
    """Parse a JSON file that crossweft reads (config, model, lock). Raises
    OSError / ValueError -- including for a key repeated in one object."""
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys)


def inside(root: Path, path: Path) -> bool:
    """True when `path` (after resolving symlinks) stays inside `root`."""
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


@dataclasses.dataclass
class Config:
    """Per-repository settings from ``crossweft.json`` at the repository root.

    The file is also the root marker: a repository adopts crossweft by adding
    it, and every command resolves the root by walking up to it."""

    root: Path
    model_dir: Path = Path("seams/model")
    output_dir: Path = Path("seams")
    lock_file: Path = Path("seams/pairs.lock.json")
    viewer_template: Path | None = None
    language: str = "en"
    requirements_doc: Path | None = None
    requirement_id: str = DEFAULT_REQUIREMENT_ID
    check_rendered_docs: bool = False
    agent_stop: str = "block"
    agent_harness: bool = False
    validators_dir: Path | None = None
    validator_timeout: int = 120
    validator_timeouts: dict = dataclasses.field(default_factory=dict)
    validator_exclude: dict = dataclasses.field(default_factory=dict)
    discover_exclude: list = dataclasses.field(default_factory=list)
    joints_dir: Path | None = None
    joints_sources: list = dataclasses.field(default_factory=list)
    hazards_include: list = dataclasses.field(default_factory=list)
    hazards_exclude: list = dataclasses.field(default_factory=list)
    hazards_rules: list = dataclasses.field(default_factory=list)
    feedback: str | None = None
    fallbacks_include: list = dataclasses.field(default_factory=list)
    fallbacks_exclude: list = dataclasses.field(default_factory=list)
    fallbacks_registry: Path = Path("seams/fallbacks.json")
    fallbacks_signers: Path | None = None
    fallbacks_stops: list = dataclasses.field(default_factory=list)
    fallbacks_strict: bool = False

    def abs(self, rel: Path) -> Path:
        return self.root / rel

    @property
    def template_path(self) -> Path:
        return self.abs(self.viewer_template) if self.viewer_template else PACKAGED_VIEWER


CONFIG_KEYS = {"$comment", "model_dir", "output_dir", "lock_file", "viewer_template", "language",
               "requirements", "check_rendered_docs", "agent", "validators", "discover",
               "joints", "hazards", "feedback", "fallbacks"}


MAX_FEEDBACK = 300


def feedback_line(cfg: Config, component: str) -> str:
    """One line pointing the agent at the repository's feedback channel, naming the part of
    crossweft it just used; empty when crossweft.json declares no `feedback`."""
    if not cfg.feedback:
        return ""
    return (f"crossweft feedback: if `crossweft {component}` got something wrong or in your way "
            f"here, report it: {cfg.feedback} (say it was `{component}`)")


def find_repo_root(start: Path) -> Path:
    """Walk up from `start` to the directory that holds ``crossweft.json``."""
    here = start.resolve()
    if here.is_file():
        here = here.parent
    for candidate in [here, *here.parents]:
        if (candidate / CONFIG_FILE).is_file():
            return candidate
    raise ModelError(f"no {CONFIG_FILE} at or above {start} -- run `crossweft init` in the "
                     "repository root, or pass --root")


def load_config(root: Path) -> Config:
    """Read ``<root>/crossweft.json``. Every key is optional; a missing file, a
    malformed file, an unknown key or a wrong type is an error (never a
    silent default)."""
    path = root / CONFIG_FILE
    if not path.is_file():
        raise ModelError(f"{CONFIG_FILE} missing in {root} -- run `crossweft init`")
    try:
        data = read_json(path)
    except (OSError, ValueError) as exc:
        raise ModelError(f"{CONFIG_FILE} is unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise ModelError(f"{CONFIG_FILE} must be a JSON object")
    unknown = set(data) - CONFIG_KEYS
    if unknown:
        raise ModelError(f"{CONFIG_FILE} has unknown keys: {sorted(unknown)} "
                         f"(allowed: {sorted(CONFIG_KEYS - {'$comment'})})")
    cfg = Config(root=root.resolve())

    def rel_path(key: str, value: object) -> Path:
        problem = path_error(value)
        if problem:
            raise ModelError(f"{CONFIG_FILE}: {key} {problem}")
        return Path(str(value))

    def sub_object(key: str, allowed: set[str]) -> dict:
        value = data.get(key, {})
        if not isinstance(value, dict):
            raise ModelError(f"{CONFIG_FILE}: {key} must be an object")
        extra = set(value) - allowed
        if extra:
            raise ModelError(f"{CONFIG_FILE}: {key} has unknown keys {sorted(extra)} "
                             f"(allowed: {sorted(allowed)})")
        return value

    if "model_dir" in data:
        cfg.model_dir = rel_path("model_dir", data["model_dir"])
    if "output_dir" in data:
        cfg.output_dir = rel_path("output_dir", data["output_dir"])
    cfg.lock_file = (rel_path("lock_file", data["lock_file"]) if "lock_file" in data
                     else cfg.output_dir / "pairs.lock.json")
    if cfg.lock_file.parent == cfg.model_dir:
        raise ModelError(f"{CONFIG_FILE}: lock_file may not live in model_dir (every *.json "
                         "there is read as a model file)")
    if "viewer_template" in data:
        cfg.viewer_template = rel_path("viewer_template", data["viewer_template"])
    if "language" in data:
        if not isinstance(data["language"], str) or data["language"] not in i18n.LANGUAGES:
            raise ModelError(f"{CONFIG_FILE}: language must be one of {sorted(i18n.LANGUAGES)}")
        cfg.language = data["language"]
    requirements = sub_object("requirements", {"doc", "id_pattern"})
    if requirements:
        if "doc" not in requirements:
            raise ModelError(f"{CONFIG_FILE}: requirements needs 'doc'")
        cfg.requirements_doc = rel_path("requirements.doc", requirements["doc"])
        if "id_pattern" in requirements:
            try:
                re.compile(requirements["id_pattern"])
            except (re.error, TypeError) as exc:
                raise ModelError(f"{CONFIG_FILE}: requirements.id_pattern invalid: {exc}") from exc
            cfg.requirement_id = requirements["id_pattern"]
    if "check_rendered_docs" in data:
        if not isinstance(data["check_rendered_docs"], bool):
            raise ModelError(f"{CONFIG_FILE}: check_rendered_docs must be true/false")
        cfg.check_rendered_docs = data["check_rendered_docs"]
    if "feedback" in data:
        value = data["feedback"]
        # one line, no control characters: in `--format github` a newline would start a workflow
        # command (::error::) and a fake "RESULT: PASS" line could follow it
        if (not isinstance(value, str) or not value.strip() or len(value) > MAX_FEEDBACK
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ModelError(f"{CONFIG_FILE}: feedback must be one line of at most {MAX_FEEDBACK} "
                             "characters without control characters (how an agent reports a "
                             "crossweft problem)")
        cfg.feedback = value.strip()
    agent = sub_object("agent", {"stop", "harness", "command"})
    if "command" in agent and (not isinstance(agent["command"], str)
                               or not agent["command"].strip()):
        # read by harness.configured_command: how the agent hooks start crossweft
        raise ModelError(f"{CONFIG_FILE}: agent.command must be a non-empty string")
    if "stop" in agent:
        if not isinstance(agent["stop"], str) or agent["stop"] not in AGENT_STOP_MODES:
            raise ModelError(f"{CONFIG_FILE}: agent.stop must be one of {list(AGENT_STOP_MODES)}")
        cfg.agent_stop = agent["stop"]
    if "harness" in agent:
        if not isinstance(agent["harness"], bool):
            raise ModelError(f"{CONFIG_FILE}: agent.harness must be true/false")
        cfg.agent_harness = agent["harness"]
    validators = sub_object("validators", {"dir", "timeout", "timeouts", "exclude"})
    if "dir" in validators:
        cfg.validators_dir = rel_path("validators.dir", validators["dir"])
    if "timeout" in validators:
        timeout = validators["timeout"]
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise ModelError(f"{CONFIG_FILE}: validators.timeout must be a positive integer")
        cfg.validator_timeout = validators["timeout"]
    if "timeouts" in validators:
        timeouts = validators["timeouts"]
        if not isinstance(timeouts, dict) or not all(
                isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v > 0
                for k, v in timeouts.items()):
            raise ModelError(f"{CONFIG_FILE}: validators.timeouts must map file names to "
                             "positive integers")
        cfg.validator_timeouts = dict(timeouts)
    if "exclude" in validators:
        exclude = validators["exclude"]
        if not isinstance(exclude, dict) or not all(
                isinstance(k, str) and k and isinstance(v, str) and v.strip()
                for k, v in exclude.items()):
            raise ModelError(f"{CONFIG_FILE}: validators.exclude must map file names to the "
                             "reason they are not run (a non-empty string)")
        cfg.validator_exclude = dict(exclude)
    discover = sub_object("discover", {"exclude"})
    if "exclude" in discover:
        exclude = discover["exclude"]
        if not isinstance(exclude, list) or not all(isinstance(e, str) and e for e in exclude):
            raise ModelError(f"{CONFIG_FILE}: discover.exclude must be a list of globs")
        cfg.discover_exclude = list(exclude)
    joint_cfg = sub_object("joints", {"dir", "sources"})
    if joint_cfg:
        if "dir" not in joint_cfg:
            raise ModelError(f"{CONFIG_FILE}: joints needs 'dir'")
        cfg.joints_dir = rel_path("joints.dir", joint_cfg["dir"])
        if "sources" in joint_cfg:
            sources = joint_cfg["sources"]
            if not isinstance(sources, list) or not sources or not all(
                    isinstance(s, str) and s and not s.startswith(("/", "\\")) and ".." not in s.split("/")
                    for s in sources):
                raise ModelError(f"{CONFIG_FILE}: joints.sources must be a non-empty list of "
                                 "repository-relative globs")
            cfg.joints_sources = list(sources)
    hazard_cfg = sub_object("hazards", {"include", "exclude", "rules"})
    if hazard_cfg:
        def globs(key: str, required: bool) -> list:
            value = hazard_cfg.get(key, [])
            if (required and not value) or not isinstance(value, list) or not all(
                    isinstance(g, str) and g and not g.startswith(("/", "\\"))
                    and ".." not in g.split("/") for g in value):
                raise ModelError(f"{CONFIG_FILE}: hazards.{key} must be a "
                                 f"{'non-empty ' if required else ''}list of "
                                 "repository-relative globs")
            return list(value)
        cfg.hazards_include = globs("include", True)
        cfg.hazards_exclude = globs("exclude", False)
        rules = hazard_cfg.get("rules", sorted(hazards.RULES))
        if not isinstance(rules, list) or not rules or not all(
                isinstance(r, str) and r in hazards.RULES for r in rules):
            raise ModelError(f"{CONFIG_FILE}: hazards.rules must be a non-empty list of "
                             f"{sorted(hazards.RULES)}")
        cfg.hazards_rules = list(rules)
    fallback_cfg = sub_object("fallbacks", {"include", "exclude", "registry", "signers", "stops",
                                            "strict"})
    if fallback_cfg:
        def fglobs(key: str, required: bool) -> list:
            value = fallback_cfg.get(key, [])
            if (required and not value) or not isinstance(value, list) or not all(
                    isinstance(g, str) and g and not g.startswith(("/", "\\"))
                    and ".." not in g.split("/") for g in value):
                raise ModelError(f"{CONFIG_FILE}: fallbacks.{key} must be a "
                                 f"{'non-empty ' if required else ''}list of "
                                 "repository-relative globs")
            return list(value)
        cfg.fallbacks_include = fglobs("include", True)
        cfg.fallbacks_exclude = fglobs("exclude", False)
        if "registry" in fallback_cfg:
            cfg.fallbacks_registry = rel_path("fallbacks.registry", fallback_cfg["registry"])
        if "signers" in fallback_cfg:
            value = fallback_cfg["signers"]
            if not isinstance(value, str) or not value.strip():
                raise ModelError(f"{CONFIG_FILE}: fallbacks.signers must be a path")
            # outside the repository (absolute or ~) keeps the trusted keys out of an
            # agent's edits; a repository-relative path is allowed and reported
            expanded = Path(os.path.expanduser(value))
            cfg.fallbacks_signers = expanded if expanded.is_absolute() else \
                rel_path("fallbacks.signers", value)
        if "strict" in fallback_cfg:
            if not isinstance(fallback_cfg["strict"], bool):
                raise ModelError(f"{CONFIG_FILE}: fallbacks.strict must be true/false")
            cfg.fallbacks_strict = fallback_cfg["strict"]
        stops = fallback_cfg.get("stops", [])
        if not isinstance(stops, list) or not all(
                isinstance(s, str) and re.fullmatch(r"[A-Za-z_][\w.-]*", s) for s in stops):
            raise ModelError(f"{CONFIG_FILE}: fallbacks.stops must be a list of function names")
        cfg.fallbacks_stops = list(stops)
    return cfg


# --------------------------------------------------------------------------- #
# Model loading and schema validation
# --------------------------------------------------------------------------- #

class Model:
    """The merged registry. Entities keep their source file for messages."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.root = cfg.root
        self.model_dir = cfg.abs(cfg.model_dir)
        self.meta: dict = {}
        self.files: list[Path] = []
        self.entities: dict[str, list[dict]] = {key: [] for key in ENTITY_KEYS}
        self.by_id: dict[str, dict[str, dict]] = {key: {} for key in ENTITY_KEYS}
        self.origin: dict[int, str] = {}
        self.errors: list[str] = []
        # joint-kind plugins (crossweft/joints.py) and the entries of their sections
        self.plugins, self.plugin_errors = joints.load(
            cfg.root, cfg.joints_dir, RESERVED_JOINT_KINDS, set(GENERATED_FILES),
            cfg.joints_sources or None)
        self.sections: dict[str, list] = {plugin.kind: [] for plugin in self.plugins}

    # convenient accessors
    @property
    def blocks(self) -> dict[str, dict]:
        return self.by_id["blocks"]

    @property
    def links(self) -> dict[str, dict]:
        return self.by_id["links"]

    @property
    def flows(self) -> dict[str, dict]:
        return self.by_id["flows"]

    @property
    def data(self) -> dict[str, dict]:
        return self.by_id["data"]

    @property
    def findings(self) -> dict[str, dict]:
        return self.by_id["findings"]

    def where(self, entity: dict) -> str:
        return self.origin.get(id(entity), "?")

    def lanes(self) -> list[dict]:
        value = self.meta.get("lanes")
        return [lane for lane in value if isinstance(lane, dict)] if isinstance(value, list) else []

    def lane_ids(self) -> set[str]:
        return {lane["id"] for lane in self.lanes() if isinstance(lane.get("id"), str)}

    def ancestors(self, block_id: str) -> list[str]:
        chain, seen = [], set()
        current = block_id
        while current and current in self.blocks and current not in seen:
            chain.append(current)
            seen.add(current)
            current = self.blocks[current].get("parent")
        return chain

    def root_of(self, block_id: str) -> str:
        chain = self.ancestors(block_id)
        return chain[-1] if chain else block_id

    def children(self, block_id: str) -> list[str]:
        return [b["id"] for b in self.entities["blocks"] if b.get("parent") == block_id]

    def related(self, first: str, second: str) -> bool:
        """True when one block contains the other (or they are the same)."""
        return first in self.ancestors(second) or second in self.ancestors(first)


def load_model(cfg: Config) -> Model:
    model = Model(cfg)
    model_dir, root = model.model_dir, cfg.root
    if not model_dir.is_dir():
        raise ModelError(f"model directory missing: {cfg.model_dir.as_posix()} "
                         f"(model_dir in {CONFIG_FILE})")
    files = sorted(model_dir.glob("*.json"))
    if not files:
        raise ModelError(f"scanned nothing -- no model files (expected *.json under "
                         f"{cfg.model_dir.as_posix()})")
    model.files = files
    for path in files:
        if not inside(root, path):
            raise ModelError(f"{path} resolves outside the repository (symlink?)")
        rel = (cfg.model_dir / path.name).as_posix()
        try:
            payload = read_json(path)
        except (OSError, ValueError) as exc:
            raise ModelError(f"{rel}: unreadable or invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ModelError(f"{rel}: top level must be an object")
        unknown = set(payload) - TOP_LEVEL_KEYS - set(model.sections)
        if unknown:
            hint = ("" if cfg.joints_dir else " (a joint kind's section needs its plugin: "
                    f"joints.dir in {CONFIG_FILE})")
            model.errors.append(f"{rel}: unknown top-level key(s): {', '.join(sorted(unknown))}"
                                + hint)
        for kind, collected in model.sections.items():
            items = payload.get(kind, [])
            if isinstance(items, list):
                collected.extend(items)
            else:
                model.errors.append(f"{rel}: '{kind}' must be a list")
        if "meta" in payload:
            if model.meta:
                model.errors.append(f"{rel}: second 'meta' section (only one file may define meta)")
            elif isinstance(payload["meta"], dict):
                model.meta = payload["meta"]
            else:
                model.errors.append(f"{rel}: meta must be an object")
        for key in ENTITY_KEYS:
            items = payload.get(key, [])
            if not isinstance(items, list):
                model.errors.append(f"{rel}: '{key}' must be a list")
                continue
            for item in items:
                if not isinstance(item, dict):
                    model.errors.append(f"{rel}: {key} entry is not an object: {item!r}"[:200])
                    continue
                model.origin[id(item)] = rel
                model.entities[key].append(item)
    if not model.meta:
        model.errors.append("no 'meta' section found in any model file")
    for key in ENTITY_KEYS:
        for item in model.entities[key]:
            ident = item.get("id")
            if not isinstance(ident, str):
                model.errors.append(f"{model.where(item)}: {key} entry without string id")
                continue
            if ident in model.by_id[key]:
                model.errors.append(f"{model.where(item)}: duplicate {key} id '{ident}'")
                continue
            model.by_id[key][ident] = item
    return model


def _fields(kind: str) -> dict[str, tuple[bool, object]]:
    """Field specs: name -> (required, type). Types are interpreted by _check_value."""
    common_anchor = ("anchors",)
    specs: dict[str, dict[str, tuple[bool, object]]] = {
        "blocks": {
            "id": (True, "id"), "name": (True, "str"), "kind": (True, ("enum", KINDS)),
            "lane": (True, "lane"), "status": (True, ("enum", set(STATUSES))),
            "summary": (True, "str"), "short": (False, "str"), "parent": (False, "block"),
            "code": (False, "paths"), "anchors": (False, common_anchor[0]),
            "docs": (False, "paths"), "owner": (False, "str"), "runs": (False, "str"),
            "evidence": (False, ("enum", set(EVIDENCE))), "waits_for": (False, "waits"),
            "notes": (False, "str"), "route_server": (False, "bool"),
            "requirements": (False, "reqs"),
        },
        "links": {
            "id": (True, "id"), "from": (True, "block"), "to": (True, "block"),
            "transport": (True, ("enum", TRANSPORTS)), "status": (True, ("enum", set(STATUSES))),
            "summary": (True, "str"), "label": (False, "str"), "contract": (False, "contract"),
            "sync": (False, ("enum", SYNC)), "waits": (False, "str"), "on_error": (False, "str"),
            "carries": (False, "data-list"), "returns": (False, "data-list"),
            "identifiers": (False, "identifiers"),
            "from_anchors": (False, "anchors"), "to_anchors": (False, "anchors"),
            "evidence": (False, ("enum", set(EVIDENCE))), "notes": (False, "str"),
            "requirements": (False, "reqs"), "envs": (False, "str-list"),
            "gone": (False, "gone"),
        },
        "flows": {
            "id": (True, "id"), "name": (True, "str"), "status": (True, ("enum", set(STATUSES))),
            "summary": (True, "str"), "trigger": (False, "str"), "result": (False, "str"),
            "steps": (True, "steps"), "requirements": (False, "reqs"),
            "evidence": (False, ("enum", set(EVIDENCE))), "notes": (False, "str"),
        },
        "data": {
            "id": (True, "id"), "name": (True, "str"), "kind": (True, ("enum", DATA_KINDS)),
            "origin": (True, "block"), "summary": (True, "str"),
            "defined_in": (False, "anchors"), "stored_in": (False, "stored"),
            "consumers": (False, "block-list"), "also_from": (False, "also-from"),
            "anchors": (False, "anchors"), "sensitivity": (False, ("enum", SENSITIVITY)),
            "notes": (False, "str"),
        },
        "joins": {
            "id": (True, "id"), "name": (True, "str"), "points": (True, "points"),
            "link": (False, "link"), "notes": (False, "str"),
        },
        "sets": {
            "id": (True, "id"), "name": (True, "str"), "left": (True, "set-side"),
            "right": (True, "set-side"),
            "mode": (True, ("enum", {"equal", "left-subset", "right-subset"})),
            "link": (False, "link"), "allow": (False, "allow"), "notes": (False, "str"),
        },
        "pairs": {
            "id": (True, "id"), "name": (True, "str"), "regions": (True, "regions"),
            "link": (False, "link"), "notes": (False, "str"),
        },
        "ui_actions": {
            "id": (True, "id"), "name": (True, "str"), "controls": (True, "ui-side"),
            "hops": (True, "ui-hops"), "link": (False, "link"), "routes": (False, "bool"),
            "orphans": (False, "bool"), "allow": (False, "ui-allow"), "notes": (False, "str"),
        },
        "findings": {
            "id": (True, "finding-id"), "title": (True, "str"),
            "severity": (True, ("enum", set(SEVERITIES))),
            "status": (True, ("enum", set(FINDING_STATUSES))),
            "kind": (True, ("enum", FINDING_KINDS)), "summary": (True, "str"),
            "where": (False, "refs"), "detected_by": (False, "str-list"),
            "evidence": (False, "anchors"), "impact": (False, "str"),
            "next_step": (False, "str"), "owner": (False, "str"),
            "defer_reason": (False, ("enum", DEFER_REASONS)), "since": (False, "str"),
            "closed_by": (False, "str"), "requirements": (False, "reqs"),
            "notes": (False, "str"),
        },
    }
    return specs[kind]


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v for v in value)


def _path_list_errors(value: object, label: str) -> list[str]:
    if not _is_str_list(value):
        return [f"{label}: must be a list of repo-relative paths"]
    return [f"{label}: {problem}" for problem in (path_error(v) for v in value) if problem]


def _check_anchor_shape(anchor: object, label: str, errors: list[str]) -> None:
    if not isinstance(anchor, dict):
        errors.append(f"{label}: anchor must be an object")
        return
    unknown = set(anchor) - {"path", "find", "regex", "note"}
    if unknown:
        errors.append(f"{label}: anchor has unknown key(s) {sorted(unknown)}")
    problem = path_error(anchor.get("path"))
    if problem:
        errors.append(f"{label}: anchor path {problem}")
    if "find" in anchor and "regex" in anchor:
        errors.append(f"{label}: anchor may use 'find' or 'regex', not both")
    for key in ("find", "regex", "note"):
        if key in anchor and (not isinstance(anchor[key], str) or not anchor[key]):
            errors.append(f"{label}: anchor '{key}' must be a non-empty string")
    if isinstance(anchor.get("regex"), str):
        try:
            re.compile(anchor["regex"], re.MULTILINE)
        except re.error as exc:
            errors.append(f"{label}: invalid regex: {exc}")


def _check_value(model: Model, value: object, vtype: object, label: str) -> list[str]:
    errors: list[str] = []
    if isinstance(vtype, tuple) and vtype[0] == "enum":
        if not isinstance(value, str) or value not in vtype[1]:
            errors.append(f"{label}: '{value}' not one of {sorted(vtype[1])}")
        return errors
    if vtype == "str":
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{label}: must be a non-empty string")
    elif vtype == "bool":
        if not isinstance(value, bool):
            errors.append(f"{label}: must be true/false")
    elif vtype == "id":
        if not isinstance(value, str) or not ID_RE.match(value):
            errors.append(f"{label}: id '{value}' must be kebab-case [a-z0-9-]")
    elif vtype == "finding-id":
        if not isinstance(value, str) or not FINDING_ID_RE.match(value):
            errors.append(f"{label}: finding id '{value}' must look like SM-001")
    elif vtype == "lane":
        if not isinstance(value, str) or value not in model.lane_ids():
            errors.append(f"{label}: unknown lane '{value}'")
    elif vtype == "block":
        if not isinstance(value, str) or value not in model.blocks:
            errors.append(f"{label}: unknown block '{value}'")
    elif vtype == "link":
        if not isinstance(value, str) or value not in model.links:
            errors.append(f"{label}: unknown link '{value}'")
    elif vtype == "block-list":
        if not _is_str_list(value):
            errors.append(f"{label}: must be a list of block ids")
        else:
            errors += [f"{label}: unknown block '{v}'" for v in value if v not in model.blocks]
    elif vtype == "data-list":
        if not _is_str_list(value):
            errors.append(f"{label}: must be a list of data ids")
        else:
            errors += [f"{label}: unknown data '{v}'" for v in value if v not in model.data]
    elif vtype == "str-list":
        if not _is_str_list(value):
            errors.append(f"{label}: must be a list of non-empty strings")
    elif vtype == "paths":
        errors += _path_list_errors(value, label)
    elif vtype == "reqs":
        pattern = re.compile(model.cfg.requirement_id)
        if not _is_str_list(value) or not all(pattern.match(v) for v in value):
            errors.append(f"{label}: requirement ids must match {model.cfg.requirement_id} "
                          f"(requirements.id_pattern in {CONFIG_FILE})")
    elif vtype == "refs":
        if not _is_str_list(value):
            errors.append(f"{label}: must be a list of refs like block:x")
        else:
            errors += [f"{label}: bad ref '{v}'" for v in value if not _ref_exists(model, v)]
    elif vtype == "anchors":
        if not isinstance(value, list):
            errors.append(f"{label}: must be a list of anchors")
        else:
            for index, anchor in enumerate(value):
                _check_anchor_shape(anchor, f"{label}[{index}]", errors)
    elif vtype == "contract":
        if not isinstance(value, dict):
            errors.append(f"{label}: must be an object")
        else:
            unknown = set(value) - {"name", "defined_in", "enforcement", "note"}
            if unknown:
                errors.append(f"{label}: unknown key(s) {sorted(unknown)}")
            errors += _check_value(model, value.get("name"), "str", f"{label}.name")
            errors += _check_value(model, value.get("enforcement"), ("enum", ENFORCEMENT),
                                   f"{label}.enforcement")
            if "defined_in" in value:
                errors += _check_value(model, value["defined_in"], "anchors", f"{label}.defined_in")
            if "note" in value:
                errors += _check_value(model, value["note"], "str", f"{label}.note")
    elif vtype == "identifiers":
        if not isinstance(value, dict):
            errors.append(f"{label}: must be an object of kind -> list")
        else:
            for kind, entries in value.items():
                if kind not in IDENTIFIER_KINDS:
                    errors.append(f"{label}: unknown identifier kind '{kind}'")
                if not _is_str_list(entries):
                    errors.append(f"{label}.{kind}: must be a list of strings")
                    continue
                if kind == "route":
                    errors += [f"{label}.route: '{e}' must be 'METHOD /path'"
                               for e in entries if not ROUTE_DECL_RE.match(e)]
    elif vtype == "gone":
        if not isinstance(value, dict):
            errors.append(f"{label}: must be an object {{status, replaced_by?, note?}}")
        else:
            unknown = set(value) - {"status", "replaced_by", "note"}
            if unknown:
                errors.append(f"{label}: unknown key(s) {sorted(unknown)}")
            status = value.get("status")
            if isinstance(status, bool) or not isinstance(status, int) or not 400 <= status <= 599:
                errors.append(f"{label}.status: the HTTP status the retired route still answers "
                              "with (400-599, usually 410)")
            if "replaced_by" in value:
                errors += _check_value(model, value["replaced_by"], "link", f"{label}.replaced_by")
            if "note" in value:
                errors += _check_value(model, value["note"], "str", f"{label}.note")
    elif vtype == "waits":
        if not isinstance(value, list):
            errors.append(f"{label}: must be a list")
        else:
            for index, wait in enumerate(value):
                sub = f"{label}[{index}]"
                if not isinstance(wait, dict):
                    errors.append(f"{sub}: must be an object")
                    continue
                unknown = set(wait) - {"what", "on", "on_missing", "anchors"}
                if unknown:
                    errors.append(f"{sub}: unknown key(s) {sorted(unknown)}")
                errors += _check_value(model, wait.get("what"), "str", f"{sub}.what")
                if "on" in wait and not _ref_exists(model, wait["on"]):
                    errors.append(f"{sub}.on: bad ref '{wait['on']}'")
                if "on_missing" in wait:
                    errors += _check_value(model, wait["on_missing"], "str", f"{sub}.on_missing")
                if "anchors" in wait:
                    errors += _check_value(model, wait["anchors"], "anchors", f"{sub}.anchors")
    elif vtype == "steps":
        if not isinstance(value, list) or not value:
            errors.append(f"{label}: must be a non-empty list")
        else:
            for index, step in enumerate(value):
                sub = f"{label}[{index}]"
                if not isinstance(step, dict):
                    errors.append(f"{sub}: must be an object")
                    continue
                unknown = set(step) - {"link", "action", "reply", "waits", "then", "note"}
                if unknown:
                    errors.append(f"{sub}: unknown key(s) {sorted(unknown)}")
                errors += _check_value(model, step.get("link"), "link", f"{sub}.link")
                errors += _check_value(model, step.get("action"), "str", f"{sub}.action")
                if "reply" in step:
                    errors += _check_value(model, step["reply"], "bool", f"{sub}.reply")
                for key in ("waits", "then", "note"):
                    if key in step:
                        errors += _check_value(model, step[key], "str", f"{sub}.{key}")
    elif vtype == "stored":
        if not isinstance(value, list):
            errors.append(f"{label}: must be a list")
        else:
            for index, entry in enumerate(value):
                sub = f"{label}[{index}]"
                if not isinstance(entry, dict) or set(entry) - {"block", "where"}:
                    errors.append(f"{sub}: must be {{block, where}}")
                    continue
                errors += _check_value(model, entry.get("block"), "block", f"{sub}.block")
                errors += _check_value(model, entry.get("where"), "str", f"{sub}.where")
    elif vtype == "also-from":
        if not isinstance(value, list):
            errors.append(f"{label}: must be a list")
        else:
            for index, entry in enumerate(value):
                sub = f"{label}[{index}]"
                if not isinstance(entry, dict) or set(entry) - {"block", "reason"}:
                    errors.append(f"{sub}: must be {{block, reason}}")
                    continue
                errors += _check_value(model, entry.get("block"), "block", f"{sub}.block")
                errors += _check_value(model, entry.get("reason"), "str", f"{sub}.reason")
    elif vtype == "points":
        if not isinstance(value, list) or len(value) < 2:
            errors.append(f"{label}: a join needs at least two points")
        else:
            for index, point in enumerate(value):
                sub = f"{label}[{index}]"
                if not isinstance(point, dict):
                    errors.append(f"{sub}: must be an object")
                    continue
                unknown = set(point) - {"path", "regex", "side", "transform", "prefix",
                                        "suffix", "note", "within"}
                if unknown:
                    errors.append(f"{sub}: unknown key(s) {sorted(unknown)}")
                problem = path_error(point.get("path"))
                if problem:
                    errors.append(f"{sub}.path: {problem}")
                errors += _check_regex_groups(point.get("regex"), f"{sub}.regex", minimum=1)
                if "within" in point:
                    errors += _check_regex_one_group(point.get("within"), f"{sub}.within")
                for key in ("side", "prefix", "suffix", "note"):
                    if key in point and not isinstance(point[key], str):
                        errors.append(f"{sub}.{key}: must be a string")
                if "transform" in point:
                    if not _is_str_list(point["transform"]) or set(point["transform"]) - TRANSFORMS:
                        errors.append(f"{sub}.transform: allowed {sorted(TRANSFORMS)}")
    elif vtype == "set-side":
        if not isinstance(value, dict):
            errors.append(f"{label}: must be an object")
        else:
            unknown = set(value) - {"label", "paths", "regex", "exclude", "transform", "within",
                                    "files"}
            if unknown:
                errors.append(f"{label}: unknown key(s) {sorted(unknown)}")
            errors += _check_value(model, value.get("label"), "str", f"{label}.label")
            if ("files" in value) == ("paths" in value):
                errors.append(f"{label}: use either 'paths'+'regex' (contents) or 'files' (existing files)")
            if "files" in value:
                errors += _check_value(model, value.get("files"), "paths", f"{label}.files")
                if "regex" in value or "within" in value:
                    errors.append(f"{label}: 'files' sides take no regex/within")
            else:
                errors += _check_value(model, value.get("paths"), "paths", f"{label}.paths")
                errors += _check_regex_one_group(value.get("regex"), f"{label}.regex")
                if "within" in value:
                    errors += _check_regex_one_group(value.get("within"), f"{label}.within")
            if "exclude" in value:
                errors += _check_value(model, value["exclude"], "paths", f"{label}.exclude")
            if "transform" in value:
                if not _is_str_list(value["transform"]) or set(value["transform"]) - TRANSFORMS:
                    errors.append(f"{label}.transform: allowed {sorted(TRANSFORMS)}")
    elif vtype == "ui-side":
        errors += _ui_side_errors(model, value, label, controls=True)
    elif vtype == "ui-hops":
        if not isinstance(value, list) or not value:
            errors.append(f"{label}: must be a non-empty list of hops")
        else:
            for index, hop in enumerate(value):
                errors += _ui_side_errors(model, hop, f"{label}[{index}]", controls=False)
    elif vtype == "ui-allow":
        if not isinstance(value, list):
            errors.append(f"{label}: must be a list")
        else:
            for index, entry in enumerate(value):
                sub = f"{label}[{index}]"
                if not isinstance(entry, dict) or set(entry) - {"item", "reason"}:
                    errors.append(f"{sub}: must be {{item, reason}}")
                    continue
                errors += _check_value(model, entry.get("item"), "str", f"{sub}.item")
                errors += _check_value(model, entry.get("reason"), "str", f"{sub}.reason")
    elif vtype == "allow":
        if not isinstance(value, list):
            errors.append(f"{label}: must be a list")
        else:
            for index, entry in enumerate(value):
                sub = f"{label}[{index}]"
                if not isinstance(entry, dict) or set(entry) - {"item", "side", "reason"}:
                    errors.append(f"{sub}: must be {{item, side, reason}}")
                    continue
                errors += _check_value(model, entry.get("item"), "str", f"{sub}.item")
                errors += _check_value(model, entry.get("side"), ("enum", {"left", "right"}),
                                       f"{sub}.side")
                errors += _check_value(model, entry.get("reason"), "str", f"{sub}.reason")
    elif vtype == "regions":
        if not isinstance(value, list) or len(value) < 2:
            errors.append(f"{label}: a pair needs at least two regions")
        else:
            seen_paths: set[str] = set()
            for index, region in enumerate(value):
                sub = f"{label}[{index}]"
                if not isinstance(region, dict):
                    errors.append(f"{sub}: must be an object")
                    continue
                unknown = set(region) - {"path", "side", "whole_file", "note"}
                if unknown:
                    errors.append(f"{sub}: unknown key(s) {sorted(unknown)}")
                problem = path_error(region.get("path"))
                if problem:
                    errors.append(f"{sub}.path: {problem}")
                elif region["path"] in seen_paths:
                    errors.append(f"{sub}.path: '{region['path']}' appears twice -- one region "
                                  "per file (its markers carry the pair id)")
                else:
                    seen_paths.add(region["path"])
                errors += _check_value(model, region.get("side"), "str", f"{sub}.side")
                if "whole_file" in region and not isinstance(region["whole_file"], bool):
                    errors.append(f"{sub}.whole_file: must be true/false")
                if "note" in region:
                    errors += _check_value(model, region["note"], "str", f"{sub}.note")
    else:  # pragma: no cover - programming error in the spec table
        errors.append(f"{label}: internal error, unknown type {vtype!r}")
    return errors


def _check_regex_groups(value: object, label: str, minimum: int = 1,
                        maximum: int | None = None) -> list[str]:
    if not isinstance(value, str) or not value:
        return [f"{label}: must be a non-empty regex"]
    try:
        compiled = re.compile(value, re.MULTILINE)
    except re.error as exc:
        return [f"{label}: invalid regex: {exc}"]
    if compiled.groups < minimum or (maximum is not None and compiled.groups > maximum):
        wanted = f"exactly {minimum}" if maximum == minimum else f"at least {minimum}"
        return [f"{label}: regex must have {wanted} capture group(s) (has {compiled.groups})"]
    return []


def _check_regex_one_group(value: object, label: str) -> list[str]:
    return _check_regex_groups(value, label, minimum=1, maximum=1)


def _ui_side_errors(model: Model, value: object, label: str, controls: bool) -> list[str]:
    """A ui_action's controls or one of its hops, read like a set side. `regex`
    takes exact values (a hop may add a second group: the value the key leads
    to), `prefix_regex` takes static prefixes (dynamic controls, or a hop's
    startswith rules)."""
    if not isinstance(value, dict):
        return [f"{label}: must be an object"]
    unknown = set(value) - UI_SIDE_KEYS
    errors = [f"{label}: unknown key(s) {sorted(unknown)}"] if unknown else []
    errors += _check_value(model, value.get("label"), "str", f"{label}.label")
    errors += _check_value(model, value.get("paths"), "paths", f"{label}.paths")
    if "regex" not in value and "prefix_regex" not in value:
        errors.append(f"{label}: needs 'regex' (exact values) and/or 'prefix_regex' (prefixes)")
    if "regex" in value:
        problems = _check_regex_groups(value["regex"], f"{label}.regex", minimum=1,
                                       maximum=1 if controls else 2)
        if problems and not controls and "capture group" in problems[0]:
            problems = [f"{label}.regex: a hop regex has 1 group (a set of keys) or 2 (key and "
                        f"the value it leads to), not {re.compile(value['regex']).groups}"]
        errors += problems
    if "prefix_regex" in value:
        errors += _check_regex_one_group(value["prefix_regex"], f"{label}.prefix_regex")
    if "within" in value:
        errors += _check_regex_one_group(value["within"], f"{label}.within")
    if "exclude" in value:
        errors += _check_value(model, value["exclude"], "paths", f"{label}.exclude")
    return errors


def _ref_exists(model: Model, ref: object) -> bool:
    if not isinstance(ref, str):
        return False
    match = REF_RE.match(ref)
    if not match:
        return False
    kind, ident = match.groups()
    table = {"block": "blocks", "link": "links", "flow": "flows", "data": "data",
             "join": "joins", "set": "sets", "pair": "pairs", "finding": "findings"}[kind]
    return ident in model.by_id[table]


UI_SIDE_KEYS = {"label", "paths", "regex", "prefix_regex", "within", "exclude"}
META_KEYS = {"$comment", "schema", "title", "verified_on", "verified_against", "planes", "lanes",
             "route_scan", "coverage", "notes", "joint_kinds"}
ROUTE_SCAN_KEYS = {"routers", "router_search", "ignore_routers", "client_globs", "client_exclude",
                   "client_literal_regexes"}


def _named_list(value: object, label: str, required: tuple[str, ...]) -> list[str]:
    """A list of objects that each carry the given non-empty string keys."""
    if not isinstance(value, list):
        return [f"{label}: must be a list"]
    errors = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            errors.append(f"{label}[{index}]: must be an object")
            continue
        for key in required:
            if not isinstance(entry.get(key), str) or not entry.get(key):
                errors.append(f"{label}[{index}]: needs a non-empty '{key}'")
    return errors


def _validate_route_scan(scan: object) -> list[str]:
    if not isinstance(scan, dict):
        return ["meta.route_scan: must be an object"]
    errors = [f"meta.route_scan: unknown key '{k}'" for k in sorted(set(scan) - ROUTE_SCAN_KEYS)]
    routers = scan.get("routers", [])
    errors += _named_list(routers, "meta.route_scan.routers", ("path", "scanner"))
    for index, router in enumerate(routers if isinstance(routers, list) else []):
        if not isinstance(router, dict):
            continue
        label = f"meta.route_scan.routers[{index}]"
        extra = set(router) - {"path", "scanner", "function_prefixes", "pattern", "prefix", "note",
                               "receivers"}
        if extra:
            errors.append(f"{label}: unknown key(s) {sorted(extra)}")
        problem = path_error(router.get("path"))
        if problem:
            errors.append(f"{label}.path: {problem}")
        scanner = router.get("scanner")
        if not isinstance(scanner, str) or scanner not in ROUTE_SCANNERS:
            errors.append(f"{label}.scanner: '{scanner}' not one of {sorted(ROUTE_SCANNERS)}")
        if scanner == "regex":
            try:
                pattern = re.compile(router.get("pattern") or "")
            except (re.error, TypeError) as exc:
                errors.append(f"{label}.pattern: invalid regex: {exc}")
            else:
                if "path" not in pattern.groupindex:
                    errors.append(f"{label}.pattern: needs a named group (?P<path>...) and may "
                                  "have (?P<method>...)")
        elif "pattern" in router:
            errors.append(f"{label}: pattern only applies to the regex scanner")
        if "function_prefixes" in router and scanner != "go-chi":
            errors.append(f"{label}: function_prefixes only applies to the go-chi scanner")
        receivers = router.get("receivers", {})
        if "receivers" in router and scanner not in NATIVE_LANG:
            errors.append(f"{label}: receivers only applies to the "
                          f"{'/'.join(sorted(NATIVE_LANG))} scanners")
        if not isinstance(receivers, dict) or not all(
                v is None or isinstance(v, str) and (v == "" or v.startswith("/"))
                for v in receivers.values()):
            errors.append(f"{label}.receivers: must map receiver names to a path prefix "
                          "(\"\" or \"/...\") or null (not a router)")
        prefixes = router.get("function_prefixes", {})
        if not isinstance(prefixes, dict) or not all(isinstance(v, str) for v in prefixes.values()):
            errors.append(f"{label}.function_prefixes: must map function names to path prefixes")
        if "prefix" in router and (not isinstance(router["prefix"], str)
                                   or not router["prefix"].startswith("/")):
            errors.append(f"{label}.prefix: must be a path starting with '/'")
    for key in ("router_search", "client_globs", "client_exclude"):
        if key in scan:
            errors += _path_list_errors(scan[key], f"meta.route_scan.{key}")
    if "ignore_routers" in scan:
        errors += _named_list(scan["ignore_routers"], "meta.route_scan.ignore_routers",
                              ("path", "reason"))
    literal = scan.get("client_literal_regexes", [])
    if not _is_str_list(literal) and literal != []:
        errors.append("meta.route_scan.client_literal_regexes: must be a list of regexes")
    else:
        for index, expr in enumerate(literal):
            errors += _check_regex_one_group(expr, f"meta.route_scan.client_literal_regexes[{index}]")
    if scan.get("client_globs") and not literal:
        errors.append("meta.route_scan: client_globs without client_literal_regexes scans nothing")
    return errors


def _validate_coverage(coverage: object) -> list[str]:
    if not isinstance(coverage, dict):
        return ["meta.coverage: must be an object"]
    errors = [f"meta.coverage: unknown key '{k}'" for k in sorted(set(coverage) - {"roots", "ignore"})]
    errors += _path_list_errors(coverage.get("roots"), "meta.coverage.roots")
    if "ignore" in coverage:
        errors += _named_list(coverage["ignore"], "meta.coverage.ignore", ("path", "reason"))
    return errors


def validate_schema(model: Model) -> list[str]:
    """Every schema error in the model. An unexpected exception inside the
    validation itself is reported as an error too (with a request to report the
    bug): a malformed model must never turn into a traceback -- in an agent's
    stop hook that would let it finish unchecked."""
    try:
        return _validate_schema(model)
    except Exception as exc:  # noqa: BLE001 -- converted to a loud, non-passing result
        return [f"the model could not be validated ({type(exc).__name__}: {exc}) -- fix the "
                "model's structure; if it looks right, report this as a crossweft bug"]


def _validate_schema(model: Model) -> list[str]:
    errors = list(model.errors) + list(model.plugin_errors)
    for plugin in model.plugins:
        if model.sections.get(plugin.kind):
            errors += joints.validate(plugin, model.sections[plugin.kind])
    meta = model.meta
    if meta:
        errors += [f"meta: unknown key '{k}'" for k in sorted(set(meta) - META_KEYS)]
        if meta.get("schema") != SCHEMA_ID:
            errors.append(f"meta.schema must be '{SCHEMA_ID}'")
        if not isinstance(meta.get("title"), str) or not meta.get("title"):
            errors.append("meta.title must be a non-empty string")
        if "verified_on" in meta and not DATE_RE.match(str(meta["verified_on"])):
            errors.append("meta.verified_on must be a date YYYY-MM-DD")
        if "verified_against" in meta and not SHA_RE.match(str(meta["verified_against"])):
            errors.append("meta.verified_against must be a git SHA (7-40 lowercase hex)")
        planes = meta.get("planes")
        plane_ids: set[str] = set()
        if planes is not None:
            if not isinstance(planes, list) or not planes:
                errors.append("meta.planes must be a non-empty list when present")
            else:
                errors += _named_list(planes, "meta.planes", ("id", "name"))
                plane_ids = {p["id"] for p in planes
                             if isinstance(p, dict) and isinstance(p.get("id"), str)}
        lanes = meta.get("lanes")
        if not isinstance(lanes, list) or not lanes:
            errors.append("meta.lanes must be a non-empty list")
        else:
            seen: set[str] = set()
            for index, lane in enumerate(lanes):
                if not isinstance(lane, dict) or not ID_RE.match(str(lane.get("id", ""))):
                    errors.append(f"meta.lanes[{index}]: needs kebab-case id")
                    continue
                if lane["id"] in seen:
                    errors.append(f"meta.lanes[{index}]: duplicate lane '{lane['id']}'")
                seen.add(lane["id"])
                extra = set(lane) - {"id", "name", "plane"}
                if extra:
                    errors.append(f"meta.lanes[{index}]: unknown key(s) {sorted(extra)}")
                if not isinstance(lane.get("name"), str) or not lane.get("name"):
                    errors.append(f"meta.lanes[{index}]: needs a name")
                if plane_ids and (not isinstance(lane.get("plane"), str)
                                  or lane["plane"] not in plane_ids):
                    errors.append(f"meta.lanes[{index}]: plane must be one of {sorted(plane_ids)}")
                if not plane_ids and "plane" in lane:
                    errors.append(f"meta.lanes[{index}]: 'plane' needs meta.planes to declare it")
        if "route_scan" in meta:
            errors += _validate_route_scan(meta["route_scan"])
        if "coverage" in meta:
            errors += _validate_coverage(meta["coverage"])
    uses_requirements = any(item.get("requirements") for kind in ("blocks", "links", "flows",
                                                                  "findings")
                            for item in model.entities[kind])
    if uses_requirements and model.cfg.requirements_doc is None:
        errors.append(f"entities reference requirements but {CONFIG_FILE} declares no "
                      "requirements.doc to check them against")
    for kind in ENTITY_KEYS:
        spec = _fields(kind)
        for item in model.entities[kind]:
            label = f"{model.where(item)}: {kind}:{item.get('id', '?')}"
            for name in sorted(set(item) - set(spec)):
                errors.append(f"{label}: unknown field '{name}'")
            for name, (required, vtype) in spec.items():
                if name not in item:
                    if required:
                        errors.append(f"{label}: missing required field '{name}'")
                    continue
                errors += _check_value(model, item[name], vtype, f"{label}.{name}")
    # structural rules beyond field types
    for block in model.entities["blocks"]:
        ident = block.get("id")
        if block.get("parent") == ident:
            errors.append(f"{model.where(block)}: block '{ident}' is its own parent")
        chain, seen, current = [], set(), block.get("parent")
        while isinstance(current, str) and current in model.blocks:
            if current in seen or current == ident:
                errors.append(f"{model.where(block)}: parent cycle at block '{ident}'")
                break
            seen.add(current)
            chain.append(current)
            current = model.blocks[current].get("parent")
    for link in model.entities["links"]:
        if link.get("from") == link.get("to"):
            errors.append(f"{model.where(link)}: link '{link.get('id')}' connects a block to itself")
        if isinstance(link.get("gone"), dict):
            label = f"{model.where(link)}: link '{link.get('id')}' gone"
            if link.get("status") != "legacy":
                errors.append(f"{label}: only a legacy link can be gone (it describes a retired "
                              "route the server still answers)")
            routes = (link.get("identifiers") or {}).get("route")
            if not routes:
                errors.append(f"{label}: needs identifiers.route -- the retired routes")
            for route in routes if isinstance(routes, list) else []:
                # a glob or a mount would also cover live routes nobody maps
                if isinstance(route, str) and ("*" in route
                                               or route.startswith(("MOUNT ", "ANY "))):
                    errors.append(f"{label}: '{route}' -- a gone route names one exact route "
                                  "with one method: no glob, no ANY, no MOUNT")
            if not link.get("to_anchors"):
                errors.append(f"{label}: needs to_anchors -- where the server answers them")
            successor = model.links.get(link["gone"].get("replaced_by") or "")
            if successor is not None and successor.get("status") != "current":
                errors.append(f"{label}.replaced_by: '{successor['id']}' is {successor.get('status')}, "
                              "not current")
    for finding in model.entities["findings"]:
        label = f"{model.where(finding)}: finding {finding.get('id')}"
        if finding.get("status") in ("open", "accepted"):
            for name in ("next_step", "owner"):
                if not finding.get(name):
                    errors.append(f"{label}: non-closed finding needs '{name}'")
        if finding.get("status") == "accepted" and not finding.get("defer_reason"):
            errors.append(f"{label}: accepted finding needs defer_reason "
                          f"({', '.join(sorted(DEFER_REASONS))})")
        if finding.get("status") == "closed" and not finding.get("closed_by"):
            errors.append(f"{label}: closed finding needs 'closed_by' (commit or evidence)")
        if not finding.get("detected_by") and not finding.get("evidence"):
            errors.append(f"{label}: needs detected_by (check keys or 'manual') or evidence")
    for entry in model.entities["ui_actions"]:
        sides = [entry.get("controls")] + list(entry.get("hops") or [])
        names = [side.get("label") for side in sides if isinstance(side, dict)]
        for name in sorted({n for n in names if isinstance(n, str) and names.count(n) > 1}):
            errors.append(f"{model.where(entry)}: ui_action {entry.get('id')}: label '{name}' "
                          "names two sides -- problem keys carry the label, so the controls "
                          "and each hop need their own")
    for join in model.entities["joins"]:
        points = join.get("points")
        for index, point in enumerate(points if isinstance(points, list) else []):
            if isinstance(point, dict) and "side" not in point:
                errors.append(f"{model.where(join)}: join {join.get('id')} point {index} needs 'side'")
            elif isinstance(point, dict):
                errors += _join_side_errors(model, point, f"{model.where(join)}: join "
                                            f"{join.get('id')} point {index}.side")
    return errors


def _join_side_errors(model: Model, point: dict, label: str) -> list[str]:
    """`side` is the label a point is reported under; which end of a link a
    file is on is decided by its path (code, anchors), never by this label.
    Free labels stay valid (measured 2026-10-07: 638 of 644 points in a real
    261-join map are labels such as "server (Go)"). Only a label that IS a
    block id makes a claim, and it is checked: the block must hold the file
    (it, a part of it, or a block containing it owns the file's code)."""
    side = point["side"]
    if not isinstance(side, str):
        return []   # the type error is reported with the other point fields
    if not side.strip():
        return [f"{label}: must be a non-empty string"]
    path = point.get("path")
    if side not in model.blocks or path_error(path):
        return []
    owners = code_owners(model, path)
    if not owners or any(model.related(side, owner) for owner in owners):
        return []
    return [f"{label}: '{side}' is a block id, but {path} is code of {', '.join(owners)} -- "
            f"name a block that holds the file ({', '.join(owners)}, a part of it or a block "
            f"containing it), or a plain label that is not a block id"]


# --------------------------------------------------------------------------- #
# Checks against the code
# --------------------------------------------------------------------------- #

class Problem:
    """One detected inconsistency. `key` is what findings refer to."""

    __slots__ = ("key", "category", "message", "fatal", "finding", "path", "line")

    def __init__(self, key: str, category: str, message: str, fatal: bool = False,
                 path: str | None = None, line: int | None = None) -> None:
        self.key = key
        self.category = category
        self.message = message
        self.fatal = fatal          # fatal problems cannot be excused by a finding
        self.finding: str | None = None
        self.path = path            # where to look (for annotations and agents)
        self.line = line

    def as_dict(self) -> dict:
        row = {"key": self.key, "category": self.category, "message": self.message}
        if self.finding:
            row["finding"] = self.finding
        if self.path:
            row["path"] = self.path
        if self.line:
            row["line"] = self.line
        return row


# --------------------------------------------------------------------------- #
# Pairs: fingerprinted regions and the attestation lock
# --------------------------------------------------------------------------- #

LOCK_SCHEMA = "crossweft.lock.v1"


def extract_region(text: str, pair_id: str, whole_file: bool = False
                   ) -> tuple[str, int, str | None]:
    """(normalised content, first line, error) of the region a pair marks in a
    file: the lines strictly between a line containing ``crossweft:begin <id>``
    and one containing ``crossweft:end <id>`` (any comment syntax). Exactly one
    of each is required -- a second begin marker would make "the region"
    ambiguous. Normalisation ignores trailing whitespace and line endings only."""
    if whole_file:
        return normalise(text), 1, None
    ident = re.escape(pair_id)
    begins = list(re.finditer(re.escape(PAIR_BEGIN) + r"\s+" + ident + r"(?![\w-])", text))
    ends = list(re.finditer(re.escape(PAIR_END) + r"\s+" + ident + r"(?![\w-])", text))
    if len(begins) != 1 or len(ends) != 1:
        return "", 0, (f"expected exactly one '{PAIR_BEGIN} {pair_id}' and one '{PAIR_END} "
                       f"{pair_id}' marker, found {len(begins)} and {len(ends)}")
    begin, end_marker = begins[0], ends[0]
    if end_marker.start() < begin.end():
        return "", 0, f"'{PAIR_END} {pair_id}' must come after '{PAIR_BEGIN} {pair_id}'"
    start = text.find("\n", begin.end())
    if start < 0 or start >= end_marker.start():
        return "", 0, (f"put '{PAIR_BEGIN} {pair_id}' and '{PAIR_END} {pair_id}' on separate "
                       "lines, with the region between them")
    end = text.rfind("\n", 0, end_marker.start())
    body = normalise(text[start + 1:end + 1] if end > start else "")
    if not body.strip():
        # an empty region fingerprints nothing: attested once, it would pass forever
        return "", 0, "the region between the markers is empty -- it would guard nothing"
    return body, text.count("\n", 0, start) + 2, None


def normalise(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip("\n")


def fingerprint(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# Hex digits of the digest that ends a join/pair problem key. It only has to
# tell apart successive disagreements of ONE guard, so 32 bits is ample, and
# the key stays short enough to copy into a finding.
KEY_DIGEST_LENGTH = 8


def key_digest(parts: list[tuple[str, ...]]) -> str:
    """A short id of one specific disagreement: the distinct (where, what)
    parts -- (file, pattern, value) of each join point, (file, fingerprint) of
    each pair region. Tying every value to its place means the same values
    moving to other files is a new disagreement, not the recorded one; line
    numbers are left out, so moving code does not change the key. A finding
    that names the key excuses that disagreement only: when it changes, the new
    problem fails and the old finding goes stale. Line endings are normalised,
    so a CRLF checkout and an LF checkout produce the same key."""
    rows = sorted({tuple(field.replace("\r\n", "\n").replace("\r", "\n") for field in part)
                   for part in parts})
    text = json.dumps(rows, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:KEY_DIGEST_LENGTH]


def read_lock(cfg: Config) -> dict:
    """The attestation lock. A missing file is an empty lock (nothing attested
    yet -- every pair then reports 'unattested', loud); a malformed one is an
    error."""
    path = cfg.abs(cfg.lock_file)
    if not path.is_file():
        return {"schema": LOCK_SCHEMA, "pairs": {}}
    try:
        data = read_json(path)
    except (OSError, ValueError) as exc:
        raise ModelError(f"{cfg.lock_file.as_posix()} is unreadable: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != LOCK_SCHEMA \
            or not isinstance(data.get("pairs"), dict):
        raise ModelError(f"{cfg.lock_file.as_posix()} is not a {LOCK_SCHEMA} lock file")
    for ident, entry in data["pairs"].items():
        if (not isinstance(entry, dict) or not isinstance(entry.get("regions"), dict)
                or not isinstance(entry.get("reason"), str) or not entry.get("reason").strip()
                or not DATE_RE.match(str(entry.get("attested_on", "")))):
            raise ModelError(f"{cfg.lock_file.as_posix()}: entry '{ident}' needs regions, a "
                             "non-empty reason and attested_on (YYYY-MM-DD)")
    return data


def safe_target(cfg: Config, path: Path) -> None:
    """Refuse to write `path` unless it stays inside the repository: no symlinked
    file, no directory on the way that resolves elsewhere."""
    if path.is_symlink():
        raise ModelError(f"refusing to write {path}: it is a symlink")
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if not inside(cfg.root, existing):
        raise ModelError(f"refusing to write {path}: it resolves outside the repository")


def write_lock(cfg: Config, data: dict) -> None:
    path = cfg.abs(cfg.lock_file)
    safe_target(cfg, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_bytes(text.encode("utf-8"))


class RouteIndex:
    """Routes (METHOD, path, tag) by normalised path. Each path is normalised
    once, when it is added; matching a route is a dict lookup plus a scan of the
    few entries that hold a '*' glob -- not a pass over every route (2,000
    routes took half a minute that way)."""

    def __init__(self, routes: list[tuple[str, str, object]]) -> None:
        self.exact: dict[str, list[tuple[str, object]]] = {}
        self.globs: list[tuple[str, str, object]] = []
        for method, path, tag in routes:
            norm = Checker._norm_route_path(path)
            self.exact.setdefault(norm, []).append((method, tag))
            if "*" in norm:
                self.globs.append((method, norm, tag))
        self.paths = sorted(self.exact)

    def below(self, prefix: str) -> list[tuple[str, object]]:
        """Entries whose normalised path starts with `prefix`."""
        found: list[tuple[str, object]] = []
        index = bisect.bisect_left(self.paths, prefix)
        while index < len(self.paths) and self.paths[index].startswith(prefix):
            found += self.exact[self.paths[index]]
            index += 1
        return found

    def covering(self, method: str, path: str) -> list[object]:
        """Tags of the declared routes that map a registered METHOD path: the
        same path (or a matching glob) for that method or ANY. A mount is
        covered by any declared route below it, including a glob such as
        "ANY /admin*"."""
        norm = Checker._norm_route_path(path)
        if method == "MOUNT":
            below = norm.rstrip("/") + "/"
            return ([tag for _, tag in self.below(below)]
                    + [tag for _, glob, tag in self.globs if fnmatch.fnmatchcase(below, glob)])
        return ([tag for m, tag in self.exact.get(norm, []) if m in (method, "ANY")]
                + [tag for m, glob, tag in self.globs
                   if m in (method, "ANY") and fnmatch.fnmatchcase(norm, glob)])

    def explains(self, literal: str) -> bool:
        """A client literal is explained by a declared route with its path (any
        method), below it when the literal ends in '/', or by a matching glob."""
        lit = Checker._norm_route_path(literal)
        return (lit in self.exact or (lit.endswith("/") and bool(self.below(lit)))
                or any(fnmatch.fnmatchcase(lit, glob) for _, glob, _ in self.globs))

    def declares(self, path: str) -> bool:
        """A request path is declared by a route with that path (any method) or a
        matching glob. Unlike `explains`, a path ending in '/' is one request, not
        every route below it: `fetch("/v1/" + kind)` must not pass as `/v1/`."""
        norm = Checker._norm_route_path(path)
        return (norm in self.exact
                or any(fnmatch.fnmatchcase(norm, glob) for _, glob, _ in self.globs))

    def serves(self, method: str, path: str) -> bool:
        """On an index of REGISTERED routes: a declared METHOD path is served
        when it is registered with that method, or either side is ANY; a
        declared glob matches registered paths."""
        norm = Checker._norm_route_path(path)

        def same_method(registered: str) -> bool:
            return method in (registered, "ANY") or registered == "ANY"

        if any(same_method(m) for m, _ in self.exact.get(norm, [])):
            return True
        # simplification: a declared glob scans every registered route; declared
        # globs are rare, and an index by prefix would pay off only if they were not
        return "*" in norm and any(same_method(m) and fnmatch.fnmatchcase(registered, norm)
                                   for registered, entries in self.exact.items()
                                   for m, _ in entries)


class Checker:
    def __init__(self, model: Model) -> None:
        self.model = model
        self.root = model.root
        self.problems: list[Problem] = []
        self.info: list[str] = []
        self._text_cache: dict[str, str | None] = {}
        self._newlines: dict[str, list[int]] = {}
        self._listings: dict[Path, set[str]] = {}
        self._link_names: dict[Path, frozenset[str] | None] = {}
        self._spelling_checked: set[str] = set()
        self.files_scanned: set[str] = set()
        self.items_checked = 0
        # `check --changed`: the changed paths (None = the full check). A guard
        # none of whose files is in scope is skipped, and the problem keys it
        # could have raised are kept, so a finding it detects is never called
        # stale for want of a run (see reconcile_findings).
        self.scope: list[str] | None = None
        self.guards_total = 0
        self.guards_run = 0
        self.skipped_keys: set[str] = set()
        self.skipped_prefixes: list[str] = []
        self.unverified: list[str] = []
        self.route_index: RouteIndex | None = None
        self.ui_traces: dict[str, list[ui_actions.Trace]] = {}

    # ------------------------------------------------------------- change scope
    def in_scope(self, targets: list[str]) -> bool:
        return self.scope is None or any(_scope_hit(changed, target)
                                         for target in targets for changed in self.scope)

    def guard(self, targets: list[str], keys: tuple[str, ...] = (),
              prefixes: tuple[str, ...] = ()) -> bool:
        """Count one guard; True when it must run. A skipped guard records the
        exact keys and key prefixes of the problems it raises."""
        self.guards_total += 1
        if self.in_scope(targets):
            self.guards_run += 1
            return True
        self.skipped_keys.update(keys)
        self.skipped_prefixes.extend(prefixes)
        return False

    def evaluated(self, key: str) -> bool:
        """Whether the detector of problem `key` ran in this check."""
        if self.scope is None:
            return True
        if key in self.skipped_keys or key.startswith(tuple(self.skipped_prefixes)):
            return False
        kind, _, rel = key.partition(":")
        if kind in ("escape", "path-case"):
            # raised by whichever guard reads the file: only known when it was read
            return rel in self._spelling_checked or rel in self._text_cache \
                or self.in_scope([rel])
        return True

    # ------------------------------------------------------------------ utils
    def text(self, rel: str) -> str | None:
        """File contents, or None when the file is missing or resolves outside
        the repository (a symlink pointing out of the tree is refused, loud)."""
        if rel not in self._text_cache:
            path = self.root / rel
            if path.exists() and not self.within(path):
                self.add(f"escape:{rel}", "anchors",
                         f"{rel} resolves outside the repository (symlink?) -- refused", fatal=True)
                self._text_cache[rel] = None
                return None
            try:
                self._text_cache[rel] = path.read_text(encoding="utf-8", errors="replace")
                self.files_scanned.add(rel)
                self.check_spelling(rel)
            except OSError:
                self._text_cache[rel] = None
        return self._text_cache[rel]

    def within(self, path: Path) -> bool:
        """inside(self.root, path) without a resolve() per file. resolve() asks
        the OS for the final path of every file (31 ms a call on a Windows
        machine with on-access scanning: 138 s of a 230 s check on a 40-service
        repo). A path below the root can only leave it through a symlink or
        junction on the way, so each directory is listed once and its link
        entries noted; a path that crosses one -- or that cannot be walked
        plainly -- takes the full resolve() as before. Names are compared
        casefolded: a case-insensitive file system may open `Link/a.ts` for a
        link named `link`, so a case difference errs towards resolving."""
        try:
            parts = path.relative_to(self.root).parts
        except ValueError:
            return inside(self.root, path)
        here = self.root
        for part in parts:
            if part in ("..", "."):
                return inside(self.root, path)
            if here not in self._link_names:
                self._link_names[here] = self._links_in(here)
            links = self._link_names[here]
            if links is None or part.casefold() in links:
                return inside(self.root, path)
            here = here / part
        return True

    @staticmethod
    def _links_in(directory: Path) -> frozenset[str] | None:
        """Casefolded names of the symlinks and junctions in `directory` (None
        when it cannot be listed). os.scandir reports both from the listing
        itself: d_type on POSIX, the find data's reparse attribute on Windows."""
        names = set()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_symlink() or (os.name == "nt" and getattr(
                            entry.stat(follow_symlinks=False), "st_file_attributes", 0)
                            & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                        names.add(entry.name.casefold())
        except OSError:
            return None
        return frozenset(names)

    def check_spelling(self, rel: str) -> None:
        """An EXISTING model path must be spelled as the file system names it. A
        case-insensitive file system opens `Web/API.ts` for `web/api.ts`, but
        git, `impact` and the agent hooks compare paths as written and would
        never connect a change to it."""
        rel = rel.rstrip("/")
        if rel in self._spelling_checked:
            return
        self._spelling_checked.add(rel)
        here, actual = self.root, []
        for part in rel.split("/"):
            if here not in self._listings:
                try:
                    self._listings[here] = set(os.listdir(here))
                except OSError:
                    return
            names = self._listings[here]
            if part not in names:
                folded = [name for name in names if name.casefold() == part.casefold()]
                if len(folded) != 1:
                    return          # not a case difference (missing: reported elsewhere)
                part = folded[0]
            actual.append(part)
            here = here / part
        spelled = "/".join(actual)
        if spelled != rel:
            self.add(f"path-case:{rel}", "anchors",
                     f"{rel} is named '{spelled}' on disk -- write the path exactly as the file "
                     "is named (git, impact and the agent hooks compare paths as written)",
                     fatal=True, path=spelled)

    def add(self, key: str, category: str, message: str, fatal: bool = False,
            path: str | None = None, line: int | None = None) -> None:
        self.problems.append(Problem(key, category, message, fatal, path, line))

    def line_at(self, rel: str, pos: int) -> int:
        """1-based line of offset `pos` in file `rel` (newline index cached per
        file: counting newlines per match is quadratic on big files)."""
        index = self._newlines.get(rel)
        if index is None:
            index = [m.start() for m in re.finditer("\n", self.text(rel) or "")]
            self._newlines[rel] = index
        return bisect.bisect_left(index, pos) + 1

    def expand(self, patterns: list[str], exclude: list[str] | None = None) -> list[str]:
        return self.expand_entries(patterns, exclude)[0]

    def expand_entries(self, patterns: list[str], exclude: list[str] | None = None
                       ) -> tuple[list[str], list[str]]:
        """(files, entries that yield no file). An entry naming an existing file
        is that file, even when it contains glob characters (`web/[id]/page.ts`
        is a Next.js route directory, not a character class); any other entry
        with `*?[` is a glob."""
        excluded = [p.replace("**", "*") for p in (exclude or [])]
        found: set[str] = set()
        empty: list[str] = []
        for pattern in patterns:
            if (self.root / pattern).is_file():
                matches = {pattern}
                self.check_spelling(pattern)
            elif any(ch in pattern for ch in "*?["):
                matches = {path.relative_to(self.root).as_posix() for path in self.root.glob(pattern)
                           if path.is_file() and self.within(path)}
            else:
                matches = set()
            kept = {f for f in matches if not any(fnmatch.fnmatch(f, ex) for ex in excluded)}
            if not kept:
                empty.append(pattern)
            found |= kept
        return sorted(found), empty

    # ---------------------------------------------------------------- anchors
    def check_anchor(self, anchor: dict, owner: str) -> None:
        rel = anchor.get("path", "")
        if not self.guard([rel], keys=(f"anchor:{owner}:{rel}",)):
            return
        self.items_checked += 1
        path = self.root / rel
        if rel.endswith("/"):
            if not path.is_dir():
                self.add(f"anchor:{owner}:{rel}", "anchors",
                         f"{owner}: directory missing: {rel}", fatal=True, path=rel)
            else:
                self.check_spelling(rel)
            return
        if not path.is_file():
            self.add(f"anchor:{owner}:{rel}", "anchors", f"{owner}: file missing: {rel}",
                     fatal=True, path=rel)
            return
        text = self.text(rel)
        if text is None:
            return
        if "find" in anchor and anchor["find"] not in text:
            self.add(f"anchor:{owner}:{rel}", "anchors",
                     f"{owner}: literal not found in {rel}: {anchor['find']!r}", fatal=True,
                     path=rel)
        if "regex" in anchor and not re.search(anchor["regex"], text, re.MULTILINE):
            self.add(f"anchor:{owner}:{rel}", "anchors",
                     f"{owner}: regex not matched in {rel}: {anchor['regex']!r}", fatal=True,
                     path=rel)

    def check_paths(self, paths: list[str], owner: str, what: str) -> None:
        for rel in paths:
            if not self.guard([rel], keys=(f"path:{owner}:{rel}",)):
                continue
            self.items_checked += 1
            if not (self.root / rel.rstrip("/")).exists():
                self.add(f"path:{owner}:{rel}", "anchors", f"{owner}: {what} path missing: {rel}",
                         fatal=True, path=rel.rstrip("/"))
            else:
                self.check_spelling(rel)

    def check_all_anchors(self) -> None:
        m = self.model
        for block in m.entities["blocks"]:
            owner = f"block:{block['id']}"
            self.check_paths(block.get("code", []), owner, "code")
            self.check_paths(block.get("docs", []), owner, "docs")
            for anchor in block.get("anchors", []):
                self.check_anchor(anchor, owner)
            for index, wait in enumerate(block.get("waits_for", [])):
                for anchor in wait.get("anchors", []):
                    self.check_anchor(anchor, f"{owner}.waits_for[{index}]")
        for link in m.entities["links"]:
            owner = f"link:{link['id']}"
            for key in ("from_anchors", "to_anchors"):
                for anchor in link.get(key, []):
                    self.check_anchor(anchor, f"{owner}.{key}")
            for anchor in link.get("contract", {}).get("defined_in", []):
                self.check_anchor(anchor, f"{owner}.contract")
        for item in m.entities["data"]:
            owner = f"data:{item['id']}"
            for key in ("defined_in", "anchors"):
                for anchor in item.get(key, []):
                    self.check_anchor(anchor, f"{owner}.{key}")
        for finding in m.entities["findings"]:
            if finding.get("status") == "closed":
                continue
            for anchor in finding.get("evidence", []):
                self.check_anchor(anchor, f"finding:{finding['id']}")

    # ------------------------------------------------------------ joins/sets
    @staticmethod
    def _transform(value: str, transforms: list[str]) -> str:
        for name in transforms:
            if name == "unescape-c":
                value = re.sub(r"\\(.)", r"\1", value)
            elif name == "lower":
                value = value.lower()
            elif name == "strip":
                value = value.strip()
            elif name == "csv-words":
                value = ",".join(re.findall(r"[A-Za-z0-9_.\-]+", value))
            elif name == "basename":
                value = re.split(r"[\\/]+", value.rstrip("/\\"))[-1]
            elif name == "go-http-status":
                value = str(GO_HTTP_STATUS.get(value, value))
        return value

    def check_joins(self) -> None:
        """Every point's regex is applied to its file; EVERY match counts. A
        value re-declared further down one side (a copy, a second constant)
        must agree too -- comparing only the first match would let a second,
        drifted definition hide behind the first one."""
        for join in self.model.entities["joins"]:
            if not self.guard([point["path"] for point in join["points"]],
                              prefixes=(f"join:{join['id']}:", f"anchor:join:{join['id']}:")):
                continue
            values: list[tuple[str, str, str, int]] = []   # (side@path, value, path, line)
            placed: list[tuple[str, str, str]] = []        # (path, regex, value) for the key
            broken = False
            for point in join["points"]:
                self.items_checked += 1
                rel = point["path"]
                text = self.text(rel)
                if text is None:
                    self.add(f"anchor:join:{join['id']}:{rel}", "anchors",
                             f"join:{join['id']}: file missing: {rel}", fatal=True, path=rel)
                    broken = True
                    continue
                bounds = [(0, len(text))]
                if "within" in point:
                    # the point reads only these regions (a default block, one struct); every
                    # match inside them still counts, so a drifted copy inside cannot hide.
                    # The regex runs on the whole file from the region start (not on a slice,
                    # and with no end cut), so ^, $, \b, lookbehind and lookahead see the real
                    # text; only matches lying wholly inside a region count.
                    bounds = [m.span(1) for m in re.finditer(point["within"], text, re.MULTILINE)
                              if m.group(1) is not None]
                    if not bounds:
                        self.add(f"anchor:join:{join['id']}:within:{rel}", "anchors",
                                 f"join:{join['id']}: the {point['side']} point's 'within' regex "
                                 f"found no region in {rel} (no match, or its group did not take "
                                 f"part) -- within: {point['within']}", fatal=True, path=rel)
                        broken = True
                        continue
                pattern = re.compile(point["regex"], re.MULTILINE)
                matches = []
                for start, end in bounds:
                    pos = start
                    while pos < end:
                        match = pattern.search(text, pos)
                        if match is None or match.start() >= end:
                            break
                        if match.end() <= end:
                            matches.append(match)
                            pos = match.end() if match.end() > match.start() else match.start() + 1
                        else:
                            pos = match.start() + 1   # it ran past the region: a shorter match may start inside
                if not matches:
                    # reported under joins; the key keeps its "anchor:" prefix because
                    # findings in users' repositories refer to it
                    self.add(f"anchor:join:{join['id']}:{rel}", "joins",
                             f"join:{join['id']}: the {point['side']} point's regex matched "
                             f"nothing in {rel} -- regex: {point['regex']}",
                             fatal=True, path=rel)
                    broken = True
                    continue
                for match in matches:
                    raw = ",".join(group or "" for group in match.groups())
                    value = self._transform(raw, point.get("transform", []))
                    value = point.get("prefix", "") + value + point.get("suffix", "")
                    line = self.line_at(rel, match.start())
                    values.append((f"{point['side']}@{rel}:{line}", value, rel, line))
                    placed.append((rel, point["regex"], value))
            if broken:
                continue
            distinct = {value for _, value, _, _ in values}
            if len(distinct) > 1:
                detail = "; ".join(f"{side}={value!r}" for side, value, _, _ in values)
                # point the reader at the first value that differs from the first one
                first = values[0][1]
                odd = next(v for v in values if v[1] != first)
                self.add(f"join:{join['id']}:{key_digest(placed)}", "joins",
                         f"join:{join['id']} disagrees: {detail}", path=odd[2], line=odd[3])

    def _set_files(self, side: dict, owner: str, key: str) -> list[str]:
        """The files one set side reads. Every entry must yield a file: a listed
        file that is gone, or a glob that matches nothing, would otherwise just
        drop out while the other entries keep the side non-empty."""
        if not side[key]:
            self.add(f"anchor:{owner}:files", "anchors",
                     f"{owner}: side '{side['label']}' lists no {key}", fatal=True)
            return []
        files, empty = self.expand_entries(side[key], side.get("exclude"))
        for entry in empty:
            if (self.root / entry).is_file():
                what, path = "is dropped by 'exclude'", entry
            elif any(ch in entry for ch in "*?["):
                what = "matches no file" + (" left by 'exclude'" if side.get("exclude") else "")
                path = None
            else:
                what, path = "is missing (or not a file)", entry
            self.add(f"anchor:{owner}:{entry}", "anchors",
                     f"{owner}: side '{side['label']}': '{entry}' {what}", fatal=True, path=path)
        return files

    def _extract_set(self, side: dict, owner: str) -> tuple[dict[str, tuple[str, int]], int]:
        """Members of one side of a set, each with the first place it was seen."""
        items: dict[str, tuple[str, int]] = {}
        if "files" in side:
            files = self._set_files(side, owner, "files")
            for rel in files:
                item = self._transform(rel, side.get("transform", []))
                if item in items:
                    # two files, one member: the set could not notice either one going
                    first = items[item][0]
                    self.add(f"{owner}:collision:{item}", "sets",
                             f"{owner}: side '{side['label']}' maps both {first} and {rel} to "
                             f"'{item}' -- the set cannot tell them apart, so either could "
                             "disappear unnoticed; narrow the glob or split the set", path=rel)
                    continue
                items[item] = (rel, 0)
            return items, len(files)
        files = self._set_files(side, owner, "paths")
        for rel in files:
            text = self.text(rel) or ""
            regions = [(0, text)]
            if "within" in side:
                # a match whose group did not take part (the other branch of an
                # alternation) is no region at all
                regions = [(m.start(1), m.group(1))
                           for m in re.finditer(side["within"], text, re.MULTILINE)
                           if m.group(1) is not None]
                if not regions:
                    self.add(f"anchor:{owner}:within:{rel}", "anchors",
                             f"{owner}: 'within' regex not matched in {rel}: {side['within']!r}",
                             fatal=True, path=rel)
            for offset, region in regions:
                for match in re.finditer(side["regex"], region, re.MULTILINE):
                    if match.group(1) is None:
                        continue       # the group did not take part: no member here
                    item = self._transform(match.group(1), side.get("transform", []))
                    if item not in items:
                        items[item] = (rel, self.line_at(rel, offset + match.start()))
        if files and not items:
            self.add(f"anchor:{owner}:items", "anchors",
                     f"{owner}: side '{side['label']}' extracted nothing (regex/paths wrong?)",
                     fatal=True, path=files[0])
        return items, len(files)

    def check_sets(self) -> None:
        for entry in self.model.entities["sets"]:
            owner = f"set:{entry['id']}"
            # exclude is ignored here: reading a set too often is safe, too rarely is not
            targets = [t for key in ("left", "right")
                       for t in entry[key].get("files") or entry[key].get("paths") or []]
            if not self.guard(targets, prefixes=(f"{owner}:", f"anchor:{owner}:")):
                continue
            left_at, _ = self._extract_set(entry["left"], owner)
            right_at, _ = self._extract_set(entry["right"], owner)
            left, right = set(left_at), set(right_at)
            self.items_checked += len(left) + len(right)
            if not left or not right:
                continue
            left_only = left - right if entry["mode"] in ("equal", "left-subset") else set()
            right_only = right - left if entry["mode"] in ("equal", "right-subset") else set()
            allowed = {(a["side"], a["item"]) for a in entry.get("allow", [])}
            for item in sorted(left_only):
                if ("left", item) not in allowed:
                    rel, line = left_at[item]
                    self.add(f"{owner}:left-only:{item}", "sets",
                             f"{owner}: '{item}' only in {entry['left']['label']}",
                             path=rel, line=line or None)
            for item in sorted(right_only):
                if ("right", item) not in allowed:
                    rel, line = right_at[item]
                    self.add(f"{owner}:right-only:{item}", "sets",
                             f"{owner}: '{item}' only in {entry['right']['label']}",
                             path=rel, line=line or None)
            for side, item in sorted(allowed):
                still = item in (left_only if side == "left" else right_only)
                if not still:
                    self.add(f"{owner}:stale-allow:{side}:{item}", "sets",
                             f"{owner}: allow entry ({side}) '{item}' no longer needed -- remove it",
                             fatal=True)

    # ------------------------------------------------------------- ui actions
    def _ui_extract(self, side: dict, owner: str, controls: bool = False
                    ) -> tuple[dict[str, list[tuple[str | None, ui_actions.Place]]],
                               dict[str, ui_actions.Place]]:
        """Exact values (each with the value its second group leads to, if any)
        and static prefixes of one side, each with where it was seen. An empty
        prefix is kept on controls (a fully computed value, which nothing can
        prove handled) and dropped on hops (a rule that would cover everything)."""
        keys: dict[str, list[tuple[str | None, ui_actions.Place]]] = {}
        prefixes: dict[str, ui_actions.Place] = {}
        found = {"regex": 0, "prefix_regex": 0}
        files = self._set_files(side, owner, "paths")
        patterns = [(name, re.compile(side[name], re.MULTILINE))
                    for name in ("regex", "prefix_regex") if name in side]
        for rel in files:
            text = self.text(rel) or ""
            regions = [(0, text)]
            if "within" in side:
                regions = [(m.start(1), m.group(1))
                           for m in re.finditer(side["within"], text, re.MULTILINE)
                           if m.group(1) is not None]
                if not regions:
                    self.add(f"anchor:{owner}:within:{rel}", "anchors",
                             f"{owner}: 'within' regex not matched in {rel}: {side['within']!r}",
                             fatal=True, path=rel)
            for offset, region in regions:
                for name, pattern in patterns:
                    for match in pattern.finditer(region):
                        item = match.group(1)
                        if item is None or (not item and not (controls and name == "prefix_regex")):
                            continue
                        found[name] += 1
                        place = ui_actions.Place(rel, self.line_at(rel, offset + match.start()))
                        if name == "prefix_regex":
                            prefixes.setdefault(item, place)
                        else:
                            value = match.group(2) if pattern.groups >= 2 else None
                            keys.setdefault(item, []).append((value, place))
        for name, _ in patterns:
            # each pattern on its own: a misspelt regex must not hide behind a
            # prefix_regex that still matches
            if files and not found[name]:
                self.add(f"anchor:{owner}:items:{side['label']}:{name}", "anchors",
                         f"{owner}: '{side['label']}' {name} extracted nothing "
                         "(regex/paths wrong?)", fatal=True, path=files[0])
        return keys, prefixes

    def check_ui_actions(self) -> None:
        """Every control reaches its handler through each hop (ui_actions.py)."""
        entries = self.model.entities["ui_actions"]
        if not entries:
            return
        index = self.route_index or RouteIndex(
            [d for d in self.declared_routes() if d[2]["status"] == "current"])
        for entry in entries:
            owner = f"ui:{entry['id']}"
            sides = [entry["controls"], *entry["hops"]]
            targets = [t for side in sides for t in side.get("paths", [])]
            if not self.guard(targets, prefixes=(f"{owner}:", f"anchor:{owner}:")):
                continue
            exact, dynamic = self._ui_extract(entry["controls"], owner, controls=True)
            controls = {value: places[0][1] for value, places in exact.items()}
            hops = []
            for side in entry["hops"]:
                keys, rules = self._ui_extract(side, owner)
                maps = "regex" in side and re.compile(side["regex"]).groups >= 2
                hop = ui_actions.Hop(side["label"], keys, rules, maps)
                hops.append(hop)
                for key, bound in ui_actions.conflicts(hop):
                    shown = ", ".join(f"{value!r} ({place.rel}:{place.line})"
                                      for value, place in bound[:3])
                    self.add(f"{owner}:conflict:{side['label']}:{key}", "ui",
                             f"{owner}: {side['label']} binds {key!r} more than once ({shown}) "
                             "-- the later binding silently wins", path=bound[-1][1].rel,
                             line=bound[-1][1].line)
            if not controls and not dynamic:
                continue   # reported by _ui_extract: a chain with no control proves nothing
            # a template literal's `${...}` is a path parameter: `/v1/orders/${id}`
            # must match `/v1/orders/{id}`, and `/v1/${kind}` matches no route at all;
            # a UI request is one path, so the client-literal "below '/'" rule does not apply
            explains = index.declares if entry.get("routes") else None
            traces = ui_actions.resolve(controls, dynamic, hops, explains,
                                        as_route=lambda value: value.split("?", 1)[0]
                                        .replace("${", "{"))
            self.ui_traces[entry["id"]] = traces
            self.items_checked += len(traces) + sum(len(h.keys) + len(h.rules) for h in hops)
            allowed = {a["item"] for a in entry.get("allow", [])}
            used: set[str] = set()
            for trace in traces:
                if trace.status == "ok":
                    continue
                item = trace.control + ("*" if trace.dynamic else "")
                if item in allowed:
                    used.add(item)
                    trace.status = "allowed"
                    continue
                self.add(f"{owner}:{trace.status}:{item}", "ui",
                         f"{owner}: control {item!r} ({trace.place.rel}:{trace.place.line}): "
                         f"{trace.detail}", path=trace.place.rel, line=trace.place.line)
            if entry.get("orphans") and hops:
                first = hops[0]
                for key in ui_actions.orphans(first, controls, dynamic):
                    if key in allowed:
                        used.add(key)
                        continue
                    place = first.keys[key][0][1] if key in first.keys else first.rules[key]
                    self.add(f"{owner}:orphan:{key}", "ui",
                             f"{owner}: {first.label} handles {key!r} but no control in "
                             f"'{entry['controls']['label']}' sends it any more -- remove the "
                             "handler or restore the control", path=place.rel, line=place.line)
            for item in sorted(allowed - used):
                self.add(f"{owner}:stale-allow:{item}", "ui",
                         f"{owner}: allow entry '{item}' no longer needed -- remove it", fatal=True)

    # ------------------------------------------------------------------ pairs
    def pair_state(self, pair: dict) -> tuple[dict[str, str], dict[str, int], bool]:
        """Fingerprints of every region of a pair: ({path: sha256}, {path: first
        line}, ok). Missing files and missing/duplicated markers are fatal
        problems -- a pair that cannot find its regions compares nothing."""
        hashes: dict[str, str] = {}
        lines: dict[str, int] = {}
        ok = True
        for region in pair["regions"]:
            self.items_checked += 1
            rel = region["path"]
            text = self.text(rel)
            if text is None:
                self.add(f"anchor:pair:{pair['id']}:{rel}", "anchors",
                         f"pair:{pair['id']}: file missing: {rel}", fatal=True, path=rel)
                ok = False
                continue
            content, line, error = extract_region(text, pair["id"], region.get("whole_file", False))
            if error:
                self.add(f"anchor:pair:{pair['id']}:{rel}", "anchors",
                         f"pair:{pair['id']}: {rel}: {error}", fatal=True, path=rel)
                ok = False
                continue
            hashes[rel] = fingerprint(content)
            lines[rel] = line
        return hashes, lines, ok

    def check_pairs(self) -> None:
        pairs = self.model.entities["pairs"]
        try:
            lock = read_lock(self.model.cfg)
        except ModelError as exc:
            self.add("pair:lock", "pairs", str(exc), fatal=True,
                     path=self.model.cfg.lock_file.as_posix())
            return
        attested = lock["pairs"]
        for pair in pairs:
            # the lock is never out of scope: a changed lock runs the full check
            ident = pair["id"]
            if not self.guard([region["path"] for region in pair["regions"]],
                              keys=(f"pair:unattested:{ident}",),
                              prefixes=(f"pair:changed:{ident}:", f"anchor:pair:{ident}:")):
                continue
            hashes, lines, ok = self.pair_state(pair)
            if not ok:
                continue
            ident = pair["id"]
            entry = attested.get(ident)
            paths = [region["path"] for region in pair["regions"]]
            if entry is None:
                self.add(f"pair:unattested:{ident}", "pairs",
                         f"pair:{ident} ({', '.join(paths)}) has never been attested -- read every "
                         f"region, make them agree, then run `crossweft attest {ident} --reason "
                         f"\"...\"`", path=paths[0], line=lines.get(paths[0]))
                continue
            before = entry["regions"]
            changed = sorted(p for p in paths if before.get(p) != hashes[p])
            gone = sorted(set(before) - set(paths))
            if changed or gone:
                unchanged = [p for p in paths if p not in changed]
                detail = f"changed since it was attested on {entry['attested_on']}: " + ", ".join(
                    changed + [f"{g} (no longer a region)" for g in gone])
                other = (f"re-read {', '.join(unchanged)} and bring it in line"
                         if unchanged else "every region changed -- re-read them together")
                state = key_digest([(path, hashes[path]) for path in paths])
                self.add(f"pair:changed:{ident}:{state}", "pairs",
                         f"pair:{ident} {detail}; {other}, then run `crossweft attest {ident} "
                         f"--reason \"...\"`", path=(changed or paths)[0],
                         line=lines.get((changed or paths)[0]))
        for ident in sorted(set(attested) - {pair["id"] for pair in pairs}):
            self.add(f"pair:stale-lock:{ident}", "pairs",
                     f"{self.model.cfg.lock_file.as_posix()} attests pair '{ident}', which the model "
                     f"no longer declares -- run `crossweft attest --prune`", fatal=True,
                     path=self.model.cfg.lock_file.as_posix())

    # ----------------------------------------------------------------- routes
    @staticmethod
    def go_scan(text: str) -> tuple[dict[int, int], list[tuple[int, int]]]:
        """Go lexical pass: map each code '{' to its '}', and list the spans of
        comments and string/rune literals (half-open [start, end))."""
        pairs: dict[int, int] = {}
        spans: list[tuple[int, int]] = []
        stack: list[int] = []
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            start = i
            if ch == "/" and text.startswith("//", i):
                end = text.find("\n", i)
                i = n if end < 0 else end
                spans.append((start, i))
                continue
            if ch == "/" and text.startswith("/*", i):
                end = text.find("*/", i + 2)
                i = n if end < 0 else end + 2
                spans.append((start, i))
                continue
            if ch in "\"'":
                i += 1
                while i < n and text[i] != ch and text[i] != "\n":
                    i += 2 if text[i] == "\\" else 1
                i += 1
                spans.append((start, i))
                continue
            if ch == "`":
                end = text.find("`", i + 1)
                i = n if end < 0 else end + 1
                spans.append((start, i))
                continue
            if ch == "{":
                stack.append(i)
            elif ch == "}" and stack:
                pairs[stack.pop()] = i
            i += 1
        return pairs, spans

    @staticmethod
    def go_code_braces(text: str) -> dict[int, int]:
        return Checker.go_scan(text)[0]

    @staticmethod
    def go_router_vars(text: str) -> set[str]:
        """Names of the variables in a Go file that hold a chi router: assigned
        from chi.NewRouter(), typed chi.Router / *chi.Mux (parameters of helper
        functions and of Route/Group closures), or derived with .With/.Group/.Route."""
        names = {a or b for a, b in GO_ROUTER_VAR_RE.findall(text)}
        derived = GO_ROUTER_DERIVED_RE.findall(text)
        grown = True
        while grown:
            grown = False
            for name, base in derived:
                if base in names and name not in names:
                    names.add(name)
                    grown = True
        return names

    @classmethod
    def route_registrations(cls, text: str, function_prefixes: dict[str, str],
                            unscannable=None) -> list[tuple[str, str, int, int]]:
        """(METHOD, full path, line, offset of the call) for every chi registration
        in one Go file's text -- the one go-chi route enumerator: `check` uses it,
        and a joint-kind plugin that needs the server's routes calls it too instead
        of keeping a second scanner. A registration whose path (or method) is not a
        literal cannot be named; it is passed to `unscannable(verb, expr, offset,
        what)` when given, because silently skipping it would hide a route."""
        newlines = [m.start() for m in re.finditer("\n", text)]
        pairs, spans = cls.go_scan(text)
        opens = sorted(pairs)
        span_starts = [start for start, _ in spans]
        scopes: list[tuple[int, int, str]] = []

        def first_brace_after(pos: int) -> int | None:
            index = bisect.bisect_left(opens, pos)
            return opens[index] if index < len(opens) else None

        def in_code(pos: int) -> bool:
            index = bisect.bisect_right(span_starts, pos) - 1
            return index < 0 or pos >= spans[index][1]

        def cannot_name(verb: str, expr: str, pos: int, what: str) -> None:
            if unscannable is not None:
                unscannable(verb, expr, pos, what)

        for match in GO_ROUTE_SCOPE_RE.finditer(text):
            if not in_code(match.start()):
                continue
            expr, end = _go_arg(text, match.end())
            body = GO_FUNC_LITERAL_RE.match(text, end + 1) if text[end:end + 1] == "," else None
            if body is None:
                continue   # Route(pattern, fn) with a named function: see function_prefixes
            prefix = _go_literal(expr)
            if prefix is None:
                cannot_name("Route", expr, match.start(), "the prefix is not a string literal")
                continue
            brace = first_brace_after(body.end())
            if brace is not None:
                scopes.append((brace, pairs[brace], prefix))
        for match in GO_FUNC_RE.finditer(text):
            prefix = function_prefixes.get(match.group(1))
            if prefix is None or not in_code(match.start()):
                continue
            # skip the parameter list, then the body is the next code brace
            depth, pos = 1, match.end()
            while pos < len(text) and depth:
                depth += {"(": 1, ")": -1}.get(text[pos], 0)
                pos += 1
            brace = first_brace_after(pos)
            if brace is not None:
                scopes.append((brace, pairs[brace], prefix))
        scopes.sort()
        routers = cls.go_router_vars(text)
        # calls made on a router variable or field (r.Get, s.router.With(mw).Post),
        # by the end of the match -- the same end GO_ROUTE_CALL_RE reports for that call
        on_router = {m.end() for m in GO_ROUTER_CALL_RE.finditer(text) if m.group(1) in routers}
        path_names = set(GO_PATH_NAME_RE.findall(text))

        def looks_like_route(expr: str, end: int) -> bool:
            """On a receiver not known to be a router: a handler follows an
            argument that holds a path (a "/..." literal inside it, or a name
            this file sets to one). `r.Header.Get(name)` has no handler;
            `cache.Get(ctx, key)` has no path."""
            return text[end:end + 1] == "," and (
                bool(re.search(r"[\"`]/", expr)) or expr.split(".")[-1].strip() in path_names)

        routes: list[tuple[str, str, int, int]] = []
        for match in GO_ROUTE_CALL_RE.finditer(text):
            pos = match.start()
            if not in_code(pos):
                continue
            verb, args = match.group(1), match.end()
            router = match.end() in on_router
            method = {"Handle": "ANY", "HandleFunc": "ANY", "Mount": "MOUNT"}.get(verb, verb.upper())
            if verb in ("Method", "MethodFunc"):
                expr, end = _go_arg(text, args)
                if text[end:end + 1] != ",":
                    continue           # not (method, pattern, handler)
                constant = GO_HTTP_METHOD_RE.fullmatch(expr)
                literal = _go_literal(expr)
                if constant is None and literal is None:
                    if router:
                        cannot_name(verb, expr, pos, "the method is neither an http.Method "
                                                     "constant nor a string literal")
                    continue
                method = (constant.group(1) if constant else literal).upper()
                args = end + 1
            expr, end = _go_arg(text, args)
            path = _go_literal(expr)
            if path is None:
                if expr and (router or looks_like_route(expr, end)):
                    cannot_name(verb, expr, pos, "the path is not a string literal")
                continue
            if not path.startswith("/"):
                continue               # a header or a map key, not a route pattern
            prefix = "".join(p for start, end, p in scopes if start < pos < end)
            routes.append((method, prefix + path, bisect.bisect_left(newlines, pos) + 1, pos))
        return routes

    def go_routes(self, rel: str, function_prefixes: dict[str, str]) -> list[tuple[str, str, int]]:
        """(METHOD, full path, line) for chi registrations in one Go file. A
        registration on a router variable whose path (or method) is not a
        literal is reported as `route:unscannable:...`: the scanner cannot say
        which route it is, and silently skipping it would hide an unmapped route."""
        text = self.text(rel)
        if text is None:
            self.add(f"anchor:route-scan:{rel}", "anchors", f"route router file missing: {rel}",
                     fatal=True)
            return []

        def unscannable(verb: str, expr: str, pos: int, what: str) -> None:
            expr = " ".join(expr.split())
            line = self.line_at(rel, pos)
            self.add(f"route:unscannable:{rel}:{verb} {expr}", "routes",
                     f"{rel}:{line}: {verb}({expr}, ...) -- {what}, so the go-chi scanner cannot "
                     "tell which route this is; write it as a literal, or record a finding with "
                     "this key", path=rel, line=line)

        return [(method, path, line) for method, path, line, _ in
                self.route_registrations(text, function_prefixes, unscannable)]

    def regex_routes(self, rel: str, pattern: str, prefix: str) -> list[tuple[str, str, int]]:
        """(METHOD, full path, line) for a server whose registrations one regex
        can find (Express `app.get("/x")`, FastAPI `@app.post("/x")`, ...). The
        pattern names a `path` group and optionally a `method` group; without
        one the route counts as ANY. No scope nesting: put a shared prefix in
        the router's `prefix`."""
        text = self.text(rel)
        if text is None:
            self.add(f"anchor:route-scan:{rel}", "anchors", f"route router file missing: {rel}",
                     fatal=True, path=rel)
            return []
        routes = []
        for match in re.finditer(pattern, text, re.MULTILINE):
            if match.group("path") is None:
                continue               # the path group did not take part: not a registration
            method = (match.groupdict().get("method") or "ANY").upper()
            if method not in ROUTE_METHODS:
                self.add(f"route:bad-method:{rel}:{method}", "routes",
                         f"{rel}: route pattern captured method '{method}', not one of "
                         f"{list(ROUTE_METHODS)}", fatal=True, path=rel,
                         line=self.line_at(rel, match.start()))
                continue
            routes.append((method, prefix + match.group("path"), self.line_at(rel, match.start())))
        return routes

    def native_routes(self, rel: str, scanner: str, receivers: dict[str, str | None]
                      ) -> list[tuple[str, str, int]]:
        """(METHOD, full path, line) for one file read by a native scanner
        (express, fastapi, flask, gin, echo; see _NativeRoutes). A file that
        yields no route and reports nothing it could not read is an error:
        zero scanned is never a pass."""
        text = self.text(rel)
        if text is None:
            self.add(f"anchor:route-scan:{rel}", "anchors", f"route router file missing: {rel}",
                     fatal=True, path=rel)
            return []
        scan = _NativeRoutes(self, rel, text, scanner, receivers)
        routes = scan.run()
        if not routes and not scan.problems:
            self.add(f"route:scan-empty:{rel}", "routes",
                     f"{rel}: the {scanner} scanner found no route registration -- wrong scanner, "
                     "or the routes live in another file (scan that one); nothing here was checked",
                     fatal=True, path=rel)
        return routes

    @staticmethod
    def _norm_route_path(path: str) -> str:
        """Parameters in any common spelling -- {id}, :id, <int:id> -- compare equal."""
        path = path.split("?", 1)[0]
        for pattern in ROUTE_PARAM_RES:
            path = pattern.sub("{}", path)
        return path

    def declared_routes(self) -> list[tuple[str, str, dict]]:
        declared = []
        for link in self.model.entities["links"]:
            for entry in link.get("identifiers", {}).get("route", []):
                method, path = entry.split(" ", 1)
                declared.append((method, path, link))
        return declared

    def check_routes(self) -> None:
        scan = self.model.meta.get("route_scan")
        if not scan:
            return
        declared = self.declared_routes()
        # only current links describe what runs today: a planned link may name a
        # route nobody serves yet, and a legacy link must not "map" a live route
        current = [d for d in declared if d[2]["status"] == "current"]
        gone = [d for d in declared if d[2]["status"] == "legacy" and d[2].get("gone")]
        elsewhere = [d for d in declared if d[2]["status"] != "current" and not d[2].get("gone")]
        mapped = RouteIndex(current)
        self.route_index = mapped
        retired = RouteIndex(gone)
        # Two guards, because the two halves read different files. Under
        # `check --changed` the model is unchanged -- a changed model file, and
        # with it meta.route_scan and every link's routes, makes change_scope
        # fall back to the full check -- so each half depends only on its own
        # files, and a half none of whose files changed gives today exactly the
        # verdicts it gave at REV:
        #  - the server half (unmapped, unserved, unscanned routers) compares ALL
        #    registrations with the links, so it scans EVERY router as soon as
        #    any router file (or a router_search / ignore_routers path) changed:
        #    a route removed from a changed router is "unserved" unless an
        #    unchanged router still registers it, which only a scan of that
        #    router can tell;
        #  - the client half judges each literal against the declared routes
        #    alone (`mapped`), never against the registrations, so it reads only
        #    the client files that changed. A router that stops serving a route
        #    an unchanged client still calls is caught by the server half as
        #    route:unserved (the link still declares it; a link that drops it
        #    is a model change, hence a full check).
        self.check_route_servers(scan, current, elsewhere, mapped, gone, retired)
        self.check_route_clients(scan, mapped, retired)

    def check_route_servers(self, scan: dict, current: list, elsewhere: list,
                            mapped: RouteIndex, gone: list, retired: RouteIndex) -> None:
        routers = scan.get("routers", [])
        feeds = ([router["path"] for router in routers] + scan.get("router_search", [])
                 + [entry["path"] for entry in scan.get("ignore_routers", [])])
        if not self.guard(feeds, keys=("anchor:route-scan:empty", "route:no-server-block")
                          + tuple(f"anchor:route-scan:{router['path']}" for router in routers),
                          prefixes=("route:unmapped:", "route:unserved:", "route:unscannable:",
                                    "route:gone-unserved:",
                                    "route:bad-method:", "route:scan-empty:",
                                    "route:unscanned-router:", "route:stale-ignore:")):
            return
        registered: list[tuple[str, str, str, int]] = []
        router_files = []
        for router in scan.get("routers", []):
            rel = router["path"]
            router_files.append(rel)
            if router["scanner"] == "go-chi":
                found = self.go_routes(rel, router.get("function_prefixes", {}))
            elif router["scanner"] == "regex":
                found = self.regex_routes(rel, router["pattern"], router.get("prefix", ""))
            else:
                found = self.native_routes(rel, router["scanner"], router.get("receivers", {}))
            if router["scanner"] != "regex" and router.get("prefix"):
                found = [(m, router["prefix"] + p, line) for m, p, line in found]
            for method, path, line in found:
                registered.append((method, path, rel, line))
        self.items_checked += len(registered)
        if scan.get("routers") and not registered:
            self.add("anchor:route-scan:empty", "anchors",
                     "route scan found no registrations -- misconfigured", fatal=True)
        # every router in the server must be scanned or explicitly ignored
        ignored = {entry["path"] for entry in scan.get("ignore_routers", [])}
        for rel in self.expand(scan.get("router_search", []), ["**/*_test.go"]):
            if rel in router_files or rel in ignored:
                continue
            text = self.text(rel) or ""
            kind = next((k for k, expr in ROUTER_FILE_RES.items() if expr.search(text)), None)
            if kind:
                self.add(f"route:unscanned-router:{rel}", "routes",
                         f"{rel} builds a {'chi' if kind == 'go-chi' else kind} router that is "
                         "neither scanned nor ignored", fatal=True, path=rel)
        for entry in scan.get("ignore_routers", []):
            if not (self.root / entry["path"]).is_file():
                self.add(f"route:stale-ignore:{entry['path']}", "routes",
                         f"ignore_routers entry {entry['path']} no longer exists -- remove it",
                         fatal=True)
        unmapped_elsewhere = RouteIndex(elsewhere)
        for method, path, rel, line in registered:
            if not mapped.covering(method, path) and not retired.covering(method, path):
                other = sorted({link["id"] for link in unmapped_elsewhere.covering(method, path)})
                detail = (f"only non-current link(s) declare it: {', '.join(other)}"
                          if other else "no mapped link declares it")
                self.add(f"route:unmapped:{method} {path}", "routes",
                         f"server registers {method} {path} ({rel}:{line}) but {detail}",
                         path=rel, line=line)
        served = RouteIndex([(m, p, None) for m, p, _, _ in registered])
        servers = {b["id"] for b in self.model.entities["blocks"] if b.get("route_server")}
        if registered and not servers:
            self.add("route:no-server-block", "routes",
                     "route_scan found registrations but no block sets \"route_server\": true -- "
                     "declared routes cannot be checked against what the server serves", fatal=True)
        for d_method, d_path, link in current:
            target_chain = set(self.model.ancestors(link["to"]))
            if not (target_chain & servers) or d_method == "MOUNT":
                continue
            if not served.serves(d_method, d_path):
                self.add(f"route:unserved:{d_method} {d_path}", "routes",
                         f"link:{link['id']} says the server serves {d_method} {d_path}, "
                         f"but no registration exists")
        for d_method, d_path, link in gone:
            if not (set(self.model.ancestors(link["to"])) & servers) or d_method == "MOUNT":
                continue
            if not served.serves(d_method, d_path):
                self.add(f"route:gone-unserved:{d_method} {d_path}", "routes",
                         f"link:{link['id']} says the server still answers the retired "
                         f"{d_method} {d_path} with {link['gone']['status']}, but no registration "
                         "exists -- drop it from the map, or keep the tombstone registered")

    def check_route_clients(self, scan: dict, mapped: RouteIndex,
                            retired: RouteIndex | None = None) -> None:
        """Client literals must be explained by a mapped link, and must not name a
        route that is only kept to answer that it is gone."""
        retired = retired or RouteIndex([])
        globs = scan.get("client_globs", [])
        if not globs or not self.guard(globs, keys=("anchor:route-scan:client",),
                                       prefixes=("route:client-unexplained:",
                                                 "route:gone-called:")):
            return
        literal_res = [re.compile(expr) for expr in scan.get("client_literal_regexes", [])]
        client_files = self.expand(globs, scan.get("client_exclude", []))
        if not client_files:
            self.add("anchor:route-scan:client", "anchors",
                     "client route scan matched no files -- misconfigured", fatal=True)
        if self.scope is not None:
            # only the changed client files (see check_routes). A literal is
            # keyed by its text, not its file: an unchanged file may still hold
            # one whose finding the changed files no longer raise, so with any
            # file skipped those findings are "not re-checked", never stale.
            changed = [rel for rel in client_files if self.in_scope([rel])]
            if len(changed) < len(client_files):
                self.skipped_prefixes += ["route:client-unexplained:", "route:gone-called:"]
            client_files = changed
        unexplained: dict[str, list[tuple[str, int]]] = {}
        called_gone: dict[str, list[tuple[str, int]]] = {}
        literal_count = 0
        for rel in client_files:
            text = self.text(rel) or ""
            for expr in literal_res:
                for match in expr.finditer(text):
                    literal = match.group(1)
                    if literal is None:
                        continue       # the group did not take part: no literal here
                    literal_count += 1
                    if mapped.explains(literal):
                        continue
                    where = (rel, self.line_at(rel, match.start()))
                    norm = self._norm_route_path(literal)
                    if not norm.endswith("/") and retired.explains(literal):
                        called_gone.setdefault(literal, []).append(where)
                    else:
                        unexplained.setdefault(literal, []).append(where)
        self.items_checked += literal_count
        for literal, places in sorted(unexplained.items()):
            shown = ", ".join(sorted({rel for rel, _ in places})[:3])
            self.add(f"route:client-unexplained:{literal}", "routes",
                     f"client literal {literal!r} (in {shown}) is not explained by any mapped link",
                     path=places[0][0], line=places[0][1])
        for literal, places in sorted(called_gone.items()):
            shown = ", ".join(sorted({rel for rel, _ in places})[:3])
            self.add(f"route:gone-called:{literal}", "routes",
                     f"client literal {literal!r} (in {shown}) names a retired route the server "
                     "only keeps to answer that it is gone -- call its replacement",
                     path=places[0][0], line=places[0][1])

    # ----------------------------------------------------- model consistency
    def check_status(self) -> None:
        m = self.model
        for link in m.entities["links"]:
            ends = [m.blocks[link["from"]], m.blocks[link["to"]]]
            worst = max(STATUS_RANK[b["status"]] for b in ends)
            if STATUS_RANK[link["status"]] < worst:
                names = ", ".join(f"{b['id']}={b['status']}" for b in ends)
                self.add(f"status:{link['id']}", "status",
                         f"link:{link['id']} is {link['status']} but touches {names}")
        for flow in m.entities["flows"]:
            if flow["status"] != "current":
                continue
            for index, step in enumerate(flow["steps"]):
                link = m.links.get(step["link"])
                if link and link["status"] != "current":
                    self.add(f"status:flow:{flow['id']}:{index + 1}", "status",
                             f"flow:{flow['id']} step {index + 1} uses {link['status']} "
                             f"link:{link['id']}")

    def check_provenance(self) -> None:
        m = self.model
        # (link, source, destination) per data item: `carries` flows from -> to,
        # `returns` (the response of a request/response link) flows to -> from.
        hops: dict[str, list[tuple[dict, str, str]]] = {}
        for link in m.entities["links"]:
            for data_id in link.get("carries", []):
                hops.setdefault(data_id, []).append((link, link["from"], link["to"]))
            for data_id in link.get("returns", []):
                hops.setdefault(data_id, []).append((link, link["to"], link["from"]))
        for item in m.entities["data"]:
            holders = {item["origin"]} | {entry["block"] for entry in item.get("also_from", [])}
            changed = True
            while changed:
                changed = False
                for link, src, dst in hops.get(item["id"], []):
                    if dst in holders:
                        continue
                    if any(m.related(src, h) for h in holders):
                        holders.add(dst)
                        changed = True
            for link, src, dst in hops.get(item["id"], []):
                self.items_checked += 1
                if not any(m.related(src, h) for h in holders):
                    direction = "carries" if src == link["from"] else "returns"
                    self.add(f"provenance:{link['id']}:{item['id']}", "provenance",
                             f"link:{link['id']} {direction} data:{item['id']} out of block "
                             f"'{src}', which neither creates nor receives it")
            for consumer in item.get("consumers", []):
                if not any(m.related(consumer, h) for h in holders):
                    self.add(f"provenance:consumer:{item['id']}:{consumer}", "provenance",
                             f"data:{item['id']} lists consumer '{consumer}' but no mapped link "
                             f"delivers it there")
            if not hops.get(item["id"]) and not item.get("consumers"):
                self.info.append(f"data:{item['id']} is not carried by any link (local only)")

    def check_link_evidence(self) -> None:
        m = self.model
        for link in m.entities["links"]:
            if link["status"] != "current":
                continue
            ends = [m.blocks[link["from"]], m.blocks[link["to"]]]
            has_code = [bool(self._has_code(b["id"])) for b in ends]
            for side, key in ((0, "from_anchors"), (1, "to_anchors")):
                if has_code[side] and not link.get(key):
                    self.add(f"evidence:{link['id']}:{key}", "evidence",
                             f"current link:{link['id']} has code on the {key[:-8]} side "
                             f"({ends[side]['id']}) but no {key}; anchor it to a literal in "
                             f"a file of {ends[side]['id']}, e.g. \"{key}\": [{{\"path\": "
                             f"\"<file>\", \"find\": \"<text in that file>\"}}]", fatal=True)

    def _has_code(self, block_id: str) -> bool:
        return any(self.model.blocks[b].get("code") for b in self.model.ancestors(block_id))

    # ------------------------------------------------------------------ seams
    def _code_block(self, block_id: str) -> str | None:
        """The block whose `code` stands for this one: itself, or its nearest
        ancestor that has code."""
        return next((b for b in self.model.ancestors(block_id) if self.model.blocks[b].get("code")),
                    None)

    def _side_present(self, link: dict, side: str) -> bool:
        return bool(link.get(f"{side}_anchors")) or self._has_code(link[side])

    def _file_side(self, rel: str, link: dict) -> str | None:
        """'from' or 'to' when file `rel` belongs to exactly that end of the link,
        else None. An anchor file of one side belongs to it. Any other file
        belongs to the block with the most specific `code` holding it (its
        owner), and so to the end that is that owner or contains it -- the deeper
        end when the two ends contain each other. A file whose owner only
        ENCLOSES an end counts for that end (a part without code of its own)
        unless the other end inherits the same code: two parts of one block
        cannot tell their files apart by code, only by anchors."""
        anchored = [side for side in ("from", "to")
                    if any(a["path"] == rel for a in link.get(f"{side}_anchors", []))]
        if len(anchored) == 1:
            return anchored[0]
        m = self.model
        sides: set[str | None] = set()
        for owner in code_owners(m, rel):
            chain = m.ancestors(owner)
            containing = [side for side in ("from", "to") if link[side] in chain]
            if containing:
                sides.add(max(containing, key=lambda side: len(m.ancestors(link[side]))))
                continue
            inheriting = [side for side in ("from", "to") if self._code_block(link[side]) == owner]
            sides.add(inheriting[0] if len(inheriting) == 1 else None)
        return sides.pop() if len(sides) == 1 else None

    def _guard_files(self, kind: str, entry: dict) -> set[str]:
        if kind == "joins":
            return {point["path"] for point in entry.get("points", [])}
        if kind == "pairs":
            return {region["path"] for region in entry.get("regions", [])}
        if kind == "ui_actions":
            return {f for side in [entry["controls"], *entry["hops"]]
                    for f in self.expand(side.get("paths", []), side.get("exclude"))}
        files: set[str] = set()
        for key in ("left", "right"):
            side = entry.get(key, {})
            files.update(self.expand(side.get("files") or side.get("paths") or [],
                                     side.get("exclude")))
        return files

    def _spans(self, files: set[str], link: dict) -> bool:
        """A guard compares the two sides only if it reads a file of the `from`
        end and a file of the `to` end. Each file belongs to one end at most, so
        the two are different files (one shared file compared with itself proves
        nothing) of different ends (two files of one end prove nothing either)."""
        return {"from", "to"} <= {self._file_side(f, link) for f in files}

    def check_seams(self) -> None:
        m = self.model
        guards: dict[str, list[tuple[str, set[str]]]] = {}
        for kind, prefix in (("joins", "join"), ("sets", "set"), ("pairs", "pair"),
                             ("ui_actions", "ui")):
            for entry in m.entities[kind]:
                if entry.get("link"):
                    guards.setdefault(entry["link"], []).append(
                        (f"{prefix}:{entry['id']}", self._guard_files(kind, entry)))
        for link in m.entities["links"]:
            if link["status"] != "current":
                continue
            ident = link["id"]
            self.items_checked += 1
            both_sides = self._side_present(link, "from") and self._side_present(link, "to")
            contract = link.get("contract")
            if not contract:
                if both_sides and link["transport"] not in COMPILER_CHECKED_TRANSPORTS:
                    self.add(f"seam:undeclared:{ident}", "seams",
                             f"link:{ident} ({link['transport']}) has our code on both ends but "
                             f"declares no contract -- say how the two sides are kept in "
                             f"agreement (contract.enforcement: "
                             f"{', '.join(sorted(ENFORCEMENT))})", fatal=True)
                continue
            enforcement = contract["enforcement"]
            if enforcement in SINGLE_OWNER_ENFORCEMENT:
                if not contract.get("defined_in"):
                    self.add(f"seam:owner-unanchored:{ident}", "seams",
                             f"link:{ident} claims {enforcement} but contract.defined_in does "
                             f"not name the single owner (shared header / generated file / "
                             f"schema)", fatal=True)
                continue
            if enforcement == "none" and not both_sides:
                continue  # the other side is not our code: nothing to compare
            present = guards.get(ident, [])
            if any(self._spans(files, link) for _, files in present):
                continue
            if present:
                names = ", ".join(name for name, _ in present)
                detail = (f"its guard(s) {names} do not read a file of each side "
                          f"(from: {link['from']}, to: {link['to']})")
            else:
                detail = "no join/set/pair with \"link\": \"" + ident + "\" compares the two sides"
            anchors = link.get("from_anchors") or link.get("to_anchors") or []
            self.add(f"seam:unguarded:{ident}", "seams",
                     f"link:{ident} is written by hand on both sides ({enforcement}) and {detail} "
                     f"-- updating one side would silently leave the other behind; add a "
                     f"join/set/pair or record a finding with this key",
                     path=anchors[0]["path"] if anchors else None)

    def check_coverage(self) -> None:
        coverage = self.model.meta.get("coverage")
        if not coverage:
            return
        code_paths = [p.rstrip("/") for b in self.model.entities["blocks"] for p in b.get("code", [])]
        ignored = {entry["path"].rstrip("/") for entry in coverage.get("ignore", [])}
        for pattern in coverage.get("roots", []):
            for path in sorted(self.root.glob(pattern)):
                # hidden directories and Python's byte-code cache are never source
                # areas; `_billing` is (anything else goes in coverage.ignore)
                if not path.is_dir() or path.name.startswith(".") or path.name == "__pycache__":
                    continue
                rel = path.relative_to(self.root).as_posix()
                self.items_checked += 1
                if rel in ignored:
                    continue
                covered = any(cp == rel or cp.startswith(rel + "/") or rel.startswith(cp + "/")
                              for cp in code_paths)
                if not covered:
                    self.add(f"coverage:{rel}", "coverage",
                             f"source area {rel} is not placed on the map (no block covers it)")
        for entry in coverage.get("ignore", []):
            if not (self.root / entry["path"]).exists():
                self.add(f"coverage:stale-ignore:{entry['path']}", "coverage",
                         f"coverage ignore entry {entry['path']} no longer exists", fatal=True)

    def check_requirements(self) -> None:
        refs: set[str] = set()
        for kind in ("blocks", "links", "flows", "findings"):
            for item in self.model.entities[kind]:
                refs.update(item.get("requirements", []))
        doc = self.model.cfg.requirements_doc
        if not refs or doc is None:
            return  # a reference without a configured doc is a schema error already
        text = self.text(doc.as_posix())
        if text is None:
            self.add("anchor:requirements", "anchors", f"{doc.as_posix()} missing", fatal=True,
                     path=doc.as_posix())
            return
        for ref in sorted(refs):
            self.items_checked += 1
            if not re.search(r"^\|\s*" + re.escape(ref) + r"\s*\|", text, re.MULTILINE):
                self.add(f"requirement:{ref}", "anchors",
                         f"requirement {ref} is referenced but has no table row "
                         f"'| {ref} |' in {doc.as_posix()}", fatal=True, path=doc.as_posix())

    def check_freshness(self) -> None:
        verified = self.model.meta.get("verified_against")
        if not verified:
            return
        probe = subprocess.run(["git", "-C", str(self.root), "cat-file", "-t", verified],
                               capture_output=True, text=True)
        if probe.returncode != 0 or probe.stdout.strip() != "commit":
            self.info.append(f"freshness: verified_against {verified} not reachable -- "
                             f"drift cannot be computed")
            return
        try:
            changed = _git_paths(self.root, "diff", "--name-only", "--relative",
                                 f"{verified}..HEAD")
        except ImpactError:
            self.info.append("freshness: git diff failed -- drift cannot be computed")
            return
        stale = []
        for block in self.model.entities["blocks"]:
            paths = [p.rstrip("/") for p in block.get("code", [])]
            touched = [c for c in changed if any(c == p or c.startswith(p + "/") for p in paths)]
            if touched:
                stale.append((block["id"], len(touched)))
        if stale:
            listed = ", ".join(f"{ident}({count})" for ident, count in stale)
            self.info.append(f"freshness: code changed since {verified[:12]} in {len(stale)} "
                             f"block(s) -- re-verify: {listed}")

    # --------------------------------------------------------------- findings
    def reconcile_findings(self) -> list[str]:
        """Attach problems to findings; return keys of stale findings."""
        by_key: dict[str, str] = {}
        stale: list[str] = []
        for finding in self.model.entities["findings"]:
            if finding["status"] == "closed":
                continue
            for key in finding.get("detected_by", []):
                if key != "manual":
                    by_key[key] = finding["id"]
        fired = {p.key for p in self.problems}
        for problem in self.problems:
            if not problem.fatal and problem.key in by_key:
                problem.finding = by_key[problem.key]
        for finding in self.model.entities["findings"]:
            if finding["status"] == "closed":
                continue
            for key in finding.get("detected_by", []):
                if key == "manual" or key in fired:
                    continue
                if self.evaluated(key):
                    stale.append(f"{finding['id']}:{key}")
                else:
                    # its guard was skipped by `check --changed`: not re-checked, not stale
                    self.unverified.append(f"{finding['id']}:{key}")
        return stale

    def run(self) -> None:
        self.check_all_anchors()
        self.check_joins()
        self.check_sets()
        self.check_pairs()
        self.check_routes()
        self.check_ui_actions()
        self.check_status()
        self.check_provenance()
        self.check_seams()
        self.check_link_evidence()
        self.check_coverage()
        self.check_requirements()
        self.check_freshness()
        self.check_agents()
        self.check_joints()
        self.check_hazards()
        self.check_fallbacks()

    def check_hazards(self) -> None:
        """Built-in hazard rules (crossweft/hazards.py) over the files named by
        `hazards.include`. A glob that names no file is a loud error: a rule set
        that silently scans nothing must not read as clean."""
        cfg = self.model.cfg
        if not cfg.hazards_include:
            return
        files, empty = self.expand_entries(cfg.hazards_include, cfg.hazards_exclude)
        for pattern in empty:
            self.add(f"hazard-config:empty:{pattern}", "hazards",
                     f"hazards.include entry '{pattern}' names no file -- fix the glob or remove "
                     f"it from {CONFIG_FILE}", fatal=True)
        rules = set(cfg.hazards_rules)
        for rel in files:
            if not self.guard([rel], prefixes=(f"hazard:{rel}:",)):
                continue
            text = self.text(rel)
            if text is None:
                continue
            self.items_checked += 1
            hits = hazards.scan(rel, text, rules)
            for hit, key in zip(hits, hazards.keys(rel, hits)):
                self.add(key, "hazards", f"{hit.rule}: {hit.message} (`{hit.text}`)",
                         path=rel, line=hit.line)

    def fallback_sites(self) -> tuple[list[fallbacks.Site], dict[str, str]]:
        """Every fallback site in `fallbacks.include` and the text of each scanned file; a
        glob naming no file and a file that cannot be scanned are fatal problems."""
        cfg = self.model.cfg
        files, empty = self.expand_entries(cfg.fallbacks_include, cfg.fallbacks_exclude)
        for pattern in empty:
            self.add(f"fallback-config:empty:{pattern}", "fallbacks",
                     f"fallbacks.include entry '{pattern}' names no file -- fix the glob or "
                     f"remove it from {CONFIG_FILE}", fatal=True)
        sites: list[fallbacks.Site] = []
        texts: dict[str, str] = {}
        for rel in files:
            text = self.text(rel)
            if text is None:
                self.add(f"fallback-config:unreadable:{rel}", "fallbacks",
                         f"{rel} could not be read as UTF-8 text -- fix it or exclude it in "
                         "fallbacks.exclude", fatal=True, path=rel)
                continue
            result = fallbacks.scan(rel, text, frozenset(cfg.fallbacks_stops))
            if isinstance(result, str):
                self.add(f"fallback-config:unscanned:{rel}", "fallbacks",
                         f"{rel}: {result} -- exclude it in fallbacks.exclude or fix it",
                         fatal=True, path=rel)
                continue
            self.items_checked += 1
            texts[rel] = text
            sites.extend(result)
        return sites, texts

    def check_fallbacks(self) -> None:
        """Every fallback site is in the registry; refused ones are gone; agreed ones are
        unchanged; every entry still has its site (crossweft/fallbacks.py)."""
        cfg = self.model.cfg
        if not cfg.fallbacks_include:
            return
        # simplification: always a full scan; scope by changed files when it gets slow
        self.guards_total += 1
        self.guards_run += 1
        registry = cfg.fallbacks_registry.as_posix()
        try:
            entries, last_id = fallbacks.load_registry(cfg.abs(cfg.fallbacks_registry))
        except fallbacks.RegistryError as exc:
            self.add("fallback-config:registry", "fallbacks", str(exc), fatal=True, path=registry)
            return
        sites, texts = self.fallback_sites()
        result = fallbacks.match(sites, entries)
        wrappers = {e["symbol"] for e in entries if e.get("wrapper")}
        uses = {rel: fallbacks.wrapper_calls(rel, text, wrappers) for rel, text in texts.items()}
        self.fallback_report = {"entries": entries, "match": result, "uses": uses,
                                "last_id": last_id}
        for key, site in fallbacks.keyed(result.unregistered):
            self.add(f"fallback:unregistered:{key}", "fallbacks",
                     f"{site.symbol}: {fallbacks.KINDS[site.kind]} (`{site.text}`) and "
                     f"{registry} does not list it -- register it for the owner's decision: "
                     f"crossweft fallbacks propose {key} --behavior '...' --reason '...'",
                     fatal=True, path=site.path, line=site.line)
        for site, why in result.unknown_markers:
            self.add(f"fallback:unknown-marker:{site.path}:{site.line}", "fallbacks",
                     f"marker `fallback: {site.marker}` {why} -- a marker ties one site to "
                     "its own entry; propose this site instead", fatal=True, path=site.path,
                     line=site.line)
        signed = self.fallback_signers()
        previous = self.fallback_registry_at_head()
        for entry in entries:
            ident = entry["id"]
            found = result.by_entry.get(ident, [])
            called = [(rel, line) for rel, calls in uses.items()
                      for name, line in calls if name == entry["symbol"]] \
                if entry.get("wrapper") else []
            if not found:
                self.add(f"fallback:stale:{ident}", "fallbacks",
                         f"{ident}: {entry['symbol']} in {entry['path']} no longer has this "
                         f"{entry['kind']} -- remove the entry", fatal=True, path=registry)
            if entry["status"] == "rejected" and (found or called):
                where = found[0].path if found else called[0][0]
                line = found[0].line if found else called[0][1]
                self.add(f"fallback:rejected:{ident}", "fallbacks",
                         f"{ident} was refused ({entry['reason']}) and is still in the code -- "
                         "remove it and let the error surface", fatal=True, path=where, line=line)
            if entry["status"] == "approved" and ident in result.changed:
                site = result.changed[ident][0]
                self.add(f"fallback:changed:{ident}", "fallbacks",
                         f"{ident} was agreed as it was and its code changed since -- propose "
                         f"it again: crossweft fallbacks propose {ident} --reason '...'",
                         fatal=True, path=site.path, line=site.line)
            if entry["status"] == "proposed" and cfg.fallbacks_strict:
                self.add(f"fallback:undecided:{ident}", "fallbacks",
                         f"{ident} waits for the owner's decision and fallbacks.strict is on: "
                         f"{entry['behavior']}", fatal=True,
                         path=found[0].path if found else registry,
                         line=found[0].line if found else None)
            if signed is not None and entry["status"] != "proposed":
                problem = signed(entry)
                if problem:
                    self.add(f"fallback:unsigned:{ident}", "fallbacks",
                             f"{ident}: the {entry['status']} decision {problem}", fatal=True,
                             path=registry)
            before = previous.get(ident)
            if before is not None and entry.get("rev", 0) < before.get("rev", 0):
                self.add(f"fallback:rollback:{ident}", "fallbacks",
                         f"{ident} is at revision {entry.get('rev', 0)}, older than "
                         f"{before.get('rev', 0)} in the last commit -- an older decision was "
                         "put back", fatal=True, path=registry)
        if signed is None and any(e.get("sig") for e in entries):
            self.add("fallback-config:signing-off", "fallbacks",
                     f"{registry} holds signed decisions but fallbacks.signers is not set -- "
                     "signing cannot be switched off while signed decisions exist", fatal=True,
                     path=CONFIG_FILE)
        waiting = sum(1 for e in entries if e["status"] == "proposed")
        if waiting and not cfg.fallbacks_strict:
            self.info.append(f"fallbacks: {waiting} of {len(entries)} wait for the owner's "
                             "decision (crossweft fallbacks --status proposed)")
        if signed is None and any(e["status"] != "proposed" for e in entries):
            self.info.append("fallbacks: decisions are not signed (no fallbacks.signers), so "
                             "anyone who can edit the registry can approve")
        if signed is not None and not cfg.fallbacks_signers.is_absolute():
            self.info.append("fallbacks: the trusted keys are inside the repository, where an "
                             "agent can edit them -- keep them outside (an absolute path)")

    def fallback_registry_at_head(self) -> dict[str, dict]:
        """The registry entries as committed at HEAD, by id ({} outside git or when the file
        is new): an entry whose revision went down was put back from older history."""
        cfg = self.model.cfg
        rel = cfg.fallbacks_registry.as_posix()
        try:
            done = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=str(cfg.root),
                                  capture_output=True, text=True, encoding="utf-8", timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return {}
        if done.returncode != 0:
            return {}
        try:
            data = json.loads(done.stdout)
        except ValueError:
            return {}
        return {e.get("id"): e for e in data.get("fallbacks", []) if isinstance(e, dict)}

    def fallback_signers(self):
        """With fallbacks.signers, a function telling why a decision's signature does not
        verify (None when it does); None when signing is off."""
        cfg = self.model.cfg
        if cfg.fallbacks_signers is None:
            return None
        signers = cfg.fallbacks_signers if cfg.fallbacks_signers.is_absolute() \
            else cfg.abs(cfg.fallbacks_signers)

        def check(entry: dict) -> str | None:
            if not signers.is_file():
                return f"cannot be verified: {signers.as_posix()} is missing"
            if not entry.get("sig"):
                return "is not signed"
            return fallbacks.verify(entry, signers)
        return check

    def check_agents(self) -> None:
        """With agent.harness, the repository must carry its own agent harness
        (hooks, AGENTS.md block, skill) -- see crossweft/harness.py."""
        cfg = self.model.cfg
        if not cfg.agent_harness:
            return
        # not counted in items_checked: the harness is not model content, and an
        # empty model must still fail as "scanned nothing"
        for key, message, path in harness.problems(cfg.root, cfg.model_dir.as_posix()):
            self.add(key, "agents", message, path=path)

    # ----------------------------------------------------------------- joints
    def check_joints(self) -> None:
        """Joint-kind plugins (crossweft/joints.py): `meta.joint_kinds` keeps every
        registered kind on, then each plugin checks its own section."""
        model = self.model
        pinned = model.meta.get("joint_kinds")
        if pinned is not None or any(model.sections.values()):
            problems, info = joints.pin_problems(pinned, model.plugins, model.sections)
            for key, message in problems:
                self.add(key, "joints", message, fatal=True)
            if info:
                self.info.append(info)
        for plugin in model.plugins:
            entries = model.sections.get(plugin.kind) or []
            if not entries:
                continue
            # the files a kind declares (entry paths + IMPACT_GLOBS); a kind that
            # declares none cannot be scoped and always runs
            feeds = sorted(joints.references(plugin, entries))
            if feeds and not self.guard(feeds, prefixes=(f"{plugin.kind}:",)):
                continue
            if not feeds:
                self.guards_total += 1
                self.guards_run += 1
            outcome = joints.run_check(plugin, self.root, entries, model)
            for key, message, fatal in outcome.problems:
                self.add(key, "joints", message, fatal=fatal)
            self.files_scanned |= outcome.files
            self.items_checked += outcome.items
            if outcome.info:
                self.info.append(f"{plugin.kind}: {outcome.info}")


@dataclasses.dataclass
class CheckResult:
    """Everything one `check` found. Pure data: formatting is the caller's."""

    # why the check could not produce a verdict (unreadable model, crash, nothing
    # scanned): exit 2, never a pass
    load_error: str | None = None
    schema_errors: list = dataclasses.field(default_factory=list)
    new: list = dataclasses.field(default_factory=list)          # Problems that fail the check
    known: list = dataclasses.field(default_factory=list)        # Problems excused by findings
    stale: list = dataclasses.field(default_factory=list)        # finding:key no longer firing
    render_stale: list = dataclasses.field(default_factory=list)  # generated files out of date
    info: list = dataclasses.field(default_factory=list)
    counts: dict = dataclasses.field(default_factory=dict)
    files: int = 0
    items: int = 0
    # the check ran but read nothing (load_error says why); hooks tell an empty,
    # not yet mapped model apart from a broken one by this, not by `ok`
    nothing_scanned: bool = False
    # `check --changed REV`: {"rev", "files", "guards_run", "guards_total",
    # "unverified", "fallback"}; fallback says why the full check ran instead.
    # None for a plain `check`.
    changed: dict | None = None

    @property
    def ok(self) -> bool:
        return not (self.load_error or self.schema_errors or self.new or self.stale
                    or self.render_stale)

    def failure_lines(self, limit: int = 12) -> list[str]:
        """Short, agent-readable reasons (used by hooks and summaries)."""
        rows: list[str] = []
        if self.load_error:
            rows.append(self.load_error)
        rows += [f"model schema: {error}" for error in self.schema_errors]
        for problem in self.new:
            where = f" [{problem.path}{':' + str(problem.line) if problem.line else ''}]" \
                if problem.path else ""
            must = " -- must be fixed, a finding cannot excuse it" if problem.fatal else ""
            rows.append(f"{problem.message}{where} (key: {problem.key}){must}")
        rows += [f"stale finding {entry}: its detector no longer fires -- close or update it"
                 for entry in self.stale]
        if self.render_stale:
            rows.append(f"generated docs are stale ({', '.join(self.render_stale)}) -- run "
                        "`crossweft render`")
        if len(rows) > limit:
            rows = rows[:limit] + [f"... and {len(rows) - limit} more (run `crossweft check`)"]
        return rows


def evaluate(cfg: Config, changed: str | None = None,
             also_changed: list[str] | None = None) -> CheckResult:
    """Run every check against the code and return the result, printing nothing.
    With `changed` (a git revision), run only the guards that read a file
    changed since it (see change_scope) -- or the full check when the change set
    cannot be trusted; result.changed says which."""
    result = CheckResult()
    try:
        model = load_model(cfg)
    except ModelError as exc:
        result.load_error = str(exc)
        return result
    scope: list[str] | None = None
    if changed is not None:
        scope, fallback = change_scope(cfg, changed, also_changed or ())
        result.changed = {"rev": changed, "files": len(scope or []), "guards_run": 0,
                          "guards_total": 0, "unverified": [], "fallback": fallback}
    result.counts = {k: len(v) for k, v in model.entities.items()}
    result.schema_errors = validate_schema(model)
    result.files = len(model.files)
    if result.schema_errors:
        return result
    checker = Checker(model)
    checker.scope = scope
    try:
        checker.run()
    except Exception as exc:  # noqa: BLE001 -- converted to a loud, non-passing result
        result.load_error = (f"crossweft crashed while checking ({type(exc).__name__}: {exc}) "
                             "-- nothing was verified; report this as a crossweft bug")
        return result
    result.stale = checker.reconcile_findings()
    if result.changed is not None:
        result.changed.update(guards_run=checker.guards_run, guards_total=checker.guards_total,
                              unverified=checker.unverified)
    result.new = [p for p in checker.problems if p.finding is None]
    result.known = [p for p in checker.problems if p.finding is not None]
    result.info = checker.info
    result.files = len(checker.files_scanned) + len(model.files)
    result.items = checker.items_checked + sum(len(v) for v in model.entities.values())
    if cfg.check_rendered_docs:
        try:
            result.render_stale = stale_generated_files(model)
        except (OSError, ModelError) as exc:
            result.load_error = f"render check failed: {exc}"
    if result.ok and (result.files == 0 or result.items == 0):
        # zero inputs is an error, never a pass -- in every output format
        result.nothing_scanned = True
        result.load_error = "scanned nothing -- nothing was verified: " + (
            "the model declares nothing yet. Run `crossweft discover` and add your first "
            "blocks, links and guards." if not any(result.counts.values()) else "misconfigured")
    return result


CATEGORIES = ("anchors", "joins", "sets", "pairs", "routes", "status", "provenance", "seams",
              "evidence", "coverage", "agents", "joints", "hazards", "ui")


def _gh_escape(text: str, property_value: bool = False) -> str:
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if property_value:
        text = text.replace(":", "%3A").replace(",", "%2C")
    return text


SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"


SARIF_RULES = {
    "model": "The model or config does not load or validate, so nothing was checked.",
    "anchors": "A literal the map points at moved or vanished.",
    "joins": "A value that must be equal across sides differs.",
    "sets": "A member exists on one side of a seam only.",
    "pairs": "A fingerprinted region of duplicated logic changed since its attestation.",
    "routes": "A route is served or called but not declared by a link (or the reverse).",
    "status": "A current link depends on a legacy or planned block.",
    "provenance": "Data leaves a block that neither created nor received it.",
    "seams": "A link declares no contract, or a hand-written seam has no guard.",
    "evidence": "A link lacks the anchors that tie it to code.",
    "coverage": "A source area is on no block of the map.",
    "agents": "The repository's agent harness is missing or out of date.",
    "joints": "A repository-specific joint plugin reports a disagreement or failed.",
    "hazards": "Script text that silently breaks what another program reads (crossweft/hazards.py).",
    "ui": "A control does not reach its handler, or a handler no control sends to.",
    "stale-finding": "A recorded finding no longer reproduces and must be closed.",
    "render": "Generated documentation is out of date with the model.",
}


def sarif_payload(result: CheckResult, fallback: str = CONFIG_FILE) -> dict:
    """SARIF 2.1.0 for code-scanning UIs. Exit codes stay those of `check`.

    Every result has a location (code scanning drops or rejects one without):
    a problem with no file of its own points at `fallback`, line 1. A
    finding-excused problem carries an external suppression; an unreadable
    model is a failed invocation AND a located error, never an empty run that
    a code-scanning upload would read as "every alert fixed".
    """
    from . import __version__

    def result_row(rule: str, message: str, level: str = "error", path: str | None = None,
                   line: int | None = None, key: str | None = None) -> dict:
        return {
            "ruleId": rule, "level": level, "message": {"text": message},
            "locations": [{"physicalLocation": {
                "artifactLocation": {"uri": path or fallback, "uriBaseId": "%SRCROOT%"},
                "region": {"startLine": line if (path and line) else 1}}}],
            **({"partialFingerprints": {"crossweftKey/v1": key}} if key else {}),
        }

    def text_key(kind: str, text: str) -> str:
        # a problem with no finding key of its own is identified by its rule and
        # its text (whitespace and path separators normalised), so a re-upload
        # through the API updates the same alert instead of opening a duplicate
        normalised = " ".join(text.replace("\\", "/").split())
        return f"{kind}:{normalised}"

    rows = []
    if result.load_error:
        rows.append(result_row("crossweft/model", result.load_error,
                               key=text_key("model", result.load_error)))
    rows += [result_row("crossweft/model", f"model schema: {e}",
                        key=text_key("model-schema", str(e))) for e in result.schema_errors]
    for problem in result.new:
        must = " (must be fixed; a finding cannot excuse it)" if problem.fatal else ""
        rows.append(result_row(f"crossweft/{problem.category}", problem.message + must,
                               path=problem.path, line=problem.line, key=problem.key))
    for problem in result.known:
        row = result_row(f"crossweft/{problem.category}", problem.message, level="note",
                         path=problem.path, line=problem.line, key=problem.key)
        row["suppressions"] = [{"kind": "external",
                                "justification": f"recorded finding {problem.finding}"}]
        rows.append(row)
    rows += [result_row("crossweft/stale-finding",
                        f"stale finding {entry}: its detector no longer fires -- close or update it",
                        key=f"stale:{entry}")
             for entry in result.stale]
    if result.render_stale:
        rows.append(result_row("crossweft/render",
                               f"generated docs are stale ({', '.join(result.render_stale)}) "
                               "-- run `crossweft render`",
                               # one alert for "the docs are stale", whichever files it lists
                               key="render:stale"))
    invocation: dict = {"executionSuccessful": not (result.load_error or result.schema_errors)}
    if result.load_error:
        invocation["toolExecutionNotifications"] = [
            {"level": "error", "message": {"text": result.load_error}}]

    def rule(rule_id: str) -> dict:
        text = SARIF_RULES.get(rule_id.split("/", 1)[1], "A crossweft check failed.")
        return {"id": rule_id, "shortDescription": {"text": text},
                "fullDescription": {"text": text},
                "help": {"text": text + " Run `crossweft check` for the details and the "
                                        "other side to re-read."}}

    return {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "crossweft", "version": __version__,
                "informationUri": "https://github.com/AnastasiyaW/crossweft",
                "rules": [rule(r) for r in sorted({row["ruleId"] for row in rows})]}},
            "invocations": [invocation],
            "results": rows,
        }],
    }


def json_payload(result: CheckResult) -> dict:
    """The `check --format json` document."""
    return {
        "ok": result.ok,
        "error": result.load_error,
        "schema_errors": result.schema_errors,
        "new": [p.as_dict() for p in result.new],
        "known": [p.as_dict() for p in result.known],
        "stale_findings": result.stale,
        "stale_generated": result.render_stale,
        "info": result.info,
        "counts": result.counts,
        "scanned": {"files": result.files, "items": result.items},
        # null for a plain check; with --changed, "fallback" is non-null when
        # the full check ran instead
        "changed_only": result.changed,
    }


def machine_document(result: CheckResult, fmt: str) -> str:
    """`result` as the one json or sarif document a machine format puts on stdout."""
    payload = sarif_payload(result) if fmt == "sarif" else json_payload(result)
    return json.dumps(payload, ensure_ascii=True, indent=2)


def config_error_document(message: str, fmt: str) -> str:
    """A failed-check json/sarif document for when crossweft.json itself cannot be
    read: same shape as a normal run, `ok` false / a failed invocation, exit 2."""
    return machine_document(CheckResult(load_error=message), fmt)


def print_result(cfg: Config, result: CheckResult, fmt: str = "text") -> None:
    if fmt in ("json", "sarif"):
        print(machine_document(result, fmt))
        return
    if fmt == "github":
        # GitHub Actions workflow commands: one annotation per problem.
        for line in ([result.load_error] if result.load_error else []) + [
                f"model schema: {e}" for e in result.schema_errors]:
            print(f"::error title=crossweft::{_gh_escape(line)}")
        for problem in result.new:
            props = ["title=crossweft " + _gh_escape(problem.category, True)]
            if problem.path:
                props.insert(0, "file=" + _gh_escape(problem.path, True))
                if problem.line:
                    props.insert(1, f"line={problem.line}")
            print(f"::error {','.join(props)}::{_gh_escape(problem.message)}")
        for entry in result.stale:
            print(f"::error title=crossweft stale finding::{_gh_escape(entry)}")
        if result.render_stale:
            print("::error title=crossweft::generated docs are stale -- run `crossweft render`")
        for problem in result.known:
            print(f"::notice title=crossweft known ({problem.finding})::{_gh_escape(problem.key)}")
    # text (also printed after the github annotations, for the log)
    if result.load_error:
        out(f"[ERR] {result.load_error}")
    elif result.schema_errors:
        out(f"[FAIL] model schema: {len(result.schema_errors)} error(s)")
        for error in result.schema_errors:
            out(f"   - {error}")
    else:
        counts = " ".join(f"{k}={v}" for k, v in result.counts.items())
        out(f"CROSSWEFT CHECK  model={cfg.model_dir.as_posix()}  {counts}")
        for category in CATEGORIES:
            rows = [p for p in result.new if p.category == category]
            if category == "hazards" and not rows and not cfg.hazards_include:
                continue        # an opt-in category: not shown where it is off
            if rows:
                out(f"[FAIL] {category}: {len(rows)}")
                for problem in rows:
                    out(f"   - {problem.message}")
                    where = (f"  at {problem.path}" + (f":{problem.line}" if problem.line else "")
                             if problem.path else "")
                    out(f"     key: {problem.key}{where}")
            else:
                out(f"[OK]   {category}")
        # a problem outside the listed categories is still shown, never only counted
        for problem in [p for p in result.new if p.category not in CATEGORIES]:
            out(f"[FAIL] {problem.category}: {problem.message}")
            out(f"     key: {problem.key}")
        if result.known:
            out(f"[KNOWN] {len(result.known)} problem(s) are recorded findings:")
            for problem in result.known:
                out(f"   - {problem.finding}: {problem.key}")
        if result.stale:
            out(f"[FAIL] stale findings: {len(result.stale)} detector key(s) no longer fire -- "
                f"close or update these findings:")
            for entry in result.stale:
                out(f"   - {entry}")
        if result.render_stale:
            out(f"[FAIL] generated docs are stale: {', '.join(result.render_stale)} -- run "
                "`crossweft render`")
        for line in result.info:
            out(f"[INFO] {line}")
        partial = _changed_only(result)
        if result.changed and result.changed["fallback"]:
            out(f"[INFO] --changed {result.changed['rev']}: ran the FULL check instead -- "
                f"{result.changed['fallback']}")
        elif partial:
            out(f"[INFO] --changed {partial['rev']}: only the {partial['guards_run']} of "
                f"{partial['guards_total']} guards that read one of {partial['files']} changed (or "
                "git-ignored) path(s) ran; model-wide checks ran in full. Not a full check: run "
                "`crossweft check` before a release.")
            if partial["unverified"]:
                out(f"[INFO] {len(partial['unverified'])} finding key(s) not re-checked (their "
                    "guards read no changed file), so neither confirmed nor stale:")
                for entry in partial["unverified"]:
                    out(f"   - {entry}")
    if result.load_error:
        out("RESULT: ERROR (not verified -- see [ERR] above; this is not a pass)")
    elif result.schema_errors:
        out("RESULT: ERROR (model not loaded -- fix the schema errors above; nothing was "
            "checked against the code)")
    else:
        out(f"RESULT: {'PASS' if result.ok else 'FAIL'} (new={len(result.new)} "
            f"known={len(result.known)} stale_findings={len(result.stale)})")
    partial = _changed_only(result)
    suffix = (f"  (changed-only: {partial['guards_run']} of {partial['guards_total']} guards)"
              if partial else "  (full check; --changed fell back)"
              if result.changed and result.changed["fallback"] else "")
    out(f"SCANNED: files={result.files} items={result.items}{suffix}")
    if not result.ok and feedback_line(cfg, "check"):
        out(feedback_line(cfg, "check"))


def _changed_only(result: CheckResult) -> dict | None:
    """The `--changed` summary when only part of the guards ran, else None (a
    plain check, or a --changed run that fell back to the full check)."""
    if result.changed is None or result.changed["fallback"]:
        return None
    return result.changed


PASS_RECORD = "crossweft-last-pass.json"


def _pass_record_path(cfg: Config) -> Path | None:
    """Where the last fully verified commit is kept: inside the git directory of
    this work tree, so it is never committed and every worktree has its own."""
    try:
        found = _git_lines(cfg.root, "rev-parse", "--git-path", PASS_RECORD)
    except ImpactError:
        return None
    if not found:
        return None
    path = Path(found[0])
    return path if path.is_absolute() else cfg.root / path


def _engine_digest() -> str:
    """The crossweft code that checked, not just its version number: a record made
    by an edited checkout of the same version must not be trusted."""
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _pass_snapshot(cfg: Config) -> tuple[str, list[str], str] | None:
    """HEAD, the uncommitted paths and the engine digest, read BEFORE the check
    runs: a commit, an edit or an engine change that lands while it runs is then
    never recorded as verified."""
    try:
        head = _git_lines(cfg.root, "rev-parse", "HEAD")
        dirty = set(_git_paths(cfg.root, "diff", "--name-only", "--relative", "--no-renames", "HEAD"))
        dirty.update(_git_paths(cfg.root, "ls-files", "--others", "--exclude-standard"))
    except ImpactError:
        return None
    return (head[0], sorted(dirty), _engine_digest()) if head else None


def incremental_base(cfg: Config) -> tuple[str | None, list[str], str]:
    """(commit, dirty, why): the last commit at which the map passed with this
    crossweft version and the paths that were uncommitted then (they rerun), or
    (None, [], why the full check must run)."""
    from . import __version__
    path = _pass_record_path(cfg)
    if path is None:
        return None, [], "not a git work tree"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, [], "no passing check recorded yet"
    except (OSError, ValueError) as exc:
        return None, [], f"the pass record is unreadable ({exc})"
    commit = record.get("commit") if isinstance(record, dict) else None
    dirty = record.get("dirty", []) if isinstance(record, dict) else []
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return None, [], "the pass record names no commit"
    if not isinstance(dirty, list) or not all(isinstance(d, str) for d in dirty):
        return None, [], "the pass record's dirty paths are malformed"
    if record.get("version") != __version__:
        return None, [], f"the last pass was checked by crossweft {record.get('version')}"
    if record.get("engine") != _engine_digest():
        return None, [], "the last pass was checked by a different build of crossweft"
    try:
        if not _git_lines(cfg.root, "rev-parse", "--verify", "--quiet", commit + "^{commit}"):
            return None, [], f"{commit[:12]} is gone"
    except ImpactError:
        return None, [], f"{commit[:12]} is gone"
    return commit, dirty, ""


def record_pass(cfg: Config, result: CheckResult,
                snapshot: tuple[str, list[str], str] | None) -> None:
    """After a passing check, remember HEAD as verified, with the paths that were
    uncommitted: the next incremental check reruns every guard reading one of
    them, so reverting an uncommitted file cannot bring back bytes nobody
    checked. A --changed pass counts: every guard that reads a changed path ran
    and the rest read the same bytes as at the verified base."""
    from . import __version__
    if not result.ok or result.nothing_scanned:
        return
    path = _pass_record_path(cfg)
    if path is None or snapshot is None:
        return
    head, dirty, engine_digest = snapshot
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"commit": head, "version": __version__,
                                   "engine": engine_digest, "dirty": dirty}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def evaluate_incremental(cfg: Config) -> tuple[CheckResult, str | None, str]:
    """`check --incremental`: --changed against the last verified commit, else the
    full check; a pass on a clean tree moves the verified commit forward."""
    base, dirty, why = incremental_base(cfg)
    snapshot = _pass_snapshot(cfg)
    result = evaluate(cfg, changed=base, also_changed=dirty) if base else evaluate(cfg)
    record_pass(cfg, result, snapshot)
    return result, base, why


def run_check(cfg: Config, fmt: str = "text", changed: str | None = None,
              incremental: bool = False) -> int:
    """`crossweft check`. Exit 0 = consistent, 1 = problems, 2 = no verdict: the
    model or config could not be read, or nothing was scanned (never a pass)."""
    if changed is not None and fmt == "sarif":
        # code scanning reads a missing alert as fixed: a partial run uploaded as
        # SARIF would close the alerts of every guard it skipped. A usage error,
        # not a check: stderr only, and stdout stays empty so an upload of it
        # fails as "not SARIF" instead of reading as a run with no alerts
        sys.stderr.write("[ERR] --changed cannot be combined with --format sarif: code "
                         "scanning would close the alerts of every guard it skipped -- "
                         "upload a full check\n")
        return 2
    if incremental and fmt == "sarif":
        sys.stderr.write("[ERR] --incremental cannot be combined with --format sarif: it may "
                         "run part of the guards -- upload a full check\n")
        return 2

    def run() -> CheckResult:
        if not incremental:
            return evaluate(cfg, changed)
        result, base, why = evaluate_incremental(cfg)
        result.info.insert(0, f"incremental: changes since the last verified commit {base[:12]}"
                           if base else f"incremental: full check ({why})")
        return result

    if fmt in ("json", "sarif"):
        # a machine format owns stdout: anything else printed while checking (a
        # joint plugin's print) goes to stderr instead of corrupting the document
        with contextlib.redirect_stdout(sys.stderr):
            result = run()
    else:
        result = run()
    print_result(cfg, result, fmt)
    if result.load_error or result.schema_errors:
        return 2
    return 0 if result.ok else 1


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

def _md_escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _mermaid_label(text: str) -> str:
    return (text.replace("\"", "#quot;").replace("\n", "<br/>").replace("[", "(")
            .replace("]", ")").replace("{", "(").replace("}", ")").replace(";", ","))


def _mermaid_id(ident: str) -> str:
    return "n_" + re.sub(r"[^A-Za-z0-9]", "_", ident)


def _seq_text(text: str) -> str:
    return re.sub(r"[;#]", ",", text).replace("\n", " ").strip()


class Renderer:
    """Markdown pages + the HTML viewer, in the configured language. Links to
    repository files are relative to the output directory, whatever its depth."""

    def __init__(self, model: Model) -> None:
        self.m = model
        self.cfg = model.cfg
        self.t = i18n.table(self.cfg.language)
        self.s = self.t["text"]
        self.up = "../" * len(self.cfg.output_dir.parts)
        self.lanes = model.lanes()
        self.lane_name = {lane["id"]: lane["name"] for lane in self.lanes}
        self.carried_by: dict[str, list[tuple[dict, str, str]]] = {}
        for link in model.entities["links"]:
            for data_id in link.get("carries", []):
                self.carried_by.setdefault(data_id, []).append((link, link["from"], link["to"]))
            for data_id in link.get("returns", []):
                self.carried_by.setdefault(data_id, []).append((link, link["to"], link["from"]))
        self.findings_for: dict[str, list[dict]] = {}
        for finding in model.entities["findings"]:
            for ref in finding.get("where", []):
                self.findings_for.setdefault(ref, []).append(finding)

    # --------------------------------------------------------------- helpers
    def repo_link(self, rel: str) -> str:
        return self.up + rel

    def anchor_md(self, anchor: dict) -> str:
        rel = anchor["path"]
        parts = [f"[`{rel}`]({self.repo_link(rel)})"]
        if "find" in anchor:
            parts.append(f"› `{anchor['find']}`")
        elif "regex" in anchor:
            parts.append(f"› regex `{anchor['regex']}`")
        if "note" in anchor:
            parts.append(f"— {anchor['note']}")
        return " ".join(parts).replace("|", "\\|")

    def name(self, block_id: str) -> str:
        return self.m.blocks[block_id]["name"]

    def short(self, block_id: str) -> str:
        block = self.m.blocks[block_id]
        return block.get("short") or block["name"]

    def block_anchor(self, block_id: str) -> str:
        return f"blocks.md#{block_id}"

    def link_label(self, link: dict) -> str:
        return link.get("label") or self.t["transport"].get(link["transport"], link["transport"])

    def header(self, title: str) -> list[str]:
        meta = self.m.meta
        model_rel = self.cfg.model_dir.as_posix()
        model_link = os.path.relpath(self.cfg.model_dir.as_posix(),
                                     self.cfg.output_dir.as_posix()).replace(os.sep, "/") + "/"
        lines = [f"# {title}", "", self.s["generated"].format(model=model_rel,
                                                             model_link=model_link)]
        if meta.get("verified_against"):
            lines.append(self.s["verified"].format(sha=meta["verified_against"][:12],
                                                   date=meta.get("verified_on", "?")))
        return lines + [""]

    def roots_in_lane(self, lane_id: str, statuses: tuple[str, ...]) -> list[dict]:
        return [b for b in self.m.entities["blocks"]
                if not b.get("parent") and b["lane"] == lane_id and b["status"] in statuses]

    def aggregated_edges(self, block_ids: set[str], statuses: tuple[str, ...]
                         ) -> list[tuple[str, str, list[dict]]]:
        edges: dict[tuple[str, str], list[dict]] = {}
        for link in self.m.entities["links"]:
            if link["status"] not in statuses:
                continue
            src, dst = self.m.root_of(link["from"]), self.m.root_of(link["to"])
            if src == dst or src not in block_ids or dst not in block_ids:
                continue
            edges.setdefault((src, dst), []).append(link)
        return [(s, d, links) for (s, d), links in edges.items()]

    def mermaid_overview(self, plane: str | None, statuses: tuple[str, ...]) -> list[str]:
        lines = ["```mermaid", "flowchart LR"]
        shown: set[str] = set()
        for lane in self.lanes:
            if plane is not None and lane.get("plane") != plane:
                continue
            blocks = self.roots_in_lane(lane["id"], statuses)
            if not blocks:
                continue
            lines.append(f"  subgraph {_mermaid_id('lane-' + lane['id'])}"
                         f"[\"{_mermaid_label(lane['name'])}\"]")
            for block in blocks:
                label = self.short(block["id"])
                if block["status"] != "current":
                    label += f"<br/>({self.t['status'][block['status']]})"
                lines.append(f"    {_mermaid_id(block['id'])}[\"{_mermaid_label(label)}\"]")
                shown.add(block["id"])
            lines.append("  end")
        for src, dst, links in self.aggregated_edges(shown, statuses):
            labels = sorted({self.link_label(link) for link in links})
            text = ", ".join(labels[:2]) + (f" +{len(labels) - 2}" if len(labels) > 2 else "")
            arrow = "-.->" if all(link["status"] != "current" for link in links) else "-->"
            lines.append(f"  {_mermaid_id(src)} {arrow}|\"{_mermaid_label(text)}\"| "
                         f"{_mermaid_id(dst)}")
        lines.append("```")
        return lines

    def outgoing(self, block_id: str) -> list[dict]:
        return [l for l in self.m.entities["links"] if l["from"] == block_id]

    def incoming(self, block_id: str) -> list[dict]:
        return [l for l in self.m.entities["links"] if l["to"] == block_id]

    # ----------------------------------------------------------------- pages
    def joint_links(self) -> list[str]:
        """README navigation to the pages of joint-kind plugins."""
        links = [f"[{plugin.title(self.cfg.language)}]({plugin.page})"
                 for plugin in self.m.plugins if plugin.page]
        return ["- " + " · ".join(links)] if links else []

    def joint_page(self, plugin: "joints.Plugin") -> str:
        """A plugin's page under crossweft's own header, so `render` knows it as
        generated and the README links to it."""
        try:
            body = joints.render_markdown(plugin, self.m.sections.get(plugin.kind) or [])
        except ValueError as exc:
            raise ModelError(str(exc)) from exc
        lines = body.splitlines()
        if lines and lines[0].startswith("# "):   # the header carries the title
            lines = lines[1:]
            while lines and not lines[0].strip():
                lines = lines[1:]
        return "\n".join(self.header(plugin.title(self.cfg.language)) + lines)

    def readme(self) -> str:
        m, s, t = self.m, self.s, self.t
        lines = self.header(m.meta["title"])
        lines += [
            s["intro"], "", s["viewer_bullet"], s["pages_bullet"], *self.joint_links(), "",
            f"## {s['how_to_read']}", "",
            f"| {s['term']} | {s['meaning']} |", "|---|---|",
        ]
        for term in ("block", "link", "flow", "data", "finding", "contract"):
            lines.append(f"| {s['term_' + term]} | {s['term_' + term + '_d']} |")
        lines += ["",
                  f"{s['statuses']}: " + ", ".join(f"**{v}** (`{k}`)" for k, v in
                                                   t["status"].items()) + ". "
                  + f"{s['evidence_label']}: " + ", ".join(f"`{k}` — {v}" for k, v in
                                                          t["evidence"].items()) + ".", ""]
        planes = m.meta.get("planes")
        if planes:
            for plane in planes:
                lines += [f"## {plane['name']}", ""]
                lines += self.mermaid_overview(plane["id"], ("current",))
                lines.append("")
        else:
            lines += [f"## {s['overview']}", ""]
            lines += self.mermaid_overview(None, ("current",))
            lines.append("")
        legacy_blocks = [b for b in m.entities["blocks"] if not b.get("parent")
                         and b["status"] != "current"]
        if legacy_blocks:
            lines += [f"## {s['not_current']}", "", s["not_current_d"], "",
                      f"| {s['block']} | {s['status_col']} | {s['lane']} | {s['what_it_is']} |",
                      "|---|---|---|---|"]
            for block in legacy_blocks:
                lines.append(f"| [{_md_escape(block['name'])}]({self.block_anchor(block['id'])}) | "
                             f"{t['status'][block['status']]} | "
                             f"{_md_escape(self.lane_name[block['lane']])} | "
                             f"{_md_escape(block['summary'])} |")
            lines.append("")
        lines += [f"## {s['blocks_by_lane']}", "",
                  f"| {s['lane']} | {s['block']} | {s['does']} | {s['talks_to']} |",
                  "|---|---|---|---|"]
        for lane in self.lanes:
            for block in self.roots_in_lane(lane["id"], ("current",)):
                peers = sorted({self.m.root_of(l["to"]) for l in self._tree_links(block["id"], "out")}
                               | {self.m.root_of(l["from"]) for l in self._tree_links(block["id"], "in")})
                peers = [p for p in peers if p != block["id"]]
                peer_text = ", ".join(f"[{_md_escape(self.short(p))}]({self.block_anchor(p)})"
                                      for p in peers)
                lines.append(f"| {_md_escape(lane['name'])} | [{_md_escape(block['name'])}]"
                             f"({self.block_anchor(block['id'])}) | {_md_escape(block['summary'])} | "
                             f"{peer_text} |")
        lines += ["", f"## {s['flows_heading']}", ""]
        for flow in m.entities["flows"]:
            status = "" if flow["status"] == "current" else f" _({t['status'][flow['status']]})_"
            lines.append(f"- [{flow['name']}](flows.md#{flow['id']}){status} — {flow['summary']}")
        open_findings = [f for f in m.entities["findings"] if f["status"] != "closed"]
        lines += ["", f"## {s['findings_heading']}", "",
                  s["findings_count"].format(open=len(open_findings),
                                             closed=len(m.entities["findings"]) - len(open_findings)),
                  "",
                  f"| {s['id']} | {s['severity_col']} | {s['status_col']} | {s['summary_col']} | "
                  f"{s['next_step']} | {s['owner']} |",
                  "|---|---|---|---|---|---|"]
        for finding in sorted(open_findings, key=lambda f: (SEVERITIES.index(f["severity"]), f["id"])):
            lines.append(f"| [{finding['id']}](findings.md#{finding['id'].lower()}) | "
                         f"{t['severity'][finding['severity']]} | "
                         f"{t['fstatus'][finding['status']]} | "
                         f"{_md_escape(finding['title'])} | {_md_escape(finding.get('next_step', ''))} | "
                         f"{_md_escape(finding.get('owner', ''))} |")
        lines += ["", f"## {s['update_heading']}", ""]
        lines += [s[f"update_{n}"].format(model=self.cfg.model_dir.as_posix()) for n in range(1, 7)]
        current = [l for l in m.entities["links"] if l["status"] == "current"]
        by_enforcement: dict[str, int] = {}
        for link in current:
            key = link.get("contract", {}).get("enforcement") or (
                "compiler" if link["transport"] in COMPILER_CHECKED_TRANSPORTS else "--")
            by_enforcement[key] = by_enforcement.get(key, 0) + 1
        order = ["shared-code", "generated", "schema-tests", "duplicated", "convention", "none",
                 "compiler", "--"]
        counts = {key: len(m.entities[key]) for key in ENTITY_KEYS}
        lines += ["", s["composition"].format(**counts), "",
                  s["contracts_line"].format(items=", ".join(
                      f"`{key}` {by_enforcement[key]}" for key in order if key in by_enforcement)
                      or "-"), ""]
        return "\n".join(lines)

    def _tree_links(self, block_id: str, direction: str) -> list[dict]:
        """Links that leave (out) / enter (in) the subtree rooted at block_id."""
        subtree = {b["id"] for b in self.m.entities["blocks"]
                   if block_id in self.m.ancestors(b["id"])}
        result = []
        for link in self.m.entities["links"]:
            inside_from, inside_to = link["from"] in subtree, link["to"] in subtree
            if direction == "out" and inside_from and not inside_to:
                result.append(link)
            if direction == "in" and inside_to and not inside_from:
                result.append(link)
        return result

    def link_row(self, link: dict, peer: str) -> str:
        contract = link.get("contract", {})
        contract_text = contract.get("name", "")
        if contract:
            contract_text += f" ({contract['enforcement']})"
        carries = ", ".join(f"→ [{self.m.data[d]['name']}](data.md#{d})"
                            for d in link.get("carries", []))
        returned = ", ".join(f"← [{self.m.data[d]['name']}](data.md#{d})"
                             for d in link.get("returns", []))
        carries = ", ".join(part for part in (carries, returned) if part)
        status = "" if link["status"] == "current" else f" _({self.t['status'][link['status']]})_"
        routes = link.get("identifiers", {}).get("route", [])
        route_text = ("<br/>" + "<br/>".join(f"`{r}`" for r in routes[:6])
                      + ("<br/>" + self.s["more"].format(n=len(routes) - 6)
                         if len(routes) > 6 else "")) if routes else ""
        return (f"| [{_md_escape(self.short(peer))}]({self.block_anchor(peer)}) | "
                f"**{_md_escape(self.link_label(link))}**{status} — {_md_escape(link['summary'])}"
                f"{route_text} | {_md_escape(contract_text)} | {_md_escape(link.get('waits', ''))} | "
                f"{_md_escape(link.get('on_error', ''))} | {carries} |")

    def block_section(self, block: dict, level: int) -> list[str]:
        s, t = self.s, self.t
        h = "#" * level
        lines = [f"<a id=\"{block['id']}\"></a>", "", f"{h} {block['name']}", "",
                 block["summary"], ""]
        facts = [f"{s['f_status']}: **{t['status'][block['status']]}**",
                 f"{s['f_kind']}: {t['kind'][block['kind']]}",
                 f"{s['f_lane']}: {self.lane_name[block['lane']]}"]
        if block.get("runs"):
            facts.append(f"{s['f_runs']}: {block['runs']}")
        if block.get("owner"):
            facts.append(f"{s['f_owner']}: {block['owner']}")
        if block.get("evidence"):
            facts.append(f"{s['f_evidence']}: {t['evidence'][block['evidence']]}")
        if block.get("parent"):
            facts.append(f"{s['f_parent']}: [{self.name(block['parent'])}]"
                         f"({self.block_anchor(block['parent'])})")
        lines += ["- " + " · ".join(facts)]
        if block.get("code"):
            lines.append(f"- {s['f_code']}: " + ", ".join(f"[`{p}`]({self.repo_link(p)})"
                                                         for p in block["code"]))
        if block.get("docs"):
            lines.append(f"- {s['f_docs']}: " + ", ".join(f"[`{p}`]({self.repo_link(p)})"
                                                         for p in block["docs"]))
        if block.get("requirements"):
            lines.append(f"- {s['f_requirements']}: " + ", ".join(block["requirements"]))
        children = self.m.children(block["id"])
        if children:
            lines.append(f"- {s['f_inside']}: " + ", ".join(
                f"[{self.short(c)}]({self.block_anchor(c)})" for c in children))
        if block.get("notes"):
            lines += ["", f"> {block['notes']}"]
        lines.append("")
        out_links, in_links = self.outgoing(block["id"]), self.incoming(block["id"])
        header = (f"| {s['col_peer']} | {s['col_what']} | {s['col_contract']} | "
                  f"{s['col_waits']} | {s['col_error']} | {s['col_data']} |")
        if out_links:
            lines += [s["sends"], "", header, "|---|---|---|---|---|---|"]
            lines += [self.link_row(link, link["to"]) for link in out_links]
            lines.append("")
        if in_links:
            lines += [s["receives"], "", header, "|---|---|---|---|---|---|"]
            lines += [self.link_row(link, link["from"]) for link in in_links]
            lines.append("")
        if block.get("waits_for"):
            lines += [s["waits_before"], ""]
            for wait in block["waits_for"]:
                text = f"- {wait['what']}"
                if wait.get("on_missing"):
                    text += f" — {s['if_missing']}: {wait['on_missing']}"
                lines.append(text)
            lines.append("")
        produced = [d for d in self.m.entities["data"] if d["origin"] == block["id"]]
        received = sorted({d for l in in_links for d in l.get("carries", [])}
                          | {d for l in out_links for d in l.get("returns", [])})
        if produced or received:
            if produced:
                lines.append(f"- {s['creates_data']}: " + ", ".join(
                    f"[{d['name']}](data.md#{d['id']})" for d in produced))
            if received:
                lines.append(f"- {s['gets_data']}: " + ", ".join(
                    f"[{self.m.data[d]['name']}](data.md#{d})" for d in received))
            lines.append("")
        findings = self.findings_for.get(f"block:{block['id']}", [])
        if findings:
            lines.append(f"- {s['findings_label']}: " + ", ".join(
                f"[{f['id']}](findings.md#{f['id'].lower()}) {f['title']}" for f in findings))
            lines.append("")
        if block.get("anchors"):
            lines += [f"<details><summary>{s['anchors_in_code']}</summary>", ""]
            lines += [f"- {self.anchor_md(a)}" for a in block["anchors"]]
            lines += ["", "</details>", ""]
        for child in children:
            lines += self.block_section(self.m.blocks[child], min(level + 1, 6))
        return lines

    def blocks_page(self) -> str:
        lines = self.header(self.s["blocks_title"])
        lines += [self.s["blocks_intro"], ""]
        for lane in self.lanes:
            roots = [b for b in self.m.entities["blocks"] if not b.get("parent")
                     and b["lane"] == lane["id"]]
            if not roots:
                continue
            lines += [f"## {lane['name']}", ""]
            for block in roots:
                lines += self.block_section(block, 3)
        return "\n".join(lines)

    def sequence(self, flow: dict) -> list[str]:
        participants: list[str] = []
        for step in flow["steps"]:
            link = self.m.links[step["link"]]
            for end in (link["from"], link["to"]):
                if end not in participants:
                    participants.append(end)
        lines = ["```mermaid", "sequenceDiagram", "  autonumber"]
        for block_id in participants:
            lines.append(f"  participant {_mermaid_id(block_id)} as {_seq_text(self.short(block_id))}")
        for step in flow["steps"]:
            link = self.m.links[step["link"]]
            src, dst = (link["to"], link["from"]) if step.get("reply") else (link["from"], link["to"])
            arrow = "-->>" if step.get("reply") else "->>"
            lines.append(f"  {_mermaid_id(src)}{arrow}{_mermaid_id(dst)}: {_seq_text(step['action'])}")
            if step.get("waits"):
                lines.append(f"  Note over {_mermaid_id(src)}: "
                             f"{_seq_text(self.s['waits_note'].format(w=step['waits']))}")
        lines.append("```")
        return lines

    def flows_page(self) -> str:
        s, t = self.s, self.t
        lines = self.header(s["flows_title"])
        lines += [s["flows_intro"], ""]
        for flow in self.m.entities["flows"]:
            lines += [f"<a id=\"{flow['id']}\"></a>", "", f"## {flow['name']}", "",
                      flow["summary"], ""]
            facts = [f"{s['f_status']}: **{t['status'][flow['status']]}**"]
            if flow.get("evidence"):
                facts.append(f"{s['f_evidence']}: {t['evidence'][flow['evidence']]}")
            if flow.get("requirements"):
                facts.append(f"{s['f_requirements']}: " + ", ".join(flow["requirements"]))
            lines.append("- " + " · ".join(facts))
            if flow.get("trigger"):
                lines.append(f"- {s['f_trigger']}: {flow['trigger']}")
            if flow.get("result"):
                lines.append(f"- {s['f_result']}: {flow['result']}")
            lines.append("")
            lines += self.sequence(flow)
            lines += ["", f"| {s['col_no']} | {s['col_who']} | {s['col_action']} | "
                          f"{s['col_wait']} | {s['col_then']} |", "|---|---|---|---|---|"]
            for index, step in enumerate(flow["steps"], 1):
                link = self.m.links[step["link"]]
                src, dst = ((link["to"], link["from"]) if step.get("reply")
                            else (link["from"], link["to"]))
                lines.append(f"| {index} | [{_md_escape(self.short(src))}]({self.block_anchor(src)}) → "
                             f"[{_md_escape(self.short(dst))}]({self.block_anchor(dst)}) "
                             f"<sub>{_md_escape(self.link_label(link))}</sub> | "
                             f"{_md_escape(step['action'])} | {_md_escape(step.get('waits', ''))} | "
                             f"{_md_escape(step.get('then', ''))} |")
            if flow.get("notes"):
                lines += ["", f"> {flow['notes']}"]
            lines.append("")
        return "\n".join(lines)

    def data_page(self) -> str:
        s, t = self.s, self.t
        lines = self.header(s["data_title"])
        lines += [s["data_intro"], "",
                  f"| {s['col_data_name']} | {s['col_kind']} | {s['col_created']} | "
                  f"{s['col_carried']} | {s['col_received']} | {s['col_stored']} |",
                  "|---|---|---|---|---|---|"]
        for item in self.m.entities["data"]:
            hops = self.carried_by.get(item["id"], [])
            path = "<br/>".join(f"{_md_escape(self.short(src))} → {_md_escape(self.short(dst))} "
                                f"<sub>{_md_escape(self.link_label(l))}</sub>" for l, src, dst in hops)
            receivers = sorted({dst for _, _, dst in hops} | set(item.get("consumers", [])))
            stored = "<br/>".join(f"{_md_escape(self.short(x['block']))}: {_md_escape(x['where'])}"
                                  for x in item.get("stored_in", []))
            lines.append(f"| [{_md_escape(item['name'])}](#{item['id']}) | "
                         f"{t['data_kind'][item['kind']]} | "
                         f"[{_md_escape(self.short(item['origin']))}]"
                         f"({self.block_anchor(item['origin'])}) | "
                         f"{path} | {', '.join(_md_escape(self.short(r)) for r in receivers)} | "
                         f"{stored} |")
        lines.append("")
        for item in self.m.entities["data"]:
            lines += [f"<a id=\"{item['id']}\"></a>", "", f"### {item['name']}", "",
                      item["summary"], ""]
            facts = [f"{s['f_kind']}: {t['data_kind'][item['kind']]}",
                     f"{s['f_created']}: [{self.name(item['origin'])}]"
                     f"({self.block_anchor(item['origin'])})"]
            if item.get("sensitivity"):
                facts.append(f"{s['f_sensitivity']}: {item['sensitivity']}")
            lines.append("- " + " · ".join(facts))
            for extra in item.get("also_from", []):
                lines.append(f"- {s['also_in']}: {self.name(extra['block'])} — {extra['reason']}")
            if item.get("defined_in"):
                lines.append(f"- {s['format_defined']}: " + "; ".join(
                    self.anchor_md(a) for a in item["defined_in"]))
            if item.get("notes"):
                lines += ["", f"> {item['notes']}"]
            lines.append("")
        return "\n".join(lines)

    def findings_page(self) -> str:
        s, t = self.s, self.t
        lines = self.header(s["findings_title"])
        lines += [s["findings_intro"], "",
                  f"| {s['id']} | {s['severity_col']} | {s['status_col']} | {s['col_fkind']} | "
                  f"{s['summary_col']} | {s['owner']} |", "|---|---|---|---|---|---|"]
        ordered = sorted(self.m.entities["findings"],
                         key=lambda f: (FINDING_STATUSES.index(f["status"]),
                                        SEVERITIES.index(f["severity"]), f["id"]))
        for finding in ordered:
            lines.append(f"| [{finding['id']}](#{finding['id'].lower()}) | "
                         f"{t['severity'][finding['severity']]} | "
                         f"{t['fstatus'][finding['status']]} | {finding['kind']} | "
                         f"{_md_escape(finding['title'])} | "
                         f"{_md_escape(finding.get('owner', ''))} |")
        lines.append("")
        for finding in ordered:
            lines += [f"<a id=\"{finding['id'].lower()}\"></a>", "",
                      f"### {finding['id']} — {finding['title']}", "", finding["summary"], ""]
            facts = [f"{s['f_severity']}: **{t['severity'][finding['severity']]}**",
                     f"{s['f_status']}: {t['fstatus'][finding['status']]}",
                     f"{s['f_fkind']}: `{finding['kind']}`"]
            if finding.get("since"):
                facts.append(f"{s['f_since']} {finding['since']}")
            lines.append("- " + " · ".join(facts))
            if finding.get("where"):
                refs = []
                for ref in finding["where"]:
                    kind, ident = ref.split(":", 1)
                    target = {"block": f"blocks.md#{ident}", "flow": f"flows.md#{ident}",
                              "data": f"data.md#{ident}"}.get(kind)
                    label = {"block": lambda i: self.name(i),
                             "data": lambda i: self.m.data[i]["name"],
                             "flow": lambda i: self.m.flows[i]["name"],
                             "link": lambda i: f"{self.short(self.m.links[i]['from'])} → "
                                               f"{self.short(self.m.links[i]['to'])}"
                             }.get(kind, lambda i: f"{kind}:{i}")(ident)
                    refs.append(f"[{label}]({target})" if target else label)
                lines.append(f"- {s['f_where']}: " + ", ".join(refs))
            for key, label in (("impact", "f_impact"), ("next_step", "f_next"),
                               ("owner", "f_owner")):
                if finding.get(key):
                    lines.append(f"- {s[label]}: {finding[key]}")
            if finding.get("defer_reason"):
                lines.append(f"- {s['f_defer']}: `{finding['defer_reason']}`")
            if finding.get("requirements"):
                lines.append(f"- {s['f_requirements']}: " + ", ".join(finding["requirements"]))
            if finding.get("closed_by"):
                lines.append(f"- {s['f_closed']}: {finding['closed_by']}")
            if finding.get("detected_by"):
                lines.append(f"- {s['f_detector']}: " + ", ".join(f"`{k}`"
                                                                 for k in finding["detected_by"]))
            if finding.get("evidence"):
                lines += [f"- {s['f_proof']}:"] + [f"  - {self.anchor_md(a)}"
                                                   for a in finding["evidence"]]
            if finding.get("notes"):
                lines += ["", f"> {finding['notes']}"]
            lines.append("")
        return "\n".join(lines)

    def viewer(self, template: str) -> str:
        t = self.t
        payload = {
            "meta": {k: self.m.meta.get(k) for k in ("title", "verified_against", "verified_on",
                                                     "lanes", "planes")},
            "blocks": self.m.entities["blocks"],
            "links": self.m.entities["links"],
            "flows": self.m.entities["flows"],
            "data": self.m.entities["data"],
            "findings": self.m.entities["findings"],
            "i18n": {"status": t["status"], "evidence": t["evidence"], "severity": t["severity"],
                     "fstatus": t["fstatus"], "transport": t["transport"], "kind": t["kind"],
                     "dataKind": t["data_kind"], "ui": t["ui"]},
            "lang": self.cfg.language,
        }
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        blob = blob.replace("</", "<\\/")
        if "/*__CROSSWEFT_DATA__*/" not in template:
            raise ModelError("viewer template lacks the /*__CROSSWEFT_DATA__*/ placeholder")
        title = html.escape(self.m.meta.get("title", t["ui"]["title"]))
        return (template.replace("/*__CROSSWEFT_DATA__*/", blob)
                .replace("__CROSSWEFT_TITLE__", title)
                .replace("__CROSSWEFT_LANG__", self.cfg.language))

    def render_all(self, template: str) -> dict[str, str]:
        pages = {
            "README.md": self.readme(),
            "blocks.md": self.blocks_page(),
            "flows.md": self.flows_page(),
            "data.md": self.data_page(),
            "findings.md": self.findings_page(),
            "viewer.html": self.viewer(template),
        }
        for plugin in self.m.plugins:
            if plugin.page:
                pages[plugin.page] = self.joint_page(plugin)
        return {name: (text if text.endswith("\n") else text + "\n") for name, text in pages.items()}


def generated_pages(model: Model) -> dict[str, str]:
    template = model.cfg.template_path.read_text(encoding="utf-8")
    return Renderer(model).render_all(template)


def existing_page(cfg: Config, name: str) -> str | None:
    """The current text of generated file `name` in output_dir, None when there
    is none. A file that is not UTF-8 was not written by crossweft: ModelError."""
    target = cfg.abs(cfg.output_dir) / name
    if not target.is_file():
        return None
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ModelError(f"{(cfg.output_dir / name).as_posix()} is not UTF-8 text ({exc.reason} "
                         f"at byte {exc.start}) -- crossweft did not generate it; rename it or "
                         f"choose another output_dir in {CONFIG_FILE}") from exc


def joint_generated_files(model: Model) -> dict[str, str]:
    """Files outside output_dir that joint plugins generate (repo-relative)."""
    cfg = model.cfg
    protected = [
        (CONFIG_FILE, "the configuration"),
        (cfg.model_dir.as_posix(), "every file in model_dir is read as the model"),
        (cfg.output_dir.as_posix(), "output_dir holds crossweft's own pages"),
        (cfg.lock_file.as_posix(), "the lock file is crossweft's own"),
    ]
    if cfg.joints_dir is not None and cfg.joints_dir.as_posix() != ".":
        protected.append((cfg.joints_dir.as_posix(), "joints.dir holds the plugins' code"))
    files: dict[str, str] = {}
    seen: dict[str, str] = {}
    for plugin in model.plugins:
        try:
            produced = joints.generated(plugin, model.root, model.sections.get(plugin.kind) or [],
                                        protected)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:  # noqa: BLE001 -- a generator failure is an error, not a skip
            raise ModelError(f"joint kind '{plugin.kind}': generated failed: {exc}") from exc
        for rel in produced:
            if rel.casefold() in seen:
                raise ModelError(f"{rel} is generated by two joint plugins")
            seen[rel.casefold()] = plugin.kind
        files.update(produced)
    return files


def _current_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ModelError(f"{path} is not UTF-8 text ({exc.reason})") from exc


def stale_generated_files(model: Model) -> list[str]:
    stale = []
    for name, text in generated_pages(model).items():
        if existing_page(model.cfg, name) != text:
            stale.append(name)
    for rel, text in joint_generated_files(model).items():
        if _current_text(model.root / rel) != text:
            stale.append(rel)
    return stale


def _generated_line_res() -> list[re.Pattern]:
    """The header line every generated page carries (Renderer.header), in each
    language, with any model path: a page stays ours after model_dir moves."""
    fill = {"{model}": r"[^`\n]+", "{model_link}": r"[^)\n]+"}
    return [re.compile("".join(fill.get(part, re.escape(part)) for part in
                               re.split(r"(\{model\}|\{model_link\})",
                                        i18n.table(language)["text"]["generated"])))
            for language in sorted(i18n.LANGUAGES)]


GENERATED_LINE_RES = _generated_line_res()
# Renderer.header writes "# title", "", then the marker line
GENERATED_MARKER_LINE = 2


def is_generated(name: str, text: str) -> bool:
    """True when an existing file carries crossweft's generated-file marker --
    for a Markdown page the exact header line, where render puts it. A page
    that merely mentions `crossweft render` is someone's own writing."""
    if name.endswith(".html"):
        return ('<meta name="generator" content="crossweft">' in text
                or '(crossweft)">' in text                  # 0.1.0 pre-release viewers
                or "scripts/system_map.py" in text)         # v0 engine viewers (upgrade path)
    lines = text.splitlines()
    if len(lines) <= GENERATED_MARKER_LINE:
        return False
    marker = lines[GENERATED_MARKER_LINE]
    return (any(pattern.fullmatch(marker) for pattern in GENERATED_LINE_RES)
            or (marker.startswith("> ") and "system_map.py render" in marker))  # v0 engine pages


def run_render(cfg: Config, check_only: bool) -> int:
    try:
        model = load_model(cfg)
    except ModelError as exc:
        out(f"[ERR] {exc}")
        return 2
    errors = validate_schema(model)
    if errors:
        out(f"[FAIL] model schema: {len(errors)} error(s) -- run `crossweft check` first")
        for error in errors[:20]:
            out(f"   - {error}")
        return 1
    try:
        pages = generated_pages(model)
        extra = joint_generated_files(model)
    except (OSError, ModelError) as exc:
        out(f"[ERR] render failed: {exc}")
        return 2
    out_dir = cfg.abs(cfg.output_dir)
    stale = []
    for rel, text in extra.items():
        target = cfg.root / rel
        try:
            safe_target(cfg, target)
            current = _current_text(target)
        except (OSError, ModelError) as exc:
            out(f"[ERR] {exc}")
            return 2
        if current != text:
            stale.append(rel)
            if not check_only:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(text.encode("utf-8"))
    for name, text in pages.items():
        target = out_dir / name
        try:
            current = existing_page(cfg, name)
        except (OSError, ModelError) as exc:
            out(f"[ERR] {exc}")
            return 2
        if current != text:
            stale.append(name)
            if not check_only:
                try:
                    safe_target(cfg, target)
                except ModelError as exc:
                    out(f"[ERR] {exc}")
                    return 2
                if current is not None and not is_generated(name, current):
                    out(f"[ERR] {cfg.output_dir.as_posix()}/{name} exists and was not generated "
                        "by crossweft -- refusing to overwrite it; move it or choose another "
                        f"output_dir in {CONFIG_FILE}")
                    return 2
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(text.encode("utf-8"))
    if check_only:
        if stale:
            out(f"[FAIL] generated docs are stale: {', '.join(stale)} -- run `crossweft render`")
            return 1
        out(f"[OK] generated docs up to date ({len(pages) + len(extra)} files)")
        return 0
    out(f"[OK] rendered {len(stale)} changed file(s) of {len(pages) + len(extra)} into "
        f"{cfg.output_dir.as_posix()}: {', '.join(stale) or '-'}")
    return 0



# --------------------------------------------------------------------------- #
# show
# --------------------------------------------------------------------------- #

def run_show(cfg: Config, ident: str) -> int:
    try:
        model = load_model(cfg)
    except ModelError as exc:
        out(f"[ERR] {exc}")
        return 2
    # accept the `kind:id` form check, impact and the hooks print, including a
    # problem key's trailing parts (join:api-version:ff55dadb -> join api-version)
    kinds = ENTITY_KEYS
    prefix, _, rest = ident.partition(":")
    singular = {k[:-1] if k.endswith("s") else k: k for k in ENTITY_KEYS}
    if rest and prefix in singular:
        kinds = (singular[prefix],)
        ident = rest.split(":")[0]
    found = False
    for kind in kinds:
        entity = model.by_id[kind].get(ident)
        if not entity:
            continue
        found = True
        out(f"== {kind[:-1] if kind.endswith('s') else kind}:{ident}  ({model.where(entity)})")
        utf8 = (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") == "utf8"
        out(json.dumps(entity, ensure_ascii=not utf8, indent=2))
        if kind == "blocks":
            for link in model.entities["links"]:
                if link["from"] == ident:
                    out(f"  -> {link['to']:<32} [{link['transport']}] link:{link['id']}")
                if link["to"] == ident:
                    out(f"  <- {link['from']:<32} [{link['transport']}] link:{link['id']}")
            for child in model.children(ident):
                out(f"  contains {child}")
            for item in model.entities["data"]:
                if item["origin"] == ident:
                    out(f"  creates data:{item['id']}")
        if kind == "data":
            for link in model.entities["links"]:
                if ident in link.get("carries", []):
                    out(f"  via link:{link['id']}  {link['from']} -> {link['to']}")
    if not found:
        known = sorted({i for kind in kinds for i in model.by_id[kind]})
        close = difflib.get_close_matches(ident, known, n=5)
        out(f"[ERR] no entity with id '{ident}'"
            + (f" -- did you mean: {', '.join(close)}?" if close else ""))
        return 1
    return 0


def _trace_row(trace: ui_actions.Trace) -> dict:
    return {"control": trace.control, "dynamic": trace.dynamic, "path": trace.place.rel,
            "line": trace.place.line, "status": trace.status, "detail": trace.detail,
            "steps": [{"hop": step.hop, "key": step.key, "kind": step.kind,
                       "path": step.place.rel if step.place else None,
                       "line": step.place.line if step.place else None, "value": step.value}
                      for step in trace.steps]}


def _fallback_checker(cfg: Config):
    """A checker that has run only the fallback check; (checker, None) or (None, error)."""
    if not cfg.fallbacks_include:
        return None, f"{CONFIG_FILE} declares no fallbacks -- add \"fallbacks\": {{\"include\": [...]}}"
    try:
        model = load_model(cfg)
    except ModelError as exc:
        return None, str(exc)
    checker = Checker(model)
    checker.check_fallbacks()
    if not hasattr(checker, "fallback_report"):
        bad = [p.message for p in checker.problems if p.key.startswith("fallback-config:")]
        return None, "; ".join(bad) or "the fallback registry could not be read"
    return checker, None


def _fallback_rows(checker) -> list[dict]:
    report = checker.fallback_report
    rows = []
    for entry in report["entries"]:
        sites = report["match"].by_entry.get(entry["id"], [])
        calls = [{"path": rel, "line": line} for rel, found in report["uses"].items()
                 for name, line in found if entry.get("wrapper") and name == entry["symbol"]]
        rows.append({**{k: entry.get(k) for k in ("id", "status", "kind", "path", "symbol",
                                                   "behavior", "reason", "by", "at",
                                                   "proposed_by")},
                     "wrapper": bool(entry.get("wrapper")),
                     "lines": [s.line for s in sites], "text": sites[0].text if sites else "",
                     "changed": entry["id"] in report["match"].changed, "calls": calls})
    for key, site in fallbacks.keyed(report["match"].unregistered):
        rows.append({"id": None, "key": key, "status": "unregistered", "kind": site.kind,
                     "path": site.path, "symbol": site.symbol, "lines": [site.line],
                     "text": site.text, "behavior": fallbacks.KINDS[site.kind], "reason": "",
                     "wrapper": False, "changed": False, "calls": []})
    return rows


def _fallback_markdown(rows: list[dict]) -> str:
    order = ("unregistered", "rejected", "proposed", "approved")
    lines = ["# Fallbacks", "",
             "Every place where the code carries on after a failure, and the decision on it. "
             "Generated by `crossweft fallbacks --md`; do not edit.", ""]
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in order}
    lines.append(" | ".join(f"{s}: {counts[s]}" for s in order))
    for status in order:
        group = [r for r in rows if r["status"] == status]
        if not group:
            continue
        lines += ["", f"## {status} ({len(group)})", "",
                  "| id | where | kind | what happens | reason |", "|---|---|---|---|---|"]
        for r in sorted(group, key=lambda r: (r["path"], r["lines"][:1])):
            where = f"`{r['path']}:{r['lines'][0] if r['lines'] else '?'}` {r['symbol']}"
            kind = r["kind"] + (" (wrapper, %d calls)" % len(r["calls"]) if r["wrapper"] else "")
            cell = lambda s: (s or "").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {r['id'] or r.get('key')} | {cell(where)} | {kind} | "
                         f"{cell(r['behavior'])} | {cell(r['reason'])} |")
    return "\n".join(lines) + "\n"


def run_fallbacks(cfg: Config, status: str | None = None, as_json: bool = False,
                  markdown: str | None = None) -> int:
    """The map of fallbacks: 0 when `check` raises no fallback problem, 1 when it does,
    2 when nothing could be scanned."""
    checker, error = _fallback_checker(cfg)
    if checker is None:
        out(f"[ERR] {error}")
        return 2
    rows = _fallback_rows(checker)
    problems = [p for p in checker.problems if p.key.startswith(("fallback:", "fallback-config:"))]
    shown = [r for r in rows if status in (None, r["status"])]
    if markdown:
        target = cfg.abs(Path(markdown))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_fallback_markdown(rows), encoding="utf-8")
    if as_json:
        print(json.dumps({"ok": not problems, "fallbacks": shown,
                          "problems": [{"key": p.key, "message": p.message, "path": p.path,
                                        "line": p.line} for p in problems]}, indent=2))
    else:
        for r in shown:
            where = f"{r['path']}:{','.join(map(str, r['lines'])) or '?'}"
            label = r["id"] or r["key"]
            flag = " CHANGED" if r["changed"] else ""
            out(f"[{r['status']}{flag}] {label}  {where}  {r['symbol']}  {r['kind']}"
                + (f"  wrapper, {len(r['calls'])} call(s)" if r["wrapper"] else ""))
            out(f"    {r['behavior']}" + (f" -- {r['reason']}" if r["reason"] else ""))
        counts = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        out(f"fallbacks: {len(rows)} -- " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        for p in problems:
            out(f"[ERR] {p.key}: {p.message}")
    return 1 if problems else 0


def run_fallback_propose(cfg: Config, targets: list[str], every: bool, behavior: str | None,
                         reason: str, by: str | None, wrapper: bool) -> int:
    """Register sites (by key) or re-open entries (by id) as proposed: the agent's step.
    Nothing it writes is agreed until the owner decides."""
    checker, error = _fallback_checker(cfg)
    if checker is None:
        out(f"[ERR] {error}")
        return 2
    if not reason.strip():
        out("[ERR] --reason must say why the code carries on here")
        return 2
    report = checker.fallback_report
    entries = [dict(e) for e in report["entries"]]
    last_id = report["last_id"]
    by_id = {e["id"]: e for e in entries}
    by_key = dict(fallbacks.keyed(report["match"].unregistered))
    wanted = list(by_key) if every else targets
    if not wanted:
        out("[ERR] name site keys or ids, or pass --all (nothing is unregistered)" if every
            else "[ERR] name site keys or ids, or pass --all")
        return 2
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    done = []
    for target in wanted:
        if target in by_id:
            entry = by_id[target]
            sites = report["match"].by_entry.get(target, [])
            if not sites:
                out(f"[ERR] {target}: its site is gone from the code -- remove the entry instead")
                return 1
            entry.update(digest=sites[0].digest, symbol=sites[0].symbol, status="proposed",
                         reason=reason.strip(), proposed_by=by or "agent", at=stamp,
                         rev=entry.get("rev", 0) + 1)
            if behavior:
                entry["behavior"] = behavior.strip()
            for field in ("by", "sig"):
                entry.pop(field, None)
            done.append(target)
        elif target in by_key:
            site = by_key[target]
            ident = fallbacks.next_id(last_id)
            last_id += 1
            entry = {"id": ident, "path": site.path, "symbol": site.symbol,
                     "kind": site.kind, "digest": site.digest,
                     "behavior": (behavior or f"{fallbacks.KINDS[site.kind]} ({site.text})").strip(),
                     "reason": reason.strip(), "status": "proposed",
                     "proposed_by": by or "agent", "at": stamp, "rev": 1}
            if wrapper:
                entry["wrapper"] = True
            entries.append(entry)
            done.append(f"{entry['id']} <- {target}")
        else:
            out(f"[ERR] {target}: neither an unregistered site key nor a registry id "
                "(run `crossweft fallbacks` for both)")
            return 1
    fallbacks.save_registry(cfg.abs(cfg.fallbacks_registry), entries, last_id)
    for line in done:
        out(f"[OK] proposed {line}")
    out(f"{len(done)} fallback(s) wait for the owner's decision: crossweft fallbacks decide <id> "
        "approve|reject --reason '...' --by <name>")
    return 0


def run_fallback_decide(cfg: Config, ident: str, verdict: str, reason: str, by: str,
                        key: str | None) -> int:
    """The owner's step: approve or reject one proposed entry; signed when
    fallbacks.signers is set."""
    checker, error = _fallback_checker(cfg)
    if checker is None:
        out(f"[ERR] {error}")
        return 2
    if verdict not in ("approve", "reject") or not reason.strip() or not by.strip():
        out("[ERR] decide <id> approve|reject --reason '...' --by <name>")
        return 2
    report = checker.fallback_report
    entries = [dict(e) for e in report["entries"]]
    entry = next((e for e in entries if e["id"] == ident), None)
    if entry is None:
        out(f"[ERR] no registry entry {ident}")
        return 1
    if ident in report["match"].changed or not report["match"].by_entry.get(ident):
        out(f"[ERR] {ident}: its code is not what was proposed (changed or gone) -- propose "
            "it again first, so the decision is about the code as it is now")
        return 1
    out(f"{ident}  {entry['path']}  {entry['symbol']}  {entry['kind']}")
    out(f"  what happens: {entry['behavior']}")
    out(f"  proposed: {entry['reason']} ({entry.get('proposed_by', '?')})")
    entry.update(status="approved" if verdict == "approve" else "rejected",
                 reason=reason.strip(), by=by.strip(), rev=entry.get("rev", 0) + 1,
                 at=datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    entry.pop("sig", None)
    if cfg.fallbacks_signers is not None:
        if not key:
            out(f"[ERR] {CONFIG_FILE} sets fallbacks.signers: sign with --key <the owner's key>")
            return 2
        try:
            entry["sig"] = fallbacks.sign(entry, Path(key).expanduser())
        except fallbacks.RegistryError as exc:
            out(f"[ERR] {exc}")
            return 1
        signers = cfg.fallbacks_signers if cfg.fallbacks_signers.is_absolute() \
            else cfg.abs(cfg.fallbacks_signers)
        problem = fallbacks.verify(entry, signers)
        if problem:
            out(f"[ERR] the new signature {problem} against {cfg.fallbacks_signers.as_posix()}")
            return 1
    fallbacks.save_registry(cfg.abs(cfg.fallbacks_registry), entries, report["last_id"])
    out(f"[OK] {ident} {entry['status']}")
    return 0


def run_actions(cfg: Config, ident: str | None = None, as_json: bool = False) -> int:
    """Print the chain of every control: which hop takes it, and where. 0 when
    `check` would raise no problem for the selected entries, 1 when it would, 2
    when nothing could be traced (an invalid model, or no ui_actions)."""
    try:
        model = load_model(cfg)
    except ModelError as exc:
        out(f"[ERR] {exc}")
        return 2
    errors = validate_schema(model)
    if errors:
        for error in errors[:20]:
            out(f"[ERR] {error}")
        return 2
    wanted = ident.split(":", 1)[1] if ident and ident.startswith("ui:") else ident
    entries = [e for e in model.entities["ui_actions"] if wanted in (None, e["id"])]
    if not entries:
        out(f"[ERR] no ui_actions entry '{wanted}'" if wanted else
            "[ERR] the model declares no ui_actions -- nothing to trace")
        return 2
    checker = Checker(model)
    checker.check_routes()
    checker.check_ui_actions()
    rows = []
    for entry in entries:
        traces = checker.ui_traces.get(entry["id"])
        rows.append({"id": entry["id"], "name": entry["name"], "routes": bool(entry.get("routes")),
                     "traced": traces is not None,
                     "controls": [_trace_row(t) for t in traces or []]})
    # the same problems `check` raises for these entries, so the two never disagree
    owners = tuple(f"{prefix}ui:{e['id']}:" for e in entries for prefix in ("", "anchor:"))
    problems = [p for p in checker.problems if p.key.startswith(owners)]
    ok = all(row["traced"] for row in rows) and not problems
    if as_json:
        print(json.dumps({"ok": ok, "ui_actions": rows,
                          "problems": [{"key": p.key, "message": p.message} for p in problems]},
                         indent=2))
    else:
        for row in rows:
            out(f"ui:{row['id']}  {row['name']}")
            if not row["traced"]:
                out("  [ERR] not traced: its controls extracted nothing")
            for control in row["controls"]:
                name = control["control"] + ("*" if control["dynamic"] else "")
                out(f"  [{control['status']}] {name}  ({control['path']}:{control['line']})")
                for step in control["steps"]:
                    where = f"  ({step['path']}:{step['line']})" if step["path"] else ""
                    lead = f" -> {step['value']}" if step["value"] else ""
                    kind = {"rule": "prefix rule ", "route": "declared route "}.get(step["kind"], "")
                    out(f"      {step['hop']}: {kind}{step['key']}{lead}{where}")
                if control["detail"]:
                    out(f"      !! {control['detail']}")
        for problem in problems:
            if not problem.key.startswith(tuple(f"ui:{e['id']}:{kind}:" for e in entries
                                                for kind in ("dead", "unrouted"))):
                out(f"[ERR] {problem.message}")   # dead/unrouted are shown under their control
        if not ok and feedback_line(cfg, "actions"):
            out(feedback_line(cfg, "actions"))
    return 0 if ok else (2 if not all(row["traced"] for row in rows) else 1)


# --------------------------------------------------------------------------- #
# impact -- which seams a change touches
# --------------------------------------------------------------------------- #

class ImpactError(Exception):
    """The change set could not be determined (git failed, unknown base, path outside)."""


def _git_lines(root: Path, *args: str) -> list[str]:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        detail = proc.stderr.strip() or f"exit {proc.returncode}"
        raise ImpactError(f"git {' '.join(args)} failed: {detail}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _git_paths(root: Path, *args: str) -> list[str]:
    """Paths from a git command, NUL-separated and unquoted (non-ASCII names
    stay readable), relative to `root` even when root is a subdirectory of the
    git work tree (a monorepo)."""
    proc = subprocess.run(["git", "-C", str(root), "-c", "core.quotePath=false", *args, "-z"],
                          capture_output=True)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
        raise ImpactError(f"git {' '.join(args)} failed: {detail}")
    return [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]


def changed_files(root: Path, base: str | None) -> tuple[list[str], str]:
    """Changed paths: tracked changes in the working tree (staged or not) plus
    untracked files, measured against HEAD -- or, with `base`, against the
    merge-base of `base` and HEAD, so the committed work of a branch counts too.
    Renames are reported as delete + add: the OLD path may be the anchored one."""
    if base:
        merge_base = _git_lines(root, "merge-base", base, "HEAD")
        if not merge_base:
            raise ImpactError(f"no merge-base between {base} and HEAD")
        ref = merge_base[0]
        label = f"merge-base({base}, HEAD) {ref[:12]} .. working tree"
    else:
        ref = "HEAD"
        label = "HEAD .. working tree"
    # Outside a work tree `git diff` silently switches to --no-index mode and
    # prints its usage; say what is wrong and what to pass instead.
    probe = subprocess.run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        raise ImpactError(f"{root} is not inside a git work tree, so there are no changes to read; "
                          "name the changed files instead: `crossweft impact <file> ...`")
    files = set(_git_paths(root, "diff", "--name-only", "--relative", "--no-renames", ref))
    files.update(_git_paths(root, "ls-files", "--others", "--exclude-standard"))
    return sorted(files), label


def change_scope(cfg: Config, rev: str, also: tuple | list = ()) -> tuple[list[str] | None, str | None]:
    """`check --changed REV`: (paths whose guards must run, None), or (None, why)
    when the change set cannot be trusted and the full check must run instead.

    The scope is every path that differs between REV and the working tree
    (committed, staged or not), every untracked file, and every git-ignored file
    or directory -- git cannot say whether an ignored file changed, so a guard
    reading one always runs. The full check runs when git cannot list the
    changes, or when crossweft.json, a model file, the lock file or a joint
    plugin is in that set: a changed map can move any guard onto any file."""
    root = cfg.root
    if not rev or rev.startswith("-"):
        return None, f"'{rev}' is not a revision"
    try:
        commit = _git_lines(root, "rev-parse", "--verify", "--quiet", rev + "^{commit}")
    except ImpactError:
        commit = []
    if not commit:
        return None, f"'{rev}' is not a commit git can resolve here"
    try:
        paths = set(_git_paths(root, "diff", "--name-only", "--relative", "--no-renames",
                               commit[0]))
        paths.update(_git_paths(root, "ls-files", "--others", "--exclude-standard"))
        paths.update(p.rstrip("/") for p in _git_paths(root, "ls-files", "--others", "--ignored",
                                                       "--exclude-standard", "--directory"))
        paths.update(also)   # paths the caller knows must rerun (see record_pass)
    except ImpactError as exc:
        return None, f"the changes since {rev} could not be listed ({exc})"
    control = [(CONFIG_FILE, CONFIG_FILE), (cfg.model_dir.as_posix(), "the model"),
               (cfg.lock_file.as_posix(), "the pair lock file")]
    if cfg.joints_dir and cfg.joints_sources:
        # the plugins and their helpers only (load() refuses an incomplete list)
        control += [(glob, "a joint plugin or its helper") for glob in cfg.joints_sources]
    elif cfg.joints_dir:
        control.append((cfg.joints_dir.as_posix(), "a joint plugin"))
    for target, what in control:
        hit = sorted(p for p in paths if _scope_hit(p, target))
        if hit:
            return None, f"{what} changed ({hit[0]})"
    return sorted(paths), None


_scope_hit = scope_hit   # crossweft/scope.py


def _normalize_paths(root: Path, paths: list[str]) -> list[str]:
    result = []
    for raw in paths:
        path = Path(raw)
        if path.is_absolute():
            try:
                rel = path.resolve().relative_to(root.resolve()).as_posix()
            except ValueError as exc:
                raise ImpactError(f"{raw} is outside the repository {root}") from exc
        else:
            rel = path.as_posix()
            while rel.startswith("./"):
                rel = rel[2:]
        rel = rel.rstrip("/")
        if not rel or rel == ".":
            raise ImpactError(f"'{raw}' names the whole repository -- pass files or directories")
        result.append(rel)
    return sorted(set(result))


def _path_hit(changed: str, target: str) -> bool:
    """A changed file/dir touches a target file/dir (either may contain the
    other). The target is compared literally first -- `web/orders/[id]/page.ts`
    names a file -- and only then as a glob."""
    target = target.rstrip("/")
    if changed == target or changed.startswith(target + "/") or target.startswith(changed + "/"):
        return True
    return (any(ch in target for ch in "*?[")
            and fnmatch.fnmatchcase(changed, target.replace("**/", "*").replace("**", "*")))


def code_owners(model: Model, path: str) -> list[str]:
    """The block(s) whose `code` holds `path` most specifically (the longest
    code path wins; two blocks listing the same path tie). Empty when no block
    covers it."""
    best: set[str] = set()
    best_len = -1
    for block in model.entities["blocks"]:
        for code in block.get("code", []):
            code = code.rstrip("/")
            if _path_hit(path, code) and len(code) >= best_len:
                if len(code) > best_len:
                    best, best_len = set(), len(code)
                best.add(block["id"])
    return sorted(best)


def build_impact(model: Model, changed: list[str]) -> dict:
    """Everything on the map a set of changed paths touches. Pure: no git, no I/O."""
    m = model

    def hits(targets: list[str]) -> list[str]:
        return sorted({c for c in changed for t in targets if _path_hit(c, t)})

    def anchor_paths(anchors: list[dict]) -> list[str]:
        return [a["path"] for a in anchors]

    guards_of: dict[str, list[str]] = {}
    guard_rows = []
    for kind, prefix in (("joins", "join"), ("sets", "set"), ("pairs", "pair"),
                         ("ui_actions", "ui")):
        for entry in m.entities[kind]:
            ref = f"{prefix}:{entry['id']}"
            if entry.get("link"):
                guards_of.setdefault(entry["link"], []).append(ref)
            if kind == "joins":
                targets = [point["path"] for point in entry["points"]]
            elif kind == "pairs":
                targets = [region["path"] for region in entry["regions"]]
            elif kind == "ui_actions":
                targets = [t for side in [entry["controls"], *entry["hops"]]
                           for t in side["paths"]]
            else:
                targets = [t for key in ("left", "right")
                           for t in (entry[key].get("files") or entry[key].get("paths") or [])]
            touched = hits(targets)
            if touched:
                row = {"ref": ref, "link": entry.get("link"), "files": touched}
                if kind in ("joins", "pairs", "ui_actions"):
                    # the other files this guard reads: the ones to re-read
                    row["other"] = sorted({t for t in targets
                                           if not any(_path_hit(c, t) for c in changed)})
                guard_rows.append(row)

    link_rows = []
    for link in m.entities["links"]:
        sides = {}
        for side in ("from", "to"):
            touched = hits(anchor_paths(link.get(f"{side}_anchors", [])))
            if touched:
                sides[side] = touched
        contract = link.get("contract", {})
        owner = hits(anchor_paths(contract.get("defined_in", [])))
        if owner:
            sides["contract"] = owner
        if not sides:
            continue
        recheck = sorted({a["path"] for key in ("from_anchors", "to_anchors")
                          for a in link.get(key, [])} |
                         set(anchor_paths(contract.get("defined_in", []))))
        ours = all(link.get(f"{side}_anchors") or
                   any(m.blocks[b].get("code") for b in m.ancestors(link[side]))
                   for side in ("from", "to"))
        link_rows.append({
            "link": link["id"], "from": link["from"], "to": link["to"], "both_ends_ours": ours,
            "transport": link["transport"], "status": link["status"],
            "enforcement": contract.get("enforcement"), "contract": contract.get("name"),
            "touched": sides,
            "recheck": [path for path in recheck if not any(_path_hit(c, path) for c in changed)],
            "guards": guards_of.get(link["id"], []),
        })

    owners: dict[str, list[str]] = {}
    unmapped = []
    for path in changed:
        best = code_owners(m, path)
        for ident in best:
            owners.setdefault(ident, []).append(path)
        if not best:
            unmapped.append(path)
    block_rows = []
    for ident, files in sorted(owners.items()):
        links = sorted({l["id"] for l in m.entities["links"]
                        if ident in (l["from"], l["to"])})
        block_rows.append({"block": ident, "status": m.blocks[ident]["status"],
                           "files": files, "links": links})

    data_rows = []
    for item in m.entities["data"]:
        touched = hits(anchor_paths(item.get("defined_in", []) + item.get("anchors", [])))
        if touched:
            data_rows.append({"data": item["id"], "files": touched})
    finding_rows = []
    for finding in m.entities["findings"]:
        if finding["status"] == "closed":
            continue
        touched = hits(anchor_paths(finding.get("evidence", [])))
        if touched:
            finding_rows.append({"finding": finding["id"], "severity": finding["severity"],
                                 "status": finding["status"], "files": touched})
    scan = m.meta.get("route_scan", {})
    route_rows = hits([router["path"] for router in scan.get("routers", [])])
    joint_rows = [row for plugin in m.plugins
                  for row in joints.impact_rows(plugin, m.sections.get(plugin.kind) or [], changed,
                                                _path_hit)]
    on_map = {f for row in link_rows for fs in row["touched"].values() for f in fs}
    on_map |= {f for row in guard_rows + data_rows + finding_rows for f in row["files"]}
    on_map |= set(route_rows)
    on_map |= {f for row in joint_rows for f in row["files"]}
    unmapped = [path for path in unmapped if path not in on_map]
    return {"changed": changed, "links": link_rows, "guards": guard_rows, "blocks": block_rows,
            "data": data_rows, "findings": finding_rows, "route_scan": route_rows,
            "joints": joint_rows, "unmapped": unmapped}


def _say(message: str) -> None:
    """A UTF-8 console gets non-ASCII model names as they are; anything else ASCII."""
    if (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") == "utf8":
        print(message)
    else:
        out(message)


def run_impact(cfg: Config, base: str | None, paths: list[str], as_json: bool = False) -> int:
    root = cfg.root
    try:
        model = load_model(cfg)
    except ModelError as exc:
        out(f"[ERR] {exc}")
        return 2
    errors = validate_schema(model)
    if errors:
        out(f"[FAIL] model schema: {len(errors)} error(s) -- run `check` first")
        return 1
    try:
        if paths:
            changed, label = _normalize_paths(root, paths), "paths given on the command line"
        else:
            changed, label = changed_files(root, base)
    except ImpactError as exc:
        out(f"[ERR] {exc}")
        return 2
    report = build_impact(model, changed)
    if as_json:
        print(json.dumps({"compared": label, **report}, ensure_ascii=True, indent=2))
        return 0
    out(f"CROSSWEFT IMPACT  compared: {label}  changed paths: {len(changed)}")
    if not changed:
        out("[OK] nothing changed -- no seam touched")
        return 0
    if report["links"]:
        out(f"Seams touched ({len(report['links'])}) -- re-read the OTHER side of each before you "
            "commit:")
        for row in report["links"]:
            if row["enforcement"]:
                enforcement = row["enforcement"]
            elif row["transport"] in COMPILER_CHECKED_TRANSPORTS:
                enforcement = "compiler-checked call"
            else:
                enforcement = "no contract declared"
            _say(f"  link:{row['link']}  {row['from']} -> {row['to']}  [{row['transport']}, "
                 f"{row['status']}]  contract: {row['contract'] or '-'} ({enforcement})")
            for side, files in row["touched"].items():
                out(f"      changed ({side} side): {', '.join(files)}")
            if row["recheck"]:
                out(f"      re-check: {', '.join(row['recheck'])}")
            if row["guards"]:
                guard = ", ".join(row["guards"])
            elif row["enforcement"] in SINGLE_OWNER_ENFORCEMENT:
                guard = "the single owner named in contract.defined_in"
            elif row["transport"] in COMPILER_CHECKED_TRANSPORTS and not row["enforcement"]:
                guard = "the compiler (same binary)"
            elif not row["both_ends_ours"]:
                guard = "- (one end is not our code)"
            else:
                guard = "NOTHING -- no join/set/pair compares the sides"
            out(f"      guarded by: {guard}")
    if report["guards"]:
        out(f"Cross-side comparisons reading changed files ({len(report['guards'])}):")
        for row in report["guards"]:
            other = f"  -> re-read: {', '.join(row['other'])}" if row.get("other") else ""
            out(f"  {row['ref']}  (link:{row['link'] or '-'})  {', '.join(row['files'])}{other}")
    if report["route_scan"]:
        out("Server route tables changed (`check` re-derives every route against the map): "
            + ", ".join(report["route_scan"]))
    if report["data"]:
        out("Data formats defined or anchored in changed files: "
            + ", ".join(f"data:{row['data']}" for row in report["data"]))
    if report["findings"]:
        out("Open findings with evidence in changed files (fixed? close them; moved? re-anchor):")
        for row in report["findings"]:
            out(f"  {row['finding']} [{row['severity']}, {row['status']}]  {', '.join(row['files'])}")
    if report["blocks"]:
        out("Blocks whose code changed (their other links may need a look too):")
        for row in report["blocks"]:
            links = ", ".join(row["links"][:8]) + (f" +{len(row['links']) - 8}"
                                                   if len(row["links"]) > 8 else "")
            out(f"  block:{row['block']} [{row['status']}]  files: {len(row['files'])}  "
                f"links: {links or '-'}")
    if report["joints"]:
        out(f"Joints touched ({len(report['joints'])}) -- re-read the other side before committing:")
        for row in report["joints"]:
            out(f"  {row['kind']}: {row['ref']}  {row['message']}")
            if row["other"]:
                out(f"      other side: {', '.join(row['other'])}")
    if report["unmapped"]:
        out(f"Not on the map ({len(report['unmapped'])}): {', '.join(report['unmapped'][:20])}"
            + (" ..." if len(report["unmapped"]) > 20 else ""))
    if set(report["unmapped"]) == set(changed):
        out("Nothing on the map is touched. If these files are code of a component, add them to "
            f"that block's `code` in {cfg.model_dir.as_posix()}/*.json and run `crossweft check`; "
            "otherwise there is no seam to keep in agreement.")
        return 0
    out(f"Next: fix the other sides, update {cfg.model_dir.as_posix()}/*.json, then "
        "`crossweft check` (and `crossweft render` if you commit the generated docs).")
    return 0


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

_SELF_TEST_GO = '''package main

// r.Post("/commented-out", nope) must be ignored: "{" inside strings too.
func registerOrders(r chi.Router) {
	r.Post("/orders", h)
}

func main() {
	r := chi.NewRouter()
	r.Get("/health", h)
	r.Mount("/admin", adm)
	msg := "brace { in string"
	_ = msg
	r.Route("/v1", func(v1 chi.Router) {
		registerOrders(v1)
		v1.Post("/carts/checkout", h)
		v1.With(limit).
			Post("/feedback", h)
		v1.Group(func(auth chi.Router) {
			auth.Mount("/reports", sub)
		})
	})
}
'''

_SELF_TEST_CPP = '''#include <string>
static const wchar_t kPipe[] = L"\\\\\\\\.\\\\pipe\\\\Shop.Events";
const char* kCheckout = "/v1/carts/checkout";
const char* kReport = "/v1/reports/daily";
'''

_SELF_TEST_HDR = '''#pragma once
#define DEMO_PIPE L"\\\\\\\\.\\\\pipe\\\\Shop.Events"
struct Dto { int a; };  // json: "field_a" "field_b"
inline bool accepted(unsigned s) { return s == 200U || s == 429U; }
'''

_SELF_TEST_GO_STATUS = '''package main
func reply(w http.ResponseWriter) { w.WriteHeader(http.StatusOK); w.WriteHeader(http.StatusTooManyRequests) }
'''

_SELF_TEST_MODELS = 'DEFAULT = {"name": "two_x"}\nALTERNATIVE = {"name": "four_x"}\n'

_SELF_TEST_GO_REPORTS = '''package main

func Reports() http.Handler {
	r := chi.NewRouter()
	r.Post("/daily", h)
	return r
}
'''

_SELF_TEST_GO_DTO = '''package main
type Dto struct {
	A int `json:"field_a"`
	B int `json:"field_b"`
}
'''

_SELF_TEST_PRICE_GO = '''package main

// crossweft:begin price-rounding
func roundCents(v float64) int64 { return int64(math.Floor(v*100 + 0.5)) }
// crossweft:end price-rounding
'''

_SELF_TEST_PRICE_TS = '''// crossweft:begin price-rounding
export const roundCents = (v: number) => Math.floor(v * 100 + 0.5);
// crossweft:end price-rounding
'''

_SELF_TEST_EXPRESS = '''const app = express();
app.get("/status", h);
app.post('/jobs/:id/retry', h);
'''


def _self_test_model() -> dict[str, dict]:
    meta = {
        "schema": SCHEMA_ID, "title": "Self-test map", "verified_on": "2026-01-01",
        "planes": [{"id": "runtime", "name": "Runtime"}],
        "lanes": [{"id": "client", "name": "Client", "plane": "runtime"},
                  {"id": "backend", "name": "Backend", "plane": "runtime"}],
        "route_scan": {
            "routers": [{"path": "server/main.go", "scanner": "go-chi",
                         "function_prefixes": {"registerOrders": "/v1"}},
                        {"path": "server/reports.go", "scanner": "go-chi",
                         "function_prefixes": {"Reports": "/v1/reports"}}],
            "router_search": ["server/**/*.go"],
            "client_globs": ["src/**/*.cpp"],
            "client_exclude": ["**/tests/**"],
            "client_literal_regexes": ["\"(/v1/[^\"]*)\""],
        },
        "coverage": {"roots": ["src/*"], "ignore": []},
    }
    blocks = [
        {"id": "app", "name": "App", "kind": "executable", "lane": "client", "status": "current",
         "summary": "demo client", "code": ["src/app/"],
         "anchors": [{"path": "src/app/client.cpp", "find": "kCheckout"}]},
        {"id": "api", "name": "API", "kind": "service", "lane": "backend", "status": "current",
         "summary": "demo server", "code": ["server/"], "route_server": True},
        {"id": "old", "name": "Old", "kind": "executable", "lane": "client", "status": "legacy",
         "summary": "legacy client"},
    ]
    links = [
        {"id": "app-checkout", "from": "app", "to": "api", "transport": "https", "status": "current",
         "summary": "checkout", "carries": ["token"],
         "contract": {"name": "demo DTO", "enforcement": "duplicated"},
         "identifiers": {"route": ["POST /v1/carts/checkout", "POST /v1/reports/daily",
                                   "GET /health", "POST /v1/orders", "POST /v1/feedback",
                                   "ANY /admin*"]},
         "from_anchors": [{"path": "src/app/client.cpp", "find": "\"/v1/carts/checkout\""}],
         "to_anchors": [{"path": "server/main.go", "find": "v1.Post(\"/carts/checkout\""}]},
        {"id": "api-reply", "from": "api", "to": "app", "transport": "https", "status": "current",
         "summary": "reply", "carries": ["token"],
         "contract": {"name": "demo reply", "enforcement": "shared-code",
                      "defined_in": [{"path": "src/app/demo.h", "find": "struct Dto"}]},
         "from_anchors": [{"path": "server/main.go", "find": "func main()"}],
         "to_anchors": [{"path": "src/app/client.cpp", "find": "kCheckout"}]},
    ]
    data = [{"id": "token", "name": "Token", "kind": "credential", "origin": "app", "summary": "demo"}]
    flows = [{"id": "demo", "name": "Demo", "status": "current", "summary": "demo flow",
              "steps": [{"link": "app-checkout", "action": "ask"},
                        {"link": "api-reply", "action": "answer", "reply": False}]}]
    joins = [{"id": "pipe-name", "name": "Pipe", "points": [
        {"path": "src/app/client.cpp", "side": "client", "regex": "kPipe\\[\\] = L\"([^\"]+)\"",
         "transform": ["unescape-c"]},
        {"path": "src/app/demo.h", "side": "service", "regex": "DEMO_PIPE L\"([^\"]+)\"",
         "transform": ["unescape-c"]}]}]
    joins.append({"id": "key-order", "name": "Export columns", "points": [
        {"path": "tools/export.py", "side": "producer", "regex": "for name in \\(([^)]*)\\)",
         "transform": ["csv-words"]},
        {"path": "src/app/keys.h", "side": "consumer",
         "regex": "kHead\\[\\] = \"([a-z_]+)\";\\s*constexpr char kNext\\[\\] = \"([a-z_]+)\""}]})
    joins.append({"id": "model", "name": "Default model", "points": [
        {"path": "tools/models.py", "side": "config", "within": "DEFAULT = \\{([^}]*)\\}",
         "regex": "\"name\": \"([a-z_]+)\""},
        {"path": "src/app/keys.h", "side": "client", "regex": "kModel\\[\\] = \"([a-z_]+)\""}]})
    sets = [{"id": "dto", "name": "DTO fields", "mode": "equal", "link": "app-checkout",
             "left": {"label": "C++", "paths": ["src/app/demo.h"], "regex": "\"(field_[a-z]+)\""},
             "right": {"label": "Go", "paths": ["server/dto.go"], "regex": "json:\"(field_[a-z]+)\""}},
            {"id": "roles", "name": "Roles", "mode": "equal",
             "left": {"label": "exporter", "paths": ["tools/export.py"], "within": "ROLES = \\{([^}]*)\\}",
                      "regex": "\"([a-z.]+)\""},
             "right": {"label": "importer", "paths": ["src/app/keys.h"], "within": "kRoles\\{([^}]*)\\}",
                       "regex": "\"([a-z.]+)\""}},
            {"id": "manifest", "name": "Manifest files exist", "mode": "left-subset",
             "left": {"label": "manifest", "paths": ["server/manifest.json"], "regex": "\"(src/[^\"]+)\""},
             "right": {"label": "files on disk", "files": ["src/app/*.cpp", "src/app/*.h"]}},
            {"id": "header-names", "name": "Header list by bare name", "mode": "equal",
             "left": {"label": "header list", "paths": ["server/headers.json"], "regex": "\"([a-z]+\\.h)\""},
             "right": {"label": "headers on disk", "files": ["src/app/*.h"], "transform": ["basename"]}},
            {"id": "statuses", "name": "HTTP statuses: Go handler vs C++ client", "mode": "left-subset",
             "left": {"label": "Go", "paths": ["server/status.go"], "regex": "http\\.(Status[A-Za-z]+)",
                      "transform": ["go-http-status"]},
             "right": {"label": "C++", "paths": ["src/app/demo.h"], "regex": "s == (\\d+)U"}}]
    return {"00-meta.json": {"meta": meta}, "10-main.json": {"blocks": blocks, "links": links,
            "data": data, "flows": flows, "joins": joins, "sets": sets, "pairs": [], "findings": []}}


_SELF_TEST_CONFIG = {"model_dir": "seams/model", "output_dir": "seams"}

# A joint-kind plugin for the self-test: each entry names a file and a text it
# must contain.
_SELF_TEST_JOINT_PLUGIN = """SECTION = "demo"


def validate(entries):
    return [f"entry {e!r} needs id, file, find" for e in entries
            if not (isinstance(e, dict) and {"id", "file", "find"} <= set(e))]


def check(root, entries, model):
    problems = []
    for e in entries:
        path = root / e["file"]
        found = path.is_file() and e["find"] in path.read_text(encoding="utf-8")
        if not found:
            problems.append((e["id"], f"{e['file']} lacks {e['find']}", False))
    return {"problems": problems, "items": len(entries), "files": {e["file"] for e in entries},
            "info": f"{len(entries)} checked"}


def self_test():
    return 0
"""


def _write_fixture(root: Path, model: dict[str, dict], config: dict | None = None) -> None:
    files = {
        "server/main.go": _SELF_TEST_GO, "server/dto.go": _SELF_TEST_GO_DTO,
        "server/reports.go": _SELF_TEST_GO_REPORTS, "server/price.go": _SELF_TEST_PRICE_GO,
        "server/status.go": _SELF_TEST_GO_STATUS, "tools/models.py": _SELF_TEST_MODELS,
        "src/app/client.cpp": _SELF_TEST_CPP, "src/app/demo.h": _SELF_TEST_HDR,
        "src/app/price.ts": _SELF_TEST_PRICE_TS,
        "server/manifest.json": '{"files": ["src/app/client.cpp", "src/app/demo.h"]}\n',
        "server/headers.json": '{"names": ["demo.h", "keys.h"]}\n',
        "tools/export.py": 'KEYS = [name for name in ("customer_id", "order_id")]\nROLES = {"a.one", "a.two"}\n',
        "src/app/keys.h": 'constexpr char kHead[] = "customer_id";\nconstexpr char kNext[] = "order_id";\n'
                          'static const std::set<std::string> kRoles{"a.one", "a.two"};\n'
                          'static const std::set<std::string> kOther{"zzz"};\n'
                          'constexpr char kModel[] = "two_x";\n',
        "src/app/tests/ignored.cpp": 'const char* x = "/v1/not/mapped";\n',
        "worker/app.js": _SELF_TEST_EXPRESS,
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (root / CONFIG_FILE).write_text(json.dumps(config or _SELF_TEST_CONFIG), encoding="utf-8")
    cfg = load_config(root)
    model_dir = cfg.abs(cfg.model_dir)
    if model_dir.exists():
        for old in model_dir.glob("*.json"):
            old.unlink()
    lock = cfg.abs(cfg.lock_file)
    if lock.exists():
        lock.unlink()
    model_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in model.items():
        (model_dir / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _captured(func, *args, **kwargs) -> tuple[object, str]:
    import contextlib
    import io
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        value = func(*args, **kwargs)
    return value, buffer.getvalue()


_SELF_TEST_RUNS = [0]   # scenarios actually executed (reported on the SELF-TEST line)


def _expect(label: str, root: Path, model: dict[str, dict], want: int, needle: str | None = None,
            mutate_files: dict[str, str] | None = None, fmt: str = "text",
            before_check=None, config: dict | None = None) -> list[str]:
    _SELF_TEST_RUNS[0] += 1
    _write_fixture(root, model, config)
    for rel, text in (mutate_files or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    cfg = load_config(root)
    if before_check:
        before_check(cfg)
    code, output = _captured(run_check, cfg, fmt)
    problems = []
    if code != want:
        problems.append(f"{label}: expected exit {want}, got {code}\n{output}")
    if needle and needle not in output:
        problems.append(f"{label}: expected {needle!r} in output\n{output}")
    return problems


def self_test() -> int:
    import copy
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="crossweft-selftest-") as tmp:
        root = Path(tmp)
        base = _self_test_model()
        failures += _expect("good model", root, base, 0, "RESULT: PASS")

        # joint-kind plugins: a working one passes; a pinned kind without entries,
        # a crashing check and a check that compared nothing are all red
        plugin = {"tools/joints/joints_demo.py": _SELF_TEST_JOINT_PLUGIN}
        joint_config = {**_SELF_TEST_CONFIG, "joints": {"dir": "tools/joints"}}
        with_joints = copy.deepcopy(base)
        with_joints["00-meta.json"]["meta"]["joint_kinds"] = ["demo"]
        with_joints["10-main.json"]["demo"] = [{"id": "head", "file": "src/app/keys.h",
                                                "find": "customer_id"}]
        failures += _expect("joint plugin passes", root, with_joints, 0, "demo: 1 checked",
                            mutate_files=plugin, config=joint_config)
        no_entries = copy.deepcopy(with_joints)
        del no_entries["10-main.json"]["demo"]
        failures += _expect("pinned joint kind without entries", root, no_entries, 1,
                            "joints:empty:demo", mutate_files=plugin, config=joint_config)
        failures += _expect("joint plugin crash", root, with_joints, 1, "joints:broken:demo",
                            config=joint_config, mutate_files={
                                k: v.replace("problems = []", "raise ValueError('planted')")
                                for k, v in plugin.items()})
        failures += _expect("joint plugin compared nothing", root, with_joints, 1,
                            "joints:broken:demo", config=joint_config, mutate_files={
                                k: v.replace('"items": len(entries)', '"items": 0')
                                for k, v in plugin.items()})
        failures += _expect("joint plugin finds drift", root, with_joints, 1, "demo:head",
                            config=joint_config, mutate_files={
                                **plugin, "src/app/keys.h": 'constexpr char kHead[] = "id";\n'})

        broken = copy.deepcopy(base)
        broken["10-main.json"]["links"][0]["from_anchors"][0]["find"] = "\"/v1/carts/checkoutX\""
        failures += _expect("broken anchor", root, broken, 1, "literal not found")

        failures += _expect("join mismatch", root, base, 1, "join:pipe-name disagrees",
                            mutate_files={"src/app/demo.h": _SELF_TEST_HDR.replace("Shop.Events", "Other.Pipe")})
        # a second, drifted definition on ONE side must not hide behind the first
        failures += _expect("join: every occurrence counts", root, base, 1, "join:pipe-name disagrees",
                            mutate_files={"src/app/demo.h": _SELF_TEST_HDR + '#define DEMO_PIPE L"\\\\\\\\.\\\\pipe\\\\Drift"\n'})
        failures += _expect("join: repeated equal occurrences are fine", root, base, 0, "RESULT: PASS",
                            mutate_files={"src/app/demo.h": _SELF_TEST_HDR + _SELF_TEST_HDR.split("\n")[1] + "\n"})

        # a finding names ONE disagreement: the key carries a digest of every
        # point's (file, pattern, value)
        pipe_points = base["10-main.json"]["joins"][0]["points"]
        drift_key = "join:pipe-name:" + key_digest([
            (pipe_points[0]["path"], pipe_points[0]["regex"], "\\\\.\\pipe\\Shop.Events"),
            (pipe_points[1]["path"], pipe_points[1]["regex"], "\\\\.\\pipe\\Other.Pipe")])
        known = copy.deepcopy(base)
        known["10-main.json"]["findings"] = [{
            "id": "SM-001", "title": "pipe drift", "severity": "major", "status": "open",
            "kind": "contract-mismatch", "summary": "demo", "detected_by": [drift_key],
            "next_step": "fix", "owner": "demo"}]
        failures += _expect("known finding", root, known, 0, "[KNOWN]",
                            mutate_files={"src/app/demo.h": _SELF_TEST_HDR.replace("Shop.Events", "Other.Pipe")})
        failures += _expect("a different disagreement is not excused", root, known, 1,
                            "stale findings",
                            mutate_files={"src/app/demo.h": _SELF_TEST_HDR.replace("Shop.Events", "Third.Pipe")})
        failures += _expect("stale finding", root, known, 1, "stale findings")

        unmapped = copy.deepcopy(base)
        unmapped["10-main.json"]["links"][0]["identifiers"]["route"].remove("GET /health")
        failures += _expect("unmapped server route", root, unmapped, 1, "route:unmapped:GET /health")

        unserved = copy.deepcopy(base)
        unserved["10-main.json"]["links"][0]["identifiers"]["route"].append("POST /v1/ghost")
        failures += _expect("unserved route", root, unserved, 1, "route:unserved:POST /v1/ghost")

        failures += _expect("client literal", root, base, 1, "route:client-unexplained:/v1/extra",
                            mutate_files={"src/app/client.cpp": _SELF_TEST_CPP + 'const char* e = "/v1/extra";\n'})

        no_server = copy.deepcopy(base)
        del no_server["10-main.json"]["blocks"][1]["route_server"]
        failures += _expect("routes without a server block", root, no_server, 1, "route:no-server-block")

        regex_scan = copy.deepcopy(base)
        regex_scan["00-meta.json"]["meta"]["route_scan"]["routers"].append(
            {"path": "worker/app.js", "scanner": "regex", "prefix": "/w",
             "pattern": "app\\.(?P<method>get|post)\\([\"'](?P<path>/[^\"']*)[\"']"})
        failures += _expect("regex route scanner", root, regex_scan, 1, "route:unmapped:POST /w/jobs/:id/retry")
        regex_ok = copy.deepcopy(regex_scan)
        regex_ok["10-main.json"]["links"][0]["identifiers"]["route"] += ["GET /w/status",
                                                                         "POST /w/jobs/{id}/retry"]
        failures += _expect("regex routes + :param normalisation", root, regex_ok, 0, "RESULT: PASS")

        prov = copy.deepcopy(base)
        prov["10-main.json"]["data"][0]["origin"] = "old"
        failures += _expect("provenance gap", root, prov, 1, "provenance:app-checkout:token")

        legacy = copy.deepcopy(base)
        legacy["10-main.json"]["links"][1]["to"] = "old"
        legacy["10-main.json"]["links"][1]["to_anchors"] = []
        failures += _expect("current link to legacy", root, legacy, 1, "status:api-reply")

        failures += _expect("set difference", root, base, 1, "set:dto:right-only:field_b",
                            mutate_files={"src/app/demo.h": _SELF_TEST_HDR.replace(' "field_b"', "")})
        allowed = copy.deepcopy(base)
        allowed["10-main.json"]["sets"][0]["allow"] = [{"item": "field_b", "side": "right",
                                                        "reason": "server-only field"}]
        failures += _expect("stale allow entry", root, allowed, 1, "no longer needed")

        failures += _expect("multi-group join", root, base, 1, "join:key-order disagrees",
                            mutate_files={"tools/export.py": 'KEYS = [name for name in ("customer_id", "sku", "order_id")]\nROLES = {"a.one", "a.two"}\n'})
        failures += _expect("set within region", root, base, 1, "set:roles:left-only:a.three",
                            mutate_files={"tools/export.py": 'KEYS = [name for name in ("customer_id", "order_id")]\nROLES = {"a.one", "a.two", "a.three"}\n'})
        failures += _expect("set files source", root, base, 1, "set:manifest:left-only:src/app/missing.cpp",
                            mutate_files={"server/manifest.json": '{"files": ["src/app/client.cpp", "src/app/missing.cpp"]}\n'})
        failures += _expect("set basename transform", root, base, 1, "set:header-names:left-only:gone.h",
                            mutate_files={"server/headers.json": '{"names": ["demo.h", "keys.h", "gone.h"]}\n'})
        failures += _expect("set go-http-status transform", root, base, 1, "set:statuses:left-only:503",
                            mutate_files={"server/status.go": _SELF_TEST_GO_STATUS.replace(
                                "http.StatusOK)", "http.StatusOK); w.WriteHeader(http.StatusServiceUnavailable)")})
        failures += _expect("set go-http-status unknown name", root, base, 1,
                            "set:statuses:left-only:StatusNoSuchThing",
                            mutate_files={"server/status.go": _SELF_TEST_GO_STATUS.replace(
                                "http.StatusOK)", "http.StatusOK); w.WriteHeader(http.StatusNoSuchThing)")})
        failures += _expect("join within: drift inside the region", root, base, 1, "join:model disagrees",
                            mutate_files={"tools/models.py": _SELF_TEST_MODELS.replace('"two_x"', '"three_x"')})
        failures += _expect("join within: a value outside the region is not compared", root, base, 0,
                            "RESULT: PASS",
                            mutate_files={"tools/models.py": _SELF_TEST_MODELS.replace('"four_x"', '"eight_x"')})
        failures += _expect("join within: a second value inside the region counts", root, base, 1,
                            "join:model disagrees",
                            mutate_files={"tools/models.py": _SELF_TEST_MODELS.replace(
                                '{"name": "two_x"}', '{"name": "two_x", "name": "nine_x"}')})
        anchored = copy.deepcopy(base)
        anchored["10-main.json"]["joins"][-1]["points"][0]["regex"] = '^"name": "([a-z_]+)"'
        failures += _expect("join within: ^ sees the file, not the region start", root, anchored, 1,
                            "anchor:join:model:tools/models.py")
        anchored["10-main.json"]["joins"][-1]["points"][0]["regex"] = '"name": "([a-z_]+)"$'
        failures += _expect("join within: $ sees the file, not the region end", root, anchored, 1,
                            "anchor:join:model:tools/models.py")
        anchored["10-main.json"]["joins"][-1]["points"][0]["regex"] = '"name": "([a-z_]+)"(?=\\})'
        failures += _expect("join within: lookahead sees past the region end", root, anchored, 0,
                            "RESULT: PASS")
        failures += _expect("join within: region not found", root, base, 1, "anchor:join:model:within:tools/models.py",
                            mutate_files={"tools/models.py": 'ALTERNATIVE = {"name": "four_x"}\n'})

        # seams: every current link between two code blocks says how its sides agree
        undeclared = copy.deepcopy(base)
        del undeclared["10-main.json"]["links"][0]["contract"]
        failures += _expect("seam without contract", root, undeclared, 1,
                            "seam:undeclared:app-checkout")
        compiled = copy.deepcopy(base)
        compiled["10-main.json"]["links"][1]["transport"] = "in-process"
        del compiled["10-main.json"]["links"][1]["contract"]
        failures += _expect("in-process seam needs no contract", root, compiled, 0, "RESULT: PASS")
        unanchored = copy.deepcopy(base)
        del unanchored["10-main.json"]["links"][1]["contract"]["defined_in"]
        failures += _expect("single owner not named", root, unanchored, 1,
                            "seam:owner-unanchored:api-reply")
        unguarded = copy.deepcopy(base)
        del unguarded["10-main.json"]["sets"][0]["link"]
        failures += _expect("hand-written seam without guard", root, unguarded, 1,
                            "seam:unguarded:app-checkout")
        one_sided = copy.deepcopy(unguarded)
        one_sided["10-main.json"]["joins"][0]["link"] = "app-checkout"  # both points in app
        failures += _expect("guard reading one side only", root, one_sided, 1,
                            "do not read a file of each side")
        excused = copy.deepcopy(unguarded)
        excused["10-main.json"]["findings"] = [{
            "id": "SM-002", "title": "unguarded seam", "severity": "minor", "status": "open",
            "kind": "hand-duplicated", "summary": "demo",
            "detected_by": ["seam:unguarded:app-checkout"], "next_step": "add a set", "owner": "demo"}]
        failures += _expect("unguarded seam recorded as finding", root, excused, 0,
                            "SM-002: seam:unguarded:app-checkout")
        guarded_again = copy.deepcopy(base)
        guarded_again["10-main.json"]["findings"] = excused["10-main.json"]["findings"]
        failures += _expect("guard added, finding must close", root, guarded_again, 1,
                            "SM-002:seam:unguarded:app-checkout")
        external = copy.deepcopy(base)
        external["10-main.json"]["blocks"].append(
            {"id": "ext", "name": "Ext", "kind": "external", "lane": "backend",
             "status": "current", "summary": "outside service"})
        external["10-main.json"]["links"].append(
            {"id": "app-ext", "from": "app", "to": "ext", "transport": "https",
             "status": "current", "summary": "call out",
             "contract": {"name": "their API", "enforcement": "none"},
             "from_anchors": [{"path": "src/app/client.cpp", "find": "kReport"}]})
        failures += _expect("no-contract seam to an external end", root, external, 0,
                            "RESULT: PASS")

        # pairs: fingerprinted regions, attested with a reason
        paired = copy.deepcopy(unguarded)
        paired["10-main.json"]["pairs"] = [{
            "id": "price-rounding", "name": "Price rounding", "link": "app-checkout",
            "regions": [{"path": "src/app/price.ts", "side": "client"},
                        {"path": "server/price.go", "side": "server"}]}]
        failures += _expect("pair never attested", root, paired, 1, "pair:unattested:price-rounding")

        def attest_all(cfg: Config) -> None:
            code, text = _captured(run_attest, cfg, [], "self-test: both regions read", True, False)
            if code != 0:
                failures.append(f"attest failed: {code}\n{text}")

        failures += _expect("attested pair guards the seam", root, paired, 0, "RESULT: PASS",
                            before_check=attest_all)

        def attest_then_edit_go(cfg: Config) -> None:
            attest_all(cfg)
            (cfg.root / "server/price.go").write_text(
                _SELF_TEST_PRICE_GO.replace("0.5", "0.49"), encoding="utf-8")

        failures += _expect("pair region changed after attestation", root, paired, 1,
                            "re-read src/app/price.ts", before_check=attest_then_edit_go)

        def attest_then_touch_whitespace(cfg: Config) -> None:
            attest_all(cfg)
            path = cfg.root / "server/price.go"
            path.write_text(path.read_text(encoding="utf-8").replace("\n", "  \r\n"),
                            encoding="utf-8")

        failures += _expect("trailing whitespace / CRLF does not change a pair", root, paired, 0,
                            "RESULT: PASS", before_check=attest_then_touch_whitespace)
        failures += _expect("pair markers missing", root, paired, 1, "expected exactly one",
                            mutate_files={"server/price.go": "package main\n"})

        def stale_lock(cfg: Config) -> None:
            write_lock(cfg, {"schema": LOCK_SCHEMA, "pairs": {"ghost": {
                "regions": {}, "attested_on": "2026-01-01", "reason": "old"}}})

        failures += _expect("lock attests an undeclared pair", root, base, 1,
                            "pair:stale-lock:ghost", before_check=stale_lock)
        code, text = _captured(run_attest, load_config(root), ["price-rounding"], "  ", False, False)
        if code == 0:
            failures.append("attest accepted an empty reason")

        uncovered = copy.deepcopy(base)
        uncovered["10-main.json"]["blocks"][0]["code"] = ["src/app/client.cpp", "src/app/demo.h",
                                                          "src/app/price.ts"]
        failures += _expect("coverage ok via file inside area", root, uncovered, 0)
        (root / "src" / "newarea").mkdir(parents=True, exist_ok=True)
        failures += _expect("new source area", root, base, 1, "coverage:src/newarea")
        (root / "src" / "newarea").rmdir()

        schema = copy.deepcopy(base)
        schema["10-main.json"]["links"][0]["transport"] = "carrier-pigeon"
        failures += _expect("schema enum", root, schema, 2, "model schema")

        escape = copy.deepcopy(base)
        escape["10-main.json"]["blocks"][0]["anchors"] = [{"path": "../outside.txt", "find": "x"}]
        failures += _expect("path leaving the repository", root, escape, 2, "may not leave the repository")
        absolute = copy.deepcopy(base)
        absolute["10-main.json"]["joins"][0]["points"][0]["path"] = "/etc/hostname"
        failures += _expect("absolute path", root, absolute, 2, "is absolute")

        orphan_plane = copy.deepcopy(base)
        del orphan_plane["00-meta.json"]["meta"]["planes"]
        failures += _expect("lane plane without planes", root, orphan_plane, 2, "needs meta.planes")

        reqs = copy.deepcopy(base)
        reqs["10-main.json"]["blocks"][0]["requirements"] = ["REQ-1"]
        failures += _expect("requirements without a configured doc", root, reqs, 2,
                            "declares no requirements.doc")

        failures += _expect("github annotations", root, base, 1, "::error file=src/app/demo.h,line=",
                            fmt="github",
                            mutate_files={"src/app/demo.h": _SELF_TEST_HDR.replace("Shop.Events", "Other.Pipe")})
        failures += _expect("json output", root, base, 0, '"ok": true', fmt="json")

        # config is strict
        _write_fixture(root, base)
        for bad, needle in (({"model_dir": "seams/model", "colour": "red"}, "unknown keys"),
                            ({"model_dir": "../x"}, "may not leave"),
                            ({"model_dir": "m", "lock_file": "m/lock.json"}, "lock_file may not"),
                            ({"language": "xx"}, "language must be")):
            (root / CONFIG_FILE).write_text(json.dumps(bad), encoding="utf-8")
            try:
                load_config(root)
                failures.append(f"config {bad} was accepted")
            except ModelError as exc:
                if needle not in str(exc):
                    failures.append(f"config {bad}: expected {needle!r}, got {exc}")

        empty_root = root / "empty"
        (empty_root / "seams" / "model").mkdir(parents=True)
        (empty_root / CONFIG_FILE).write_text("{}", encoding="utf-8")
        code, text = _captured(run_check, load_config(empty_root))
        if code != 2 or "scanned nothing" not in text:
            failures.append(f"empty model dir must fail loud; got {code}: {text}")

        init_root = root / "fresh"
        init_root.mkdir()
        code, text = _captured(run_init, init_root, "en")
        if code != 0 or not (init_root / CONFIG_FILE).is_file():
            failures.append(f"init failed: {code}\n{text}")
        code, text = _captured(run_check, load_config(init_root))
        if code != 2 or "declares nothing yet" not in text:
            failures.append(f"a freshly initialised (empty) model must fail loud: {code}\n{text}")
        code, text = _captured(run_init, init_root, "en")
        if code == 0:
            failures.append("init overwrote an existing crossweft.json")

        # brace scanner ignores braces/routes in comments and strings
        braces = Checker.go_code_braces('a { "}" /* } */ // }\n b }')
        if list(braces.items()) != [(2, 24)]:
            failures.append(f"go brace scanner wrong: {braces}")

        # i18n tables have the same keys
        failures += [f"i18n: {p}" for p in i18n.key_mismatches()]

        # render is deterministic, --check detects staleness, both languages render
        for language in sorted(i18n.LANGUAGES):
            _write_fixture(root, base, {**_SELF_TEST_CONFIG, "language": language})
            cfg = load_config(root)
            first, _ = _captured(run_render, cfg, False)
            again, _ = _captured(run_render, cfg, True)
            if first != 0 or again != 0:
                failures.append(f"render/re-check failed ({language}): {first}/{again}")
            readme = (root / "seams" / "README.md").read_text(encoding="utf-8")
            if "(../src/app/)" not in (root / "seams" / "blocks.md").read_text(encoding="utf-8"):
                failures.append(f"render ({language}): repository links not relative to output_dir")
            if "{" + "model}" in readme or "text." in readme:
                failures.append(f"render ({language}): unformatted placeholder in README")
            (root / "seams" / "README.md").write_text("tampered", encoding="utf-8")
            tampered, _ = _captured(run_render, cfg, True)
            if tampered != 1:
                failures.append("render --check did not detect a tampered generated file")
            refused, text = _captured(run_render, cfg, False)
            if refused != 2 or "was not generated by crossweft" not in text:
                failures.append(f"render overwrote a file it did not generate: {refused} {text}")
            (root / "seams" / "README.md").unlink()
        failures += _expect("check_rendered_docs gates on stale docs", root, base, 1,
                            "generated docs are stale",
                            config={**_SELF_TEST_CONFIG, "check_rendered_docs": True})

        # impact: a change on one side of a seam names the other side and its guard
        _write_fixture(root, paired)
        fixture = load_model(load_config(root))
        report = build_impact(fixture, ["server/main.go", "notes/unrelated.txt"])
        rows = {row["link"]: row for row in report["links"]}
        checkout = rows.get("app-checkout", {})
        if checkout.get("touched") != {"to": ["server/main.go"]}:
            failures.append(f"impact: app-checkout 'to' side not reported: {checkout}")
        if checkout.get("recheck") != ["src/app/client.cpp"] \
                or checkout.get("guards") != ["pair:price-rounding"]:
            failures.append(f"impact: other side / guard of app-checkout wrong: {checkout}")
        if rows.get("api-reply", {}).get("touched") != {"from": ["server/main.go"]}:
            failures.append(f"impact: api-reply 'from' side not reported: {rows.get('api-reply')}")
        if report["unmapped"] != ["notes/unrelated.txt"] or report["route_scan"] != ["server/main.go"]:
            failures.append(f"impact: unmapped/route-table rows wrong: {report}")
        guard = build_impact(fixture, ["server/price.go"])
        pair_rows = [row for row in guard["guards"] if row["ref"] == "pair:price-rounding"]
        if not pair_rows or pair_rows[0].get("other") != ["src/app/price.ts"]:
            failures.append(f"impact: pair region change does not name the other region: {guard}")
        try:
            _normalize_paths(root, [str(root.parent / "outside.txt")])
            failures.append("impact: a path outside the repository was accepted")
        except ImpactError:
            pass
        code, text = _captured(run_impact, load_config(root), None, ["server/main.go"])
        if code != 0 or "link:app-checkout" not in text:
            failures.append(f"impact command failed: {code}\n{text}")

        # built-in hazard rules: the planted cases in hazards.PLANTED, then the
        # rule set end to end -- each unsafe form red under its own key, the safe
        # forms green, and a glob that names no file red (never "scanned nothing")
        failures += hazards.self_test()
        failures += ui_actions.self_test()
        hazard_config = {**_SELF_TEST_CONFIG, "hazards": {"include": ["build/*"]}}
        unsafe = {"build/run.cmd": 'echo step=%RC%>>"status.txt"\n',
                  "build/read.ps1": "$code = [int]$parts[1]\n",
                  "build/env.cmd": "set RC=%ERRORLEVEL%\ncmake --build out\n",
                  "build/guard.sh": 'case "$SHA" in\n  ' + "?" * 40 + '*) exit 2 ;;\nesac\n'}
        safe = {"build/run.cmd": '>>"status.txt" echo step=%RC%\n',
                "build/read.ps1": "if ($parts[1] -notmatch '^[0-9]+$') { exit 2 }\n"
                                  "$code = [int]$parts[1]\n",
                "build/env.cmd": "set BUILD_RC=%ERRORLEVEL%\ncmake --build out\n",
                "build/guard.sh": '[ "${#SHA}" -eq 40 ] || exit 2\n'}
        for rel, rule in (("build/run.cmd", "cmd-digit-redirect"),
                          ("build/read.ps1", "empty-to-zero"),
                          ("build/env.cmd", "toolchain-env-name"),
                          ("build/guard.sh", "glob-length-guard")):
            failures += _expect(f"hazard {rule}", root, base, 1, f"hazard:{rel}:{rule}:",
                                mutate_files={**safe, rel: unsafe[rel]}, config=hazard_config)
        failures += _expect("hazards: safe forms pass", root, base, 0, "[OK]   hazards",
                            mutate_files=safe, config=hazard_config)
        failures += _expect("hazards: glob naming no file", root, base, 1,
                            "hazard-config:empty:tools/*.cmd", mutate_files=safe,
                            config={**_SELF_TEST_CONFIG,
                                    "hazards": {"include": ["build/*", "tools/*.cmd"]}})
    if failures:
        for failure in failures:
            out(f"[SELF-TEST FAIL] {failure}")
        return 1
    checks = _SELF_TEST_RUNS[0]
    out("[OK] crossweft engine self-test passed (anchors, joins incl. every-occurrence and "
        "multi-group, sets, pairs incl. attest/whitespace/stale lock, findings, routes incl. "
        "regex scanner, provenance, seams, status, coverage, schema, path escape, strict config, "
        "init, render in every language, github/json output, impact, joint plugins, "
        "hazards)")
    out(f"SELF-TEST: checks={checks}")
    return 0


# --------------------------------------------------------------------------- #
# attest / init
# --------------------------------------------------------------------------- #

def _load_valid(cfg: Config) -> Model | None:
    try:
        model = load_model(cfg)
    except ModelError as exc:
        out(f"[ERR] {exc}")
        return None
    errors = validate_schema(model)
    if errors:
        out(f"[FAIL] model schema: {len(errors)} error(s) -- run `crossweft check` first")
        for error in errors[:20]:
            out(f"   - {error}")
        return None
    return model


def run_attest(cfg: Config, ids: list[str], reason: str | None, all_pending: bool,
               prune: bool, other_side_unchanged: bool = False) -> int:
    """Record the current fingerprints of pairs after someone has read every
    region. The reason is mandatory and lands in the lock file, so review sees
    WHY a changed pair was accepted -- attest is a statement, not a reset."""
    model = _load_valid(cfg)
    if model is None:
        return 2
    try:
        lock = read_lock(cfg)
    except ModelError as exc:
        out(f"[ERR] {exc}")
        return 2
    declared = {pair["id"]: pair for pair in model.entities["pairs"]}
    unknown = [ident for ident in ids if ident not in declared]
    if unknown:
        out(f"[ERR] unknown pair id(s): {', '.join(unknown)} (declared: "
            f"{', '.join(sorted(declared)) or '-'}) -- nothing written")
        return 2
    changed_lock = False
    if prune:
        for ident in sorted(set(lock["pairs"]) - set(declared)):
            del lock["pairs"][ident]
            changed_lock = True
            out(f"[OK] pruned lock entry for undeclared pair '{ident}'")
    checker = Checker(model)
    states = {}
    for ident, pair in declared.items():
        if ids and ident not in ids:
            continue
        hashes, _, ok = checker.pair_state(pair)
        if not ok:
            for problem in checker.problems:
                out(f"[FAIL] {problem.message}")
            return 1
        states[ident] = hashes
    targets = [ident for ident in ids] if ids else [
        ident for ident, hashes in states.items()
        if lock["pairs"].get(ident, {}).get("regions") != hashes] if all_pending else []
    if not targets and not prune:
        if all_pending:
            out("[OK] every pair is already attested at its current content")
            return 0
        out("[ERR] name the pair id(s) to attest, or pass --all for every changed/unattested pair")
        return 2
    if targets and (not reason or not reason.strip()):
        out("[ERR] --reason is required: say what you compared (e.g. \"re-read the Go and TS "
            "rounding, both use half-up\")")
        return 2
    # The easy way out of a red pair is to attest the one region that changed.
    # When only some regions moved, their twins were not touched: refuse unless
    # the caller states it re-read them and they are already equivalent.
    one_sided = []
    for ident in targets:
        previous = (lock["pairs"].get(ident) or {}).get("regions") or {}
        changed = sorted(rel for rel, digest in states[ident].items() if previous.get(rel) != digest)
        unchanged = sorted(set(states[ident]) - set(changed))
        if previous and changed and unchanged:
            one_sided.append((ident, changed, unchanged))
    if one_sided and not other_side_unchanged:
        for ident, changed, unchanged in one_sided:
            out(f"[REFUSED] pair:{ident}: only {', '.join(changed)} changed since the last "
                f"attestation; {', '.join(unchanged)} did not.")
        out("   Bring the other region in line first (then both have changed), or -- if you "
            "re-read it and it is already equivalent -- re-run with --other-side-unchanged. "
            "Nothing written.")
        return 1
    today = datetime.date.today().isoformat()
    for ident in targets:
        current = lock["pairs"].get(ident)
        if current and current.get("regions") == states[ident]:
            out(f"[OK] pair:{ident} unchanged since {current['attested_on']} -- nothing to attest")
            continue
        lock["pairs"][ident] = {"regions": states[ident], "attested_on": today,
                                "reason": reason.strip()}
        changed_lock = True
        out(f"[OK] attested pair:{ident} ({len(states[ident])} regions)")
    if changed_lock:
        try:
            write_lock(cfg, lock)
        except ModelError as exc:
            out(f"[ERR] {exc}")
            return 2
        out(f"wrote {cfg.lock_file.as_posix()} -- commit it with the change")
    return 0


BASELINE_KINDS = {"joins": "contract-mismatch", "sets": "contract-mismatch",
                  "ui": "contract-mismatch",
                  "pairs": "hand-duplicated", "routes": "unmapped-route"}


def run_baseline(cfg: Config, owner: str | None, next_step: str | None,
                 file_name: str = "90-baseline.json") -> int:
    """Record every problem `check` reports now as open findings, one per
    category, so a repository can adopt crossweft without fixing its old drift
    first. New drift still fails, and each finding goes stale -- failing the
    check -- once its problems are fixed. Problems a finding cannot excuse
    (broken anchors, stale allow or lock entries) are refused, not recorded."""
    if not (owner and owner.strip()) or not (next_step and next_step.strip()):
        out("[ERR] --owner and --next-step are required: a baseline finding is a debt "
            "someone owns, not a mute switch")
        return 2
    if "/" in file_name or "\\" in file_name or not file_name.endswith(".json"):
        out(f"[ERR] --file must be a plain *.json name inside {cfg.model_dir.as_posix()}")
        return 2
    shown = (cfg.model_dir / file_name).as_posix()
    target = cfg.abs(cfg.model_dir) / file_name
    if target.exists():
        out(f"[ERR] {shown} already exists -- nothing written; pick another --file "
            "or extend that file by hand")
        return 2
    result = evaluate(cfg)
    if result.load_error or result.schema_errors:
        print_result(cfg, result)
        out("[ERR] baseline needs a model that loads and validates -- nothing written")
        return 2
    if result.stale or result.render_stale:
        out("[ERR] fix stale findings and run `crossweft render` first -- a baseline records "
            "drift in the code, not in the register -- nothing written")
        return 2
    fatal = [p for p in result.new if p.fatal]
    if fatal:
        out(f"[FAIL] {len(fatal)} problem(s) cannot be excused by a finding -- fix them first; "
            "nothing written:")
        for problem in fatal:
            out(f"   - {problem.message}  (key: {problem.key})")
        return 1
    if not result.new:
        out("[OK] nothing to baseline: check already passes")
        return 0
    model = load_model(cfg)
    numbers = [int(m.group(1)) for f in model.entities["findings"]
               if (m := re.match(r"^SM-(\d+)$", str(f.get("id", ""))))]
    next_id = max(numbers, default=0) + 1
    today = datetime.date.today().isoformat()
    by_category: dict[str, list[Problem]] = {}
    for problem in result.new:
        by_category.setdefault(problem.category, []).append(problem)
    findings = []
    for category in sorted(by_category):
        problems = by_category[category]
        findings.append({
            "id": f"SM-{next_id:03d}",
            "title": f"Baseline: {len(problems)} {category} problem(s) present at adoption",
            "severity": "minor", "status": "open",
            "kind": BASELINE_KINDS.get(category, "other"),
            "summary": "Recorded by `crossweft baseline` on " + today + ": "
                       + "; ".join(p.message for p in problems[:3])
                       + (f"; and {len(problems) - 3} more" if len(problems) > 3 else ""),
            "detected_by": sorted({p.key for p in problems}),
            "owner": owner.strip(), "next_step": next_step.strip(), "since": today,
        })
        next_id += 1
    try:
        # open(newline=) rather than Path.write_text(newline=), which needs Python 3.10
        with open(target, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps({"findings": findings}, indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        out(f"[ERR] could not write {shown}: {exc}")
        return 2
    after = evaluate(cfg)
    if after.load_error or after.schema_errors or after.new:
        out(f"[ERR] wrote {shown} but check still reports new problems -- "
            "inspect it; this is a crossweft bug")
        print_result(cfg, after)
        return 2
    out(f"[OK] wrote {shown}: {len(findings)} finding(s) covering "
        f"{len(result.new)} problem(s); new drift still fails the check")
    if after.render_stale:
        out("run `crossweft render` -- the findings page changed")
    out("commit it; each finding goes stale (and fails) once its problems are fixed")
    return 0


MODEL_REFERENCE_URL = "https://github.com/AnastasiyaW/crossweft/blob/main/docs/model-reference.md"

EXAMPLE_DIR = "crossweft-example"
EXAMPLE_MODEL_FILE = "20-example.json"
# One hand-written value in two languages: the smallest seam that needs a guard.
EXAMPLE_FILES = {
    f"{EXAMPLE_DIR}/api/version.py": '# The API version this server speaks.\nAPI_VERSION = "2026-10-01"\n',
    f"{EXAMPLE_DIR}/web/version.ts": ("// The API version this client sends; must equal the server's.\n"
                                      'export const API_VERSION = "2026-10-01";\n'),
}
EXAMPLE_REGEX = 'API_VERSION = "([^"]+)"'


def example_model() -> dict:
    """The model fragment `init --example` writes: two blocks, the link between
    them, and the join that compares the value both sides type by hand."""
    py, ts = (f"{EXAMPLE_DIR}/api/version.py", f"{EXAMPLE_DIR}/web/version.ts")
    return {
        "$comment": (f"A working example written by `crossweft init --example`. Delete "
                     f"{EXAMPLE_DIR}/ and this file once your own blocks are on the map. "
                     f"Field reference: {MODEL_REFERENCE_URL}"),
        "blocks": [
            {"id": "example-api", "name": "Example API", "kind": "service", "lane": "app",
             "status": "current", "summary": "Python side of the example.",
             "code": [f"{EXAMPLE_DIR}/api/"]},
            {"id": "example-web", "name": "Example web client", "kind": "site", "lane": "app",
             "status": "current", "summary": "TypeScript side of the example.",
             "code": [f"{EXAMPLE_DIR}/web/"]}],
        "links": [
            {"id": "example-web-api", "from": "example-web", "to": "example-api",
             "transport": "https", "status": "current",
             "summary": "The client sends the API version the server expects.",
             "contract": {"name": "API version", "enforcement": "duplicated"},
             "from_anchors": [{"path": ts, "find": "export const API_VERSION"}],
             "to_anchors": [{"path": py, "find": "API_VERSION ="}]}],
        "joins": [
            {"id": "example-api-version", "name": "API version", "link": "example-web-api",
             "points": [{"path": ts, "side": "example-web", "regex": EXAMPLE_REGEX},
                        {"path": py, "side": "example-api", "regex": EXAMPLE_REGEX}]}],
    }


def run_init(root: Path, language: str = "en", agents: bool = True,
             command: str = harness.DEFAULT_COMMAND, example: bool = False) -> int:
    """Create crossweft.json, an empty model skeleton and (unless agents=False)
    the agent harness; with example=True also a tiny two-file seam that the
    check guards. Never overwrites an existing file."""
    root = root.resolve()
    config_path = root / CONFIG_FILE
    if config_path.exists():
        out(f"[ERR] {config_path} already exists -- nothing written")
        return 2
    if language not in i18n.LANGUAGES:
        out(f"[ERR] language must be one of {sorted(i18n.LANGUAGES)}")
        return 2
    cfg = Config(root=root)
    model_dir = cfg.abs(cfg.model_dir)
    if model_dir.exists() and any(model_dir.iterdir()):
        out(f"[ERR] {cfg.model_dir.as_posix()} already has files -- nothing written")
        return 2
    if example and (root / EXAMPLE_DIR).exists():
        out(f"[ERR] --example writes into {EXAMPLE_DIR}/, which already exists -- nothing "
            f"written; move it away or run `crossweft init` without --example")
        return 2
    model_dir.mkdir(parents=True, exist_ok=True)
    config = {"model_dir": cfg.model_dir.as_posix(), "output_dir": cfg.output_dir.as_posix(),
              "language": language}
    if agents:
        config["agent"] = {"harness": True}
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    meta = {"meta": {"schema": SCHEMA_ID, "title": f"{root.name} system map",
                     "lanes": [{"id": "app", "name": "Application"}]}}
    (model_dir / "00-meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    body = {"$comment": f"Blocks, links and their guards. Field reference: {MODEL_REFERENCE_URL}",
            "blocks": [], "links": [], "joins": [], "sets": [], "pairs": [], "findings": []}
    (model_dir / "10-system.json").write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    out(f"[OK] wrote {CONFIG_FILE} and {cfg.model_dir.as_posix()}/ (00-meta.json, 10-system.json)")
    if example:
        for rel, text in EXAMPLE_FILES.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            with open(root / rel, "x", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        (model_dir / EXAMPLE_MODEL_FILE).write_text(
            json.dumps(example_model(), indent=2) + "\n", encoding="utf-8")
        out(f"[OK] wrote the example: {', '.join(EXAMPLE_FILES)} and "
            f"{cfg.model_dir.as_posix()}/{EXAMPLE_MODEL_FILE}")
    if agents:
        try:
            for change in harness.install(root, cfg.model_dir.as_posix(), command):
                out(f"[OK] {change}")
        except harness.HarnessError as exc:
            out(f"[ERR] agent harness not installed: {exc}. {CONFIG_FILE} asks for it, so "
                "`crossweft check` fails until `crossweft agents` succeeds.")
            return 1
    if example:
        py, ts = list(EXAMPLE_FILES)
        out(f"Next: `crossweft check` passes. Change API_VERSION in {py} only and run it again: "
            f"it fails and names {ts}, the file to bring in line. Delete {EXAMPLE_DIR}/ and "
            f"{cfg.model_dir.as_posix()}/{EXAMPLE_MODEL_FILE} when you map your own code.")
        return 0
    out("Next: `crossweft discover` lists values that already appear on both sides of a "
        "cross-language seam; add the blocks and links they belong to, then `crossweft check`.")
    return 0


def run_joint_self_tests(root_arg: Path | None) -> int:
    """`crossweft self-test` for the repository's joint-kind plugins: each one's
    own self_test() must return 0. Says so out loud when there are none."""
    try:
        root = find_repo_root(root_arg if root_arg else Path.cwd())
    except ModelError:
        out("joint plugins: none configured (no crossweft.json at or above here)")
        return 0
    try:
        cfg = load_config(root)
    except ModelError as exc:
        out(f"[FAIL] joint plugins: {exc}")
        return 1
    if cfg.joints_dir is None:
        out("joint plugins: none configured (no joints.dir in crossweft.json)")
        return 0
    plugins, errors = joints.load(cfg.root, cfg.joints_dir, RESERVED_JOINT_KINDS,
                                  set(GENERATED_FILES))
    for error in errors:
        out(f"[FAIL] {error}")
    if not plugins and not errors:
        out(f"joint plugins: none configured (no joints_<kind>.py in {cfg.joints_dir.as_posix()})")
    failed = len(errors)
    for plugin in plugins:
        why = joints.self_test(plugin)
        if why:
            failed += 1
            out(f"[FAIL] joint plugin {plugin.kind}: {why}")
        else:
            out(f"[OK] joint plugin {plugin.kind}: self_test passed")
    return 1 if failed else 0
