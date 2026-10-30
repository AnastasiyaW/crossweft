"""Fallbacks: every place where code carries on after a failure, and the owner's
decision on each.

An agent under pressure makes a failing step "work" by catching the error and
going on, by reading a setting with a default, by silencing a command. Each of
these is a decision nobody took. This module finds them and checks them
against a registry the repository keeps (``fallbacks.registry`` in
``crossweft.json``), so every such place is agreed, refused or waiting for a
decision -- never unknown.

Shapes (``KINDS``), by language:

  swallow        an error is caught and the code goes on: a Python ``except``
                 whose last statement neither raises nor ends the process, a
                 ``finally`` that returns; a Go ``if err != nil`` after which
                 the code goes on, a loop item is skipped, or a value is
                 returned in place of the error; a ``catch`` in JS/TS, C#,
                 C/C++ or PowerShell that neither rethrows, exits, surfaces the
                 error nor hands back a failure status; a JS ``.catch(...)``.
  suppress       Python ``contextlib.suppress(...)``.
  env-default    a setting read from the environment with a default:
                 ``os.environ.get(k, d)``, ``os.getenv(k, d)``,
                 ``os.getenv(k) or d`` (import aliases are followed).
  silent-continue  PowerShell ``-ErrorAction SilentlyContinue|Ignore`` and
                 ``$ErrorActionPreference = 'SilentlyContinue'``.

A site's digest covers its own text and the code it guards (the ``try`` body,
the Go statement that produced the error), so the same handler around other
code is another fallback. A site is tied to one registry entry by a marker
comment ``fallback: FB-0001`` on its line, the line above or the line below;
without a marker, by path, enclosing symbol, kind and digest.

Standard library only; ``scan()`` is pure (text in, sites out).
"""

from __future__ import annotations

import ast
import bisect
import dataclasses
import hashlib
import json
import re
from pathlib import Path

KINDS = {
    "swallow": "an error is caught and the code goes on without raising it again",
    "suppress": "errors of the listed types are silenced (contextlib.suppress)",
    "env-default": "a setting is read from the environment with a default value",
    "silent-continue": "PowerShell is told to ignore errors (SilentlyContinue / Ignore)",
}
STATUSES = ("proposed", "approved", "rejected")
ID_RE = re.compile(r"^FB-\d{4,}$")
MARKER_RE = re.compile(r"\bfallback:\s*(FB-\d{4,})\b")
# a comment that holds only a marker, with its comment sign
MARKER_COMMENT_RE = re.compile(r"(?:#|//|/\*|<#)\s*fallback:\s*FB-\d{4,}\s*(?:\*/|#>)?")
SIGN_NAMESPACE = "crossweft-fallbacks"
SIGNER_IDENTITY = "owner"
ENTRY_FIELDS = {"id", "path", "symbol", "kind", "digest", "behavior", "reason", "status",
                "by", "at", "rev", "wrapper", "proposed_by", "sig"}
SIGNED_FIELDS = ("id", "path", "symbol", "kind", "digest", "wrapper", "behavior", "status",
                 "reason", "by", "at", "rev")


@dataclasses.dataclass(frozen=True)
class Site:
    path: str
    line: int
    kind: str
    symbol: str
    digest: str
    text: str
    marker: str | None = None

    @property
    def key(self) -> str:
        return f"{self.path}:{self.symbol}:{self.kind}:{self.digest[7:15]}"


def keyed(sites: list[Site]) -> list[tuple[str, Site]]:
    """(key, site) with keys unique in file order: identical sites in one function (same
    text, same guarded code) get #2, #3... so each can be registered on its own."""
    seen: dict[str, int] = {}
    out = []
    for site in sites:
        seen[site.key] = seen.get(site.key, 0) + 1
        n = seen[site.key]
        out.append((site.key if n == 1 else f"{site.key}#{n}", site))
    return out


def _normal(text: str) -> str:
    return " ".join(MARKER_COMMENT_RE.sub("", text).split())


def _digest(text: str, guarded: str = "") -> str:
    """Of the site's text and the code it guards, without markers and with whitespace
    collapsed: adding a marker or re-indenting is not a change; any other edit is."""
    blob = _normal(text) + "\0" + _normal(guarded)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _marker(lines: list[str], line: int) -> str | None:
    """A marker on the site's line, the line above or the line below (the first line
    of a handler body) -- not further, so a neighbouring handler cannot take it."""
    for index in (line - 1, line - 2, line):
        if 0 <= index < len(lines):
            found = MARKER_RE.search(lines[index])
            if found:
                return found.group(1)
    return None


# ------------------------------------------------------------------ python
PY_STOPS = {"sys.exit", "os._exit", "exit", "quit", "os.abort"}
PY_ENV_GETS = {"os.environ.get", "os.getenv"}


class _PyVisitor(ast.NodeVisitor):
    def __init__(self, path: str, source: str, stops: frozenset = frozenset()) -> None:
        self.path, self.source, self.stops = path, source, stops
        self.lines = source.splitlines()
        self.stack: list[str] = []
        self.sites: list[Site] = []
        self.aliases: dict[str, str] = {}

    # -- names
    def visit_Import(self, node):
        for alias in node.names:
            self.aliases[alias.asname or alias.name.split(".")[0]] = \
                alias.name if alias.asname else alias.name.split(".")[0]

    def visit_ImportFrom(self, node):
        if node.module:
            for alias in node.names:
                self.aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def name(self, func: ast.AST) -> str:
        """The dotted name of a call target with import aliases resolved."""
        raw = _call_name(func)
        head, _, rest = raw.partition(".")
        full = self.aliases.get(head, head)
        return f"{full}.{rest}" if rest else full

    # -- output
    def _symbol(self) -> str:
        return ".".join(self.stack) or "<module>"

    def _add(self, node: ast.AST, kind: str, guarded: str = "") -> None:
        text = self._segment(node)
        self.sites.append(Site(self.path, node.lineno, kind, self._symbol(),
                               _digest(text, guarded),
                               text.splitlines()[0][:160] if text else "",
                               _marker(self.lines, node.lineno)))

    def _segment(self, node: ast.AST) -> str:
        """The node's source (ast.get_source_segment splits the whole file on every call,
        which is quadratic on a big file with many sites)."""
        first, last = node.lineno - 1, (node.end_lineno or node.lineno) - 1
        chunk = self.lines[first:last + 1]
        if not chunk:
            return ""
        if len(chunk) == 1:
            return chunk[0][node.col_offset:node.end_col_offset]
        return "\n".join([chunk[0][node.col_offset:], *chunk[1:-1],
                          chunk[-1][:node.end_col_offset]])

    def _body(self, statements: list[ast.stmt]) -> str:
        return "\n".join(self._segment(s) for s in statements)

    # -- scopes
    def _scoped(self, node, name: str) -> None:
        self.stack.append(name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        self._scoped(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self._scoped(node, node.name)

    # -- shapes
    def visit_Try(self, node):
        guarded = self._body(node.body)
        for handler in node.handlers:
            if not self._propagates(handler.body):
                self._add(handler, "swallow", guarded)
        if node.finalbody and any(isinstance(s, (ast.Return, ast.Break, ast.Continue))
                                  for s in node.finalbody):
            self._add(node.finalbody[0], "swallow", guarded)
        self.generic_visit(node)

    visit_TryStar = visit_Try

    def _propagates(self, body: list[ast.stmt]) -> bool:
        """The handler's last statement raises or ends the process (a raise under an
        `if` covers only that branch, so the handler still carries on otherwise)."""
        last = body[-1] if body else None
        if isinstance(last, ast.Raise):
            return True
        if isinstance(last, ast.Expr) and isinstance(last.value, ast.Call):
            name = self.name(last.value.func)
            return name in PY_STOPS or name.rsplit(".", 1)[-1] in self.stops
        return False

    def visit_With(self, node):
        for item in node.items:
            call = item.context_expr
            if isinstance(call, ast.Call) and self.name(call.func) == "contextlib.suppress":
                self._add(node, "suppress", self._body(node.body))
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    def visit_Call(self, node):
        if self.name(node.func) in PY_ENV_GETS and len(node.args) + len(node.keywords) >= 2:
            self._add(node, "env-default")
        self.generic_visit(node)

    def visit_BoolOp(self, node):
        first = node.values[0]
        if isinstance(node.op, ast.Or) and isinstance(first, ast.Call) and \
                self.name(first.func) in PY_ENV_GETS and len(first.args) + len(first.keywords) == 1:
            self._add(node, "env-default")
        self.generic_visit(node)


def _call_name(func: ast.AST) -> str:
    parts = []
    while isinstance(func, ast.Attribute):
        parts.append(func.attr)
        func = func.value
    if isinstance(func, ast.Name):
        parts.append(func.id)
    return ".".join(reversed(parts))


def _scan_python(path: str, text: str, stops: frozenset = frozenset()) -> list[Site] | str:
    text = text.lstrip("﻿")
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return f"not parsed as Python: {exc.msg} (line {exc.lineno})"
    visitor = _PyVisitor(path, text, stops)
    visitor.visit(tree)
    return visitor.sites


# ------------------------------------------------------- brace languages
REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^")
REGEX_WORDS = {"return", "typeof", "case", "in", "of", "delete", "void", "throw", "new", "else",
               "do", "yield", "await"}


def _blank(text: str, lang: str) -> str:
    """Replace string literals and comments with spaces of the same length, so offsets
    and line numbers stay and a brace or keyword inside them is not code. Knows each
    language's literals: Go raw strings, JS template and regex literals, C# verbatim
    strings, PowerShell quoting and here-strings."""
    out = list(text)
    n = len(text)

    def wipe(a: int, b: int) -> None:
        for k in range(a, min(b, n)):
            if out[k] != "\n":
                out[k] = " "

    ps = lang == "powershell"
    i = 0
    while i < n:
        ch, two = text[i], text[i:i + 2]
        if ps and two in ("@'", '@"') and text[i + 2:i + 3] in ("\n", "\r"):
            end = text.find("\n" + two[1] + "@", i + 2)
            j = n if end < 0 else end + 3
            wipe(i, j)
            i = j
            continue
        if (not ps and two == "//") or (ps and ch == "#"):
            j = text.find("\n", i)
            j = n if j < 0 else j
            wipe(i, j)
            i = j
            continue
        if (not ps and two == "/*") or (ps and two == "<#"):
            end = text.find("*/" if two == "/*" else "#>", i + 2)
            j = n if end < 0 else end + 2
            wipe(i, j)
            i = j
            continue
        if lang == "csharp" and two == '@"':
            j = i + 2
            while j < n:
                if text[j] == '"':
                    if text[j + 1:j + 2] == '"':
                        j += 2
                        continue
                    break
                j += 1
            wipe(i + 2, j)
            i = j + 1
            continue
        if lang == "js" and ch == "/" and _regex_starts(text, i):
            j = i + 1
            in_class = False
            while j < n and text[j] != "\n":
                c = text[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    break
                j += 1
            wipe(i + 1, j)
            i = j + 1
            continue
        if ch in "\"'" or (ch == "`" and lang in ("go", "js")):
            j = i + 1
            while j < n:
                c = text[j]
                if c == ch:
                    if ps and ch == "'" and text[j + 1:j + 2] == "'":
                        j += 2
                        continue
                    break
                if ps:
                    if ch == '"' and c == "`":
                        j += 2
                        continue
                elif c == "\\" and not (lang == "go" and ch == "`"):
                    j += 2
                    continue
                if c == "\n" and ch != "`" and not ps:
                    break
                j += 1
            wipe(i + 1, j)
            i = j + 1
            continue
        i += 1
    return "".join(out)


def _regex_starts(text: str, i: int) -> bool:
    """Whether the `/` at `i` opens a JS regex literal rather than a division."""
    k = i - 1
    while k >= 0 and text[k] in " \t":
        k -= 1
    if k < 0 or text[k] in "\n\r":
        return True
    if text[k] in REGEX_BEFORE:
        return text[k + 1:i + 2] != "//" and text[i + 1:i + 2] not in ("/", "*")
    word = re.search(r"(\w+)$", text[:k + 1])
    return bool(word and word.group(1) in REGEX_WORDS)


def _block(code: str, open_at: int) -> int:
    """Offset just past the brace that closes the `{` at `open_at`, or -1."""
    depth = 0
    for i in range(open_at, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def _block_before(code: str, close_at: int) -> int:
    """Offset of the `{` that opens the block whose `}` is at `close_at`, or -1."""
    depth = 0
    for i in range(close_at, -1, -1):
        if code[i] == "}":
            depth += 1
        elif code[i] == "{":
            depth -= 1
            if depth == 0:
                return i
    return -1


TRY_HEAD = re.compile(r"(?:\btry|\bcatch\b\s*(?:\([^)]*\))?\s*(?:when\s*\((?:[^()]|\([^()]*\))*\))?)\s*$",
                      re.I)


def _guarded_by_catch(text: str, code: str, catch_at: int) -> str:
    """The `try` block a catch belongs to (through earlier catches of the same chain)."""
    k = catch_at - 1
    for _ in range(64):
        while k >= 0 and code[k].isspace():
            k -= 1
        if k < 0 or code[k] != "}":
            return ""
        start = _block_before(code, k)
        if start < 0:
            return ""
        head = TRY_HEAD.search(code, max(0, start - 200), start)
        if not head:
            return ""
        if head.group(0).strip().lower().startswith("try"):
            return text[start:k + 1]
        k = head.start() - 1
    return ""


class _Text:
    """One file: its lines, line starts and enclosing-function index, built once."""

    def __init__(self, text: str, code: str, func_re: re.Pattern) -> None:
        self.text, self.code, self.lines = text, code, text.splitlines()
        self.starts = [0] + [m.end() for m in re.finditer("\n", text)]
        self.funcs: list[tuple[int, str]] = []
        for match in func_re.finditer(code):
            found = next((g for g in match.groups() if g), None)
            if found and found not in NOT_NAMES:
                self.funcs.append((match.start(), found))
        self.func_starts = [start for start, _ in self.funcs]

    def line(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset)

    def enclosing(self, offset: int) -> str:
        index = bisect.bisect_right(self.func_starts, offset) - 1
        return self.funcs[index][1] if index >= 0 else "<file>"

    def previous_line(self, offset: int) -> str:
        """The nearest non-blank code line above the one holding `offset`."""
        number = self.line(offset) - 2
        while number >= 0:
            line = self.code[self.starts[number]:self.starts[number + 1] - 1] \
                if number + 1 < len(self.starts) else self.code[self.starts[number]:]
            if line.strip():
                return self.text[self.starts[number]:self.starts[number] + len(line)]
            number -= 1
        return ""

    def site(self, path: str, start: int, end: int, kind: str, guarded: str = "") -> Site:
        snippet = self.text[start:end]
        line = self.line(start)
        return Site(path, line, kind, self.enclosing(start), _digest(snippet, guarded),
                    snippet.splitlines()[0].strip()[:160] if snippet else "",
                    _marker(self.lines, line))


GO_FUNC = re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)", re.M)
# an error variable: e, err, err2, readErr, errClose (not `body != nil`)
GO_ERR = re.compile(r"\bif\s+(?:[^{;\n]*;\s*)?(e|err\w*|\w*Err\w*)\s*!=\s*nil\s*\{")
GO_FATAL = re.compile(r"\bpanic\(|\blog\.Fatal|\bos\.Exit\(|\b\w+\.(?:Fatal|Fatalf|FailNow|Skip|Skipf)\(")
GO_RETURN = re.compile(r"\breturn\b([^\n;]*)")
GO_DEFAULT = re.compile(r"^(?:nil|true|false|-?\d[\d_.]*|\"[^\"]*\"|`[^`]*`|'[^']*'|"
                        r"[\w.\[\]*]*\{\s*\}|\w+\(\s*\))$")
GO_ERRORISH = re.compile(r"[Ee]rr|[Ff]ail|\berrors\.|\bfmt\.Errorf\(")
# a catch body that rethrows or ends the process does not hide the failure
CATCH_STOPS = re.compile(r"\bthrow\b|\bexit\s*\(|\bstd::exit\b|\babort\s*\(|\bstd::terminate\b|"
                         r"\bEnvironment\.(?:Exit|FailFast)\b|\bprocess\.exit\(|"
                         r"(?:^|[;{])\s*exit\s+[1-9]|ThrowTerminatingError\(|Promise\.reject",
                         re.I | re.M)
# calls that put the error in front of a person or a caller (logging is not one of them)
SURFACES = r"\b(?:Show|Report|Write|Raise|Send)\w*Error\w*\s*\(|\bhttp\.Error\s*\("
# a catch body that hands back a failure status (`Status::Fail(...)`, `Result.Error`,
# `worker_failed`), a negative or nonzero code, or surfaces the error: the exception is
# translated, not hidden. `return false`, a default object or a helper named
# `defaultOnError()` stay fallbacks.
CATCH_FAILS = re.compile(r"(?:\breturn\b|(?<![=!<>])=(?!=))[^;\n]*?(?:(?:::|\.)(?:Fail|Failed|Failure|"
                         r"Error|Invalid|Denied|Refused|Rejected)\w*|\b\w+_(?:failed|error|invalid|"
                         r"denied|refused)\b|-[1-9]\d*\b)|\breturn\s+[1-9]\d*\s*;|" + SURFACES)
CATCH = re.compile(r"\bcatch\b\s*(?:\([^)]*\))?\s*(?:when\s*\((?:[^()]|\([^()]*\))*\))?\s*\{", re.I)
JS_CATCH_ARROW = re.compile(r"\.catch\(\s*(?:async\s+)?(?:\w+|\([^)]*\))\s*=>\s*")
JS_CATCH_FUNCTION = re.compile(r"\.catch\(\s*(?:async\s+)?function\b[^{]*\{")
JS_CATCH_NAME = re.compile(r"\.catch\(\s*([A-Za-z_$][\w$.]*)\s*\)")
JS_FUNC = re.compile(r"(?:function\s+(\w+)|(\w+)\s*\([^)]*\)\s*\{|(?:const|let|var)\s+(\w+)\s*=)")
PS_FUNC = re.compile(r"^\s*function\s+([\w-]+)", re.M | re.I)
PS_SILENT = re.compile(r"-ErrorAction\s+['\"]?(SilentlyContinue|Ignore)\b|"
                       r"\$ErrorActionPreference\s*=\s*['\"]?(SilentlyContinue|Ignore)\b", re.I)
C_FUNC = re.compile(r"^[\w:<>,\t \*&~]+?\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*(?:const\s*)?(?:noexcept\s*)?\{",
                    re.M)
NOT_NAMES = {"if", "for", "while", "switch", "catch", "try", "return", "function", "else", "do",
             "with", "foreach", "using", "lock", "sizeof", "new", "await", "typeof"}


def _stops_re(stops: frozenset) -> re.Pattern | None:
    """Calls the repository names (`fallbacks.stops`) that report a failure and stop."""
    if not stops:
        return None
    return re.compile(r"(?<![\w.$-])(?:" + "|".join(map(re.escape, sorted(stops))) + r")(?![\w-])")


def _split_values(value: str) -> list[str]:
    """Top-level comma-separated parts of a return list (a closing brace of the
    surrounding block on the same line is dropped)."""
    value = value.strip()
    while value.endswith("}") and value.count("}") > value.count("{"):
        value = value[:-1].rstrip()
    parts, depth, current = [], 0, []
    for ch in value:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    parts.append("".join(current).strip())
    return parts


def _go_carries_on(body: str, stops: re.Pattern | None) -> bool:
    """Whether an `if err != nil` block hides the error: the code goes on, a loop item is
    skipped, or a value is returned in place of the error. A bare `return` (the handler
    wrote its error response) and a return that carries an error do not hide it."""
    if GO_FATAL.search(body) or re.search(SURFACES, body) or (stops and stops.search(body)):
        return False
    returns = [m.group(1).strip() for m in GO_RETURN.finditer(body)]
    if not returns:
        return True
    for value in returns:
        if not value:
            continue
        parts = _split_values(value)
        if len(parts) > 1:
            if not GO_DEFAULT.match(parts[-1]):
                return False            # the error position holds an error
        elif GO_ERRORISH.search(parts[0]) and not GO_DEFAULT.match(parts[0]):
            return False                # a single value that is an error
    return any(returns)


def _scan_go(path: str, text: str, stops: re.Pattern | None = None) -> list[Site]:
    code = _blank(text, "go")
    index, sites = _Text(text, code, GO_FUNC), []
    for match in GO_ERR.finditer(code):
        end = _block(code, match.end() - 1)
        if end < 0:
            continue
        if _go_carries_on(code[match.end():end - 1], stops):
            sites.append(index.site(path, match.start(), end, "swallow",
                                    index.previous_line(match.start())))
    return sites


def _catch_hides(body: str, stops: re.Pattern | None) -> bool:
    return not (CATCH_STOPS.search(body) or CATCH_FAILS.search(body)
                or (stops and stops.search(body)))


def _scan_catch(path: str, text: str, lang: str, func_re: re.Pattern,
                stops: re.Pattern | None = None) -> list[Site]:
    code = _blank(text, lang)
    index, sites = _Text(text, code, func_re), []
    for match in CATCH.finditer(code):
        end = _block(code, match.end() - 1)
        if end < 0 or not _catch_hides(code[match.end():end], stops):
            continue
        sites.append(index.site(path, match.start(), end, "swallow",
                                _guarded_by_catch(text, code, match.start())))
    if lang == "js":
        for match in list(JS_CATCH_ARROW.finditer(code)) + list(JS_CATCH_FUNCTION.finditer(code)):
            if code[match.end() - 1] == "{" or code[match.end():match.end() + 1] == "{":
                open_at = match.end() - 1 if code[match.end() - 1] == "{" else match.end()
                end = _block(code, open_at)
            else:
                stop = code.find(")", match.end())
                end = len(code) if stop < 0 else stop
            if end < 0 or not _catch_hides(code[match.end():end], stops):
                continue
            sites.append(index.site(path, match.start(), end, "swallow",
                                    index.previous_line(match.start())))
        for match in JS_CATCH_NAME.finditer(code):
            if stops and stops.fullmatch(match.group(1)):
                continue
            sites.append(index.site(path, match.start(), match.end(), "swallow",
                                    index.previous_line(match.start())))
    if lang == "powershell":
        # on the raw text (the value may be quoted), keeping only matches that start in code
        for match in PS_SILENT.finditer(text):
            if code[match.start()].isspace():
                continue
            stop = code.find("\n", match.end())
            stop = len(code) if stop < 0 else stop
            line_start = code.rfind("\n", 0, match.start()) + 1
            sites.append(index.site(path, match.start(), stop, "silent-continue",
                                    text[line_start:match.start()]))
    return sorted(sites, key=lambda s: (s.line, s.kind))


LANGUAGES = {
    ".py": "python", ".go": "go",
    ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js", ".ts": "js", ".tsx": "js",
    ".cs": "csharp", ".c": "c", ".cc": "c", ".cpp": "c", ".cxx": "c", ".h": "c", ".hpp": "c",
    ".ps1": "powershell", ".psm1": "powershell",
}
FUNC_RES = {"js": JS_FUNC, "powershell": PS_FUNC, "csharp": C_FUNC, "c": C_FUNC}


def language(path: str) -> str | None:
    return LANGUAGES.get(Path(path).suffix.lower())


def scan(path: str, text: str, stops: frozenset = frozenset()) -> list[Site] | str:
    """The fallback sites of one file, in file order; a string when the file could not
    be read as its language (a scan that silently found nothing in an unparsable file
    must not read as clean)."""
    lang = language(path)
    if lang is None:
        return f"no fallback scanner for {Path(path).suffix or 'files without an extension'}"
    if lang == "python":
        result = _scan_python(path, text, stops)
    elif lang == "go":
        result = _scan_go(path, text, _stops_re(stops))
    else:
        result = _scan_catch(path, text, lang, FUNC_RES[lang], _stops_re(stops))
    if isinstance(result, str):
        return result
    return sorted(result, key=lambda s: (s.line, s.kind))


def wrapper_calls(path: str, text: str, names: set[str]) -> list[tuple[str, int]]:
    """(name, line) of every call to a registered wrapper in a file, outside its own
    definition line."""
    if not names:
        return []
    lang = language(path)
    code = text if lang == "python" else _blank(text, lang or "c")
    pattern = re.compile(r"(?<![\w.])(" + "|".join(map(re.escape, sorted(names))) + r")\s*\(")
    starts = [0] + [m.end() for m in re.finditer("\n", text)]
    calls = []
    for match in pattern.finditer(code):
        before = code[max(0, match.start() - 12):match.start()]
        if re.search(r"\b(def|func|function)\s+$", before):
            continue
        calls.append((match.group(1), bisect.bisect_right(starts, match.start())))
    return calls


# ------------------------------------------------------------------ registry
class RegistryError(ValueError):
    pass


def load_registry(path: Path) -> tuple[list[dict], int]:
    """(entries, the highest id ever handed out); a missing file is an empty registry."""
    if not path.is_file():
        return [], 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RegistryError(f"{path.name}: not readable JSON ({exc})")
    entries = data.get("fallbacks") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise RegistryError(f"{path.name}: expected {{\"fallbacks\": [...]}}")
    last = data.get("last_id", 0)
    if not isinstance(last, int) or isinstance(last, bool) or last < 0:
        raise RegistryError(f"{path.name}: last_id must be a non-negative integer")
    seen: set[str] = set()
    for number, entry in enumerate(entries):
        where = f"{path.name}: fallbacks[{number}]"
        if not isinstance(entry, dict):
            raise RegistryError(f"{where}: not an object")
        extra = set(entry) - ENTRY_FIELDS
        if extra:
            raise RegistryError(f"{where}: unknown field(s) {sorted(extra)}")
        ident = entry.get("id")
        if not isinstance(ident, str) or not ID_RE.match(ident):
            raise RegistryError(f"{where}: id must look like FB-0001")
        if ident in seen:
            raise RegistryError(f"{where}: duplicate id {ident}")
        seen.add(ident)
        for field in ("path", "symbol", "behavior", "reason", "status"):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise RegistryError(f"{where} ({ident}): '{field}' must be a non-empty string")
        if entry["status"] not in STATUSES:
            raise RegistryError(f"{where} ({ident}): status must be one of {list(STATUSES)}")
        if entry.get("wrapper") not in (None, True):
            raise RegistryError(f"{where} ({ident}): wrapper may only be true")
        if entry.get("kind") not in KINDS:
            raise RegistryError(f"{where} ({ident}): kind must be one of {list(KINDS)}")
        if not isinstance(entry.get("digest"), str) or not entry["digest"].startswith("sha256:"):
            raise RegistryError(f"{where} ({ident}): digest must be sha256:...")
        rev = entry.get("rev", 0)
        if not isinstance(rev, int) or isinstance(rev, bool) or rev < 0:
            raise RegistryError(f"{where} ({ident}): rev must be a non-negative integer")
        last = max(last, int(ident[3:]))
    return entries, last


def save_registry(path: Path, entries: list[dict], last_id: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"$comment": "crossweft fallbacks registry: run `crossweft fallbacks` for the map",
            "last_id": last_id, "fallbacks": sorted(entries, key=lambda e: e["id"])}
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def next_id(last_id: int) -> str:
    """Never reused: a removed entry's number stays taken (last_id is kept in the file)."""
    return f"FB-{last_id + 1:04d}"


def signed_text(entry: dict) -> str:
    """What a decision signs: the site, the decision, when it was made and its revision
    (so an older signed decision cannot be put back over a newer one unnoticed)."""
    body = {k: entry.get(k) for k in SIGNED_FIELDS}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclasses.dataclass
class Match:
    """The outcome of tying the scanned sites to the registry."""
    by_entry: dict[str, list[Site]]
    unregistered: list[Site]
    unknown_markers: list[tuple[Site, str]]
    changed: dict[str, list[Site]]


def match(sites: list[Site], entries: list[dict]) -> Match:
    """Tie every site to at most one entry and every entry to at most one site."""
    known = {e["id"]: e for e in entries}
    by_entry: dict[str, list[Site]] = {e["id"]: [] for e in entries}
    changed: dict[str, list[Site]] = {}
    unregistered: list[Site] = []
    unknown: list[tuple[Site, str]] = []
    claimed: set[str] = set()

    def bind(entry: dict, site: Site) -> None:
        claimed.add(entry["id"])
        by_entry[entry["id"]].append(site)
        if entry["digest"] != site.digest:
            changed.setdefault(entry["id"], []).append(site)

    pending = []
    for site in sites:
        if not site.marker:
            pending.append(site)
            continue
        entry = known.get(site.marker)
        if entry is None:
            unknown.append((site, "names no registry entry"))
        elif (entry["path"], entry["kind"]) != (site.path, site.kind):
            unknown.append((site, f"names {entry['id']}, a {entry['kind']} in {entry['path']}"))
        elif entry["id"] in claimed:
            unknown.append((site, f"names {entry['id']}, which another site already uses"))
        elif entry["symbol"] != site.symbol and entry["digest"] != site.digest:
            unknown.append((site, f"names {entry['id']}, a fallback of {entry['symbol']}"))
        else:
            bind(entry, site)
    by_where: dict[tuple, list[dict]] = {}
    for entry in entries:
        by_where.setdefault((entry["path"], entry["symbol"], entry["kind"]), []).append(entry)
    leftovers = []
    for site in pending:
        same = [e for e in by_where.get((site.path, site.symbol, site.kind), [])
                if e["digest"] == site.digest and e["id"] not in claimed]
        if same:
            bind(same[0], site)
        else:
            leftovers.append(site)
    for site in leftovers:
        # the same place with edited text: the entry it was agreed as, when unambiguous
        near = [e for e in by_where.get((site.path, site.symbol, site.kind), [])
                if e["id"] not in claimed]
        if len(near) == 1:
            bind(near[0], site)
        else:
            unregistered.append(site)
    return Match(by_entry, unregistered, unknown, changed)


def sign(entry: dict, key: Path) -> str:
    """An SSH signature over the decision (`signed_text`) with the owner's key."""
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        message = Path(tmp) / "decision"
        message.write_bytes(signed_text(entry).encode("utf-8"))
        done = subprocess.run(["ssh-keygen", "-Y", "sign", "-n", SIGN_NAMESPACE, "-f", str(key),
                               str(message)], capture_output=True)
        if done.returncode != 0:
            raise RegistryError("ssh-keygen could not sign: "
                                + done.stderr.decode("utf-8", "replace").strip())
        return (Path(tmp) / "decision.sig").read_text(encoding="ascii")


def verify(entry: dict, signers: Path) -> str | None:
    """None when `entry["sig"]` is the owner's signature over this decision, else why not."""
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        sig = Path(tmp) / "decision.sig"
        sig.write_text(entry["sig"], encoding="ascii")
        try:
            done = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(signers),
                                   "-I", SIGNER_IDENTITY, "-n", SIGN_NAMESPACE, "-s", str(sig)],
                                  input=signed_text(entry).encode("utf-8"), capture_output=True,
                                  timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"cannot be verified: ssh-keygen did not run ({exc})"
    return None if done.returncode == 0 else "has a signature that does not verify"
