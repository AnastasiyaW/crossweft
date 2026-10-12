"""Built-in hazard rules: script text that runs without an error and still
breaks the agreement between a script and the program that reads its output,
its environment or its input.

Each rule is a seam no compiler sees: a batch file writing a status line that
another program parses, a parser turning a missing value into success, a
scratch variable that a compiler reads as configuration, a shell length guard
that refuses the value it was written to admit. A repository turns the rules
on with ``"hazards": {"include": [globs]}`` in ``crossweft.json``; each hit is
an ordinary, non-fatal problem keyed ``hazard:<path>:<rule>:<digest>``, so a
deliberate use is recorded as a finding with a reason, like any other drift.

Rules (``RULES``):

  cmd-digit-redirect   a variable expanded right before ``>`` / ``>>`` in a
                       batch line (``echo step=%RC%>>log``): when the value
                       is one digit, cmd reads ``0>>`` / ``1>>`` as a stream
                       handle and the digit never reaches the file.
  empty-to-zero        numeric coercion of a parsed field that may be empty
                       (PowerShell ``[int]$parts[1]``, Python ``int(x or 0)``):
                       an empty or missing status becomes 0, often "success".
  toolchain-env-name   a scratch value stored in an environment variable that
                       compilers and build tools read (``set RC=%ERRORLEVEL%``
                       before ``cmake``: CMake takes RC as the resource
                       compiler).
  glob-length-guard    ``case`` pattern of N ``?`` and a ``*`` in a reject
                       branch: it matches every value of N characters OR MORE,
                       so as a "not exactly N" guard it refuses valid input.

Standard library only; scan() is pure (text in, hits out).
"""

from __future__ import annotations

import bisect
import dataclasses
import hashlib
import re
import time

RULES = {
    "cmd-digit-redirect": (
        "a variable is expanded right before a redirect -- when its value is a single digit "
        "0-9, cmd reads it as a stream handle (`0>>`, `1>>`), so the line is not written or "
        "loses the digit. Put the redirect first: `>>\"file\" echo name=%VAR%`"),
    "empty-to-zero": (
        "a parsed field is coerced to a number without checking that it holds digits -- an "
        "empty or missing value becomes 0, which a status parser usually reads as success "
        "(fail-open). Check the text first (`-match '^[0-9]+$'`, `str.isdigit()`) and treat "
        "anything else as no verdict"),
    "toolchain-env-name": (
        "an environment variable that compilers and build tools read is set to a status (a "
        "number, a verdict word or an exit code) -- a later cmake/cl/msbuild/make run takes "
        "it as its own setting (`RC=0` becomes the resource compiler). Rename the variable "
        "(e.g. BUILD_RC)"),
    "glob-length-guard": (
        "a case pattern of N '?' and a '*' matches every value of N characters OR MORE -- "
        "including exactly N -- so this reject branch refuses valid N-character input. For an "
        "exact length test `[ \"${#VAR}\" -eq N ]`; for 'longer than N' use N+1 '?'"),
}

# Environment names read by C/C++ toolchains (MSVC cl/link/rc, CMake, make, cc).
TOOLCHAIN_ENV = ("RC", "CC", "CXX", "LIB", "INCLUDE", "LINK", "CL", "LIBPATH", "_CL_", "_LINK_")

CMD_EXT = (".cmd", ".bat")
PS_EXT = (".ps1", ".psm1")
PY_EXT = (".py",)
SH_EXT = (".sh", ".bash", ".ksh", ".zsh", ".dash")
_SH_SHEBANG = re.compile(r"#!\s*\S*(?:/|env\s+)(?:ba|da|k|z)?sh\b")


@dataclasses.dataclass
class Hit:
    rule: str
    line: int                 # 1-based
    text: str                 # the offending line, stripped
    message: str


def language(rel: str, text: str) -> str:
    lower = rel.lower()
    if lower.endswith(CMD_EXT):
        return "cmd"
    if lower.endswith(PS_EXT):
        return "ps"
    if lower.endswith(PY_EXT):
        return "py"
    if lower.endswith(SH_EXT) or _SH_SHEBANG.match(text[:200]):
        return "sh"
    return "other"


def _comment(line: str, lang: str) -> bool:
    stripped = line.strip()
    if lang == "cmd":
        return bool(re.match(r"(?i)@?rem(\s|$)|::", stripped))
    if lang in ("ps", "py", "sh"):
        return stripped.startswith("#")
    return False


# --------------------------------------------------------------- cmd-digit-redirect
# %NAME% / %NAME:~0,1% is recognised in any file, because batch text is often
# generated from another language's string literal (where `n%2>0` is arithmetic,
# so the positional forms are batch-only).
_NAMED = r"%[A-Za-z_][\w.#$@()-]*(?::[^%\r\n]*)?%"
_RE_ANY = re.compile(_NAMED + r"(?=>)")
# In a batch file every expansion is read left to right, so the closing % of
# %V% never starts a %2: %%i / %%~nxi (for variables), %%, %NAME%, %1 / %~dp1 /
# %*, !NAME! (delayed expansion).
_RE_CMD_TOKEN = re.compile(r"%%~?[a-zA-Z]+|%%|" + _NAMED + r"|%~?[a-zA-Z]*[0-9*]|"
                           r"![A-Za-z_][\w.#$@()-]*(?::[^!\r\n]*)?!")


def _live_offsets(line: str) -> set[int]:
    """Offsets of a batch line that cmd parses as syntax: outside double
    quotes and not escaped by a caret (`"` toggles, `^` escapes outside)."""
    live, quoted, i = set(), False, 0
    while i < len(line):
        ch = line[i]
        if ch == '"':
            quoted = not quoted
        elif ch == "^" and not quoted:
            i += 2                            # the escaped character is literal
            continue
        elif not quoted:
            live.add(i)
        i += 1
    return live


def _digit_redirect(line: str, lang: str) -> bool:
    if lang == "cmd":
        live = None
        for m in _RE_CMD_TOKEN.finditer(line):
            if m.group() == "%%" or not line.startswith(">", m.end()):
                continue
            live = _live_offsets(line) if live is None else live
            if m.end() in live:
                return True
        return False
    return bool(_RE_ANY.search(line))


# --------------------------------------------------------------------- empty-to-zero
_NUM_TYPES = r"(?:u?int(?:16|32|64)?|long|ulong|short|byte|double|single|float|decimal)"
# [int]$parts[1], [int]($m.Groups[1].Value), [int]$line.Split('=')[1].Trim()
# Every pattern here is linear in the line: no two adjacent quantifiers may
# take the same character (the self-test times a long pathological line).
_PS_CAST = re.compile(r"(?i)\[" + _NUM_TYPES + r"\](?:[ \t]*\()?[ \t]*\$([\w:]+)"
                      r"((?:\.\w+(?:\([^()]*\))?|\[[^\]]*\])+)")
_PS_AS = re.compile(r"(?i)\$([\w:]+)(?:\.\w+|\[[^\]]*\])*[ \t]+-as[ \t]+\[" + _NUM_TYPES + r"\]")
_PY_CALL = re.compile(r"\b(?:int|float)\(")
_PY_ARG_NAME = re.compile(r"[ \t]*([A-Za-z_]\w*)")
# the gap is bounded: `.match` runs at every `int(` call of the line
_PY_IF_ELSE = re.compile(r"[ \t]+if[ \t][^:\n]{0,200}?\belse[ \t]+0\b")
_DIGIT_CHECK = re.compile(r"(?i)TryParse|isdigit\(|isdecimal\(|isnumeric\(|\^\\d|\^\[0-9\]|\\d\+\$|\[0-9\]\+\$")
# accessors that never yield an empty value
_NEVER_EMPTY = re.compile(r"(?i)\.(?:count|length)$")


def _py_coerced_names(line: str) -> list[str]:
    """Variables of `int(x or 0)` / `int(x) if x else 0` calls on this line. The
    call's argument is found by matching parentheses (one pass), then matched
    with anchored patterns."""
    closing, stack = {}, []
    for pos, ch in enumerate(line):
        if ch == "(":
            stack.append(pos)
        elif ch == ")" and stack:
            closing[stack.pop()] = pos
    names = []
    for m in _PY_CALL.finditer(line):
        close = closing.get(m.end() - 1)
        if close is None:
            continue                      # the call does not close on this line
        if _ends_or_zero(line, m.end(), close) or _PY_IF_ELSE.match(line, close + 1):
            arg = _PY_ARG_NAME.match(line, m.end(), close)
            names.append(arg.group(1) if arg else "")
    return names


def _ends_or_zero(line: str, start: int, end: int) -> bool:
    """line[start:end] ends with `<space>or <zero>`, read backwards from `end`
    (the argument text is never rescanned, so nested calls stay linear)."""
    def back_blanks(i: int) -> int:
        while i > start and line[i - 1] in " \t":
            i -= 1
        return i
    end = back_blanks(end)
    for zero in ("0.0", "0", "'0'", '"0"'):
        if line.startswith(zero, end - len(zero)) and end - len(zero) >= start:
            i = end - len(zero)
            j = back_blanks(i)
            if j < i and j - 2 > start and line[j - 2:j] == "or" and line[j - 3].isspace():
                return True
    return False


def _empty_to_zero(lines: list[str], index: int, lang: str) -> bool:
    line = lines[index]
    names: list[str] = []
    if lang == "ps":
        names = [m.group(1) for m in _PS_CAST.finditer(line) if not _NEVER_EMPTY.search(m.group(2))]
        names += [m.group(1) for m in _PS_AS.finditer(line)]
    elif lang == "py":
        names = _py_coerced_names(line)
    # a digits-only check counts only on the same variable, on this line or
    # one of the three before it
    checks = [w for w in lines[max(0, index - 3):index + 1] if _DIGIT_CHECK.search(w)]
    flags = re.I if lang == "ps" else 0       # PowerShell names ignore case
    return any(not name or not any(re.search(r"(?<![\w])" + re.escape(name) + r"\b", w, flags)
                                   for w in checks)
               for name in names)


# ---------------------------------------------------------------- toolchain-env-name
_NAMES = "|".join(re.escape(n) for n in TOOLCHAIN_ENV)
_SET_CMD = re.compile(r"(?i)^\s*@?set\s+(?:/a\s+)?(\"?)(" + _NAMES + r")\s*=(.*)$")
_SET_PS = re.compile(r"(?i)\$env:(" + _NAMES + r")\s*(\+?=)\s*(.*)$")
_SET_NET = re.compile(r"(?i)SetEnvironmentVariable\(\s*['\"](" + _NAMES + r")['\"]\s*,\s*(.*)$")
_SET_SH = re.compile(r"^\s*(?:export|declare\s+-x|typeset\s+-x)\s+(" + _NAMES + r")=(.*)$")
_TOOLCHAIN_RUN = re.compile(
    r"(?i)(?<![\w.-])(?:cmake|msbuild|nmake|ninja|devenv|vcvars\w*|vsdevcmd|meson|make|"
    r"mingw32-make|cl(?:\.exe)?|link\.exe|rc\.exe|gcc|g\+\+|clang(?:\+\+|-cl)?|cargo|"
    r"dotnet\s+build)(?![\w-])")
# a line that only prints text never runs the toolchain (`echo make sure ...`)
_PRINT_LINE = re.compile(r"(?i)^\s*@?(?:echo|rem|::|write-(?:host|output)|print|printf)\b")
# what a scratch status looks like: a number or a verdict word ...
_STATUS_LITERAL = re.compile(r"(?i)-?\d+|true|false|yes|no|on|off|ok|pass(?:ed)?|fail(?:ed)?|"
                             r"success|error")
# ... or one expansion of an exit status or a status-named variable
_EXPANSION = re.compile(r"%([^%]+)%|!([^!]+)!|\$\{?(\?|[A-Za-z_][\w:]*)\}?")
# matched against the last `_`/`:` segment of the name only, so a long name
# full of underscores is read once
_STATUS_SEGMENT = re.compile(r"(?i)errorlevel|lastexitcode|\?|rc|ret|status|result|exit\w*|\w*code")
# NAME=value prefixes of one shell command line: `CC=1 make`
_SH_PREFIX = re.compile(r"^\s*((?:[A-Za-z_]\w*=\S*[ \t]+)+)(\S.*)$")


def _env_assignment(line: str, lang: str) -> tuple[str, str] | None:
    """(name, value) of a toolchain variable this line sets, or None."""
    m = _SET_CMD.match(line)
    if m:
        value = m.group(3).strip()
        if m.group(1) and value.endswith('"'):
            value = value[:-1]
        return m.group(2).upper(), value
    m = _SET_PS.search(line)
    if m:
        if m.group(2) == "+=":
            return None                       # extends the toolchain's own value
        return m.group(1).upper(), m.group(3).strip()
    m = _SET_NET.search(line)
    if m:
        return m.group(1).upper(), m.group(2).strip()
    if lang == "sh":
        m = _SET_SH.match(line)
        if m:
            return m.group(1), m.group(2).strip()
    return None


def _scratch_value(name: str, value: str) -> bool:
    """True when the value is a status -- a number, a verdict word, or an exit
    code -- which no toolchain variable ever holds. A tool, a path, an option
    list or another variable is left alone: it may be exactly what is meant."""
    bare = value.strip().strip("'\"").strip()
    if _STATUS_LITERAL.fullmatch(bare):
        return True
    m = _EXPANSION.fullmatch(bare)
    if not m:
        return False
    inner = (m.group(1) or m.group(2) or m.group(3) or "").split(":~")[0]
    if inner.upper().removeprefix("ENV:") == name:
        return False                           # the variable's own value
    segment = re.split(r"[_:]", inner[-256:])[-1]
    return bool(_STATUS_SEGMENT.fullmatch(segment))


def _toolchain_runs(line: str, lang: str) -> bool:
    return not _comment(line, lang) and not _PRINT_LINE.match(line) \
        and bool(_TOOLCHAIN_RUN.search(line))


def _sh_prefix_scratch(line: str) -> bool:
    """`CC=1 make`: a toolchain name given a status for one command that is a
    toolchain run."""
    m = _SH_PREFIX.match(line)
    if not m or not _TOOLCHAIN_RUN.match(m.group(2)):
        return False
    for pair in m.group(1).split():
        name, _, value = pair.partition("=")
        if name in TOOLCHAIN_ENV and _scratch_value(name, value):
            return True
    return False


# ----------------------------------------------------------------- glob-length-guard
_REJECT = re.compile(r"\b(?:exit|return)\s+[1-9]|\bfalse\b|\bdie\b|\bfail\w*|>&2|\babort\b")
_LENGTH_GLOB = re.compile(r"\?{2,}\*")


def _strip_sh_comments(text: str) -> str:
    """Blank out `# ...` comments (outside quotes), keeping offsets."""
    out, quote = [], None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(text):
                out.append(text[i:i + 2])
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t\n;|&("):
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            out.append(" " * (end - i))
            i = end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


_IN_WORD = re.compile(r"\bin\b")
# the group starts at a non-space, so the two \s* never share a run
_CLAUSE_HEAD = re.compile(r"\s*(?:\(\s*)?([^\s)][^)]*)?\)")


_CASE_EVENTS = re.compile(r"(?<![\w$-])(case|esac)(?![\w-])|;;&?|;&")


def _glob_length_hits(text: str) -> list[int]:
    """Offsets of `??..?*` patterns that open a reject branch. One pass over
    the `case` / `esac` / clause-end events: every clause is read once, with
    the cases nested in it skipped, so deep nesting stays linear."""
    clean = _strip_sh_comments(text)
    found: list[int] = []

    def clause(start: int, stop: int, nested: list) -> None:
        # the `pattern)` head comes before any case nested in the branch
        m = _CLAUSE_HEAD.match(clean, start, nested[0][0] if nested else stop)
        if not (m and m.group(1)):
            return
        patterns = [p.strip().strip("'\"") for p in m.group(1).split("|")]
        if not any(_LENGTH_GLOB.fullmatch(p) for p in patterns):
            return
        at = m.end()
        for begin, end in nested + [(stop, stop)]:      # the branch, minus nested cases
            if begin >= at:
                if _REJECT.search(clean, at, begin):
                    found.append(m.start(1))
                    return
                at = end

    # one open case: [body start or None, clause start, nested regions in the clause, case start]
    stack: list[list] = []
    for event in _CASE_EVENTS.finditer(clean):
        word = event.group(1)
        if word == "case":
            # `case WORD in`: the word is one token, so `in` follows closely
            head = _IN_WORD.search(clean, event.end(), event.end() + 512)
            body = head.end() if head else None
            stack.append([body, body, [], event.start()])
            continue
        if not stack or stack[-1][0] is None or event.start() < stack[-1][0]:
            if word == "esac" and stack:
                stack.pop()
            continue
        top = stack[-1]
        if word == "esac":
            stack.pop()
            clause(top[1], event.start(), top[2])
            if stack:
                stack[-1][2].append((top[3], event.end()))
        else:
            clause(top[1], event.start(), top[2])
            top[1], top[2] = event.end(), []
    return sorted(found)


# -------------------------------------------------------------------------- scan
def scan(rel: str, text: str, rules: set[str] | None = None) -> list[Hit]:
    """Every hazard in one file. `rules` limits the rule set (None = all)."""
    active = set(RULES) if rules is None else set(rules) & set(RULES)
    lang = language(rel, text)
    lines = text.splitlines()
    hits: list[Hit] = []

    def hit(rule: str, index: int) -> None:
        hits.append(Hit(rule, index + 1, lines[index].strip(), RULES[rule]))

    # the last line that runs the toolchain: an assignment before it is inherited
    last_run = max((i for i, line in enumerate(lines) if _toolchain_runs(line, lang)),
                   default=-1)
    for index, line in enumerate(lines):
        if _comment(line, lang):
            continue
        if "cmd-digit-redirect" in active and _digit_redirect(line, lang):
            hit("cmd-digit-redirect", index)
        if "empty-to-zero" in active and _empty_to_zero(lines, index, lang):
            hit("empty-to-zero", index)
        if "toolchain-env-name" in active:
            assignment = _env_assignment(line, lang)
            if (assignment and _scratch_value(*assignment) and index < last_run) \
                    or (lang == "sh" and _sh_prefix_scratch(line)):
                hit("toolchain-env-name", index)
    if "glob-length-guard" in active and lang == "sh":
        starts, at = [], 0
        for line in text.splitlines(keepends=True):     # the same line breaks as `lines`
            starts.append(at)
            at += len(line)
        for offset in _glob_length_hits(text):
            hit("glob-length-guard", bisect.bisect_right(starts, offset) - 1)
    hits.sort(key=lambda h: (h.line, h.rule))
    return hits


def keys(rel: str, hits: list[Hit]) -> list[str]:
    """Stable problem keys: the rule and a digest of the line's text, so a key
    survives lines moving and a finding stops excusing a line that changed."""
    seen: dict[str, int] = {}
    out = []
    for h in hits:
        digest = hashlib.sha256(" ".join(h.text.split()).encode("utf-8")).hexdigest()[:10]
        base = f"hazard:{rel}:{h.rule}:{digest}"
        seen[base] = seen.get(base, 0) + 1
        out.append(base if seen[base] == 1 else f"{base}-{seen[base]}")
    return out


# --------------------------------------------------------------------- self-test
# (file name, text, rule that must fire or None for "must stay quiet")
PLANTED = [
    ("run.cmd", 'echo step=%RC%>>"status.txt"\n', "cmd-digit-redirect"),
    ("run.cmd", "echo done=!CODE!>status.txt\n", "cmd-digit-redirect"),
    ("make_run.ps1", "$lines += 'echo step=%RC%>>\"status.txt\"'\n", "cmd-digit-redirect"),
    ("run.cmd", '>>"status.txt" echo step=%RC%\n', None),
    ("run.cmd", "echo step=%RC% >>status.txt\ncall step.cmd >out.txt 2>&1\n", None),
    ("run.cmd", 'echo "%RC%>not a redirect"\nrem echo %RC%>>x\n', None),
    ("read.ps1", "$parts = $line -split '='\n$code = [int]$parts[1]\n", "empty-to-zero"),
    ("read.ps1", "$n = $m.Groups[1].Value -as [int]\n", "empty-to-zero"),
    ("read.py", "code = int(fields.get('rc') or 0)\n", "empty-to-zero"),
    ("read.py", "code = int(raw) if raw else 0\n", "empty-to-zero"),
    ("read.ps1", "if ($parts[1] -notmatch '^[0-9]+$') { throw 'no verdict' }\n"
                 "$code = [int]$parts[1]\n", None),
    ("read.ps1", "$total = [int]$items.Count\n", None),
    ("read.py", "if not raw.isdigit():\n    raise ValueError(raw)\ncode = int(raw)\n", None),
    ("build.cmd", "set RC=%ERRORLEVEL%\ncmake --build out\n", "toolchain-env-name"),
    ("build.ps1", "$env:LIB = 'ok'\n& msbuild app.sln\n", "toolchain-env-name"),
    ("build.sh", "export CC=1\nmake all\n", "toolchain-env-name"),
    ("build.cmd", "set BUILD_RC=%ERRORLEVEL%\ncmake --build out\n", None),
    ("build.cmd", "set CC=clang-cl\nset INCLUDE=%INCLUDE%;C:\\sdk\\inc\ncmake -B out\n", None),
    ("build.cmd", "cmake --build out\nset RC=%ERRORLEVEL%\nexit /b %RC%\n", None),
    ("guard.sh", 'case "$SHA" in\n  ' + "?" * 40 + '*) echo "bad sha" >&2; exit 2 ;;\nesac\n',
     "glob-length-guard"),
    ("guard.sh", 'case $x in ????*) return 1;; *) ;; esac\n', "glob-length-guard"),
    ("guard.sh", 'if [ "${#SHA}" -ne 40 ]; then exit 2; fi\n'
                 'case "$SHA" in *[!0-9a-f]*) exit 2 ;; esac\n', None),
    ("guard.sh", 'case "$name" in ??*) echo long ;; esac\n# case $x in ????*) exit 1;; esac\n',
     None),
    # found by the independent pre-release reviews
    ("read.ps1", "if ($line -match '^[0-9]+$') { }\n$code = [int]$parts[1]\n", "empty-to-zero"),
    ("read.py", "if raw.isdigit():\n    code = int(other or 0)\n", "empty-to-zero"),
    ("build.cmd", "set RC=%STEP_EXIT_CODE%\ncmake -B out\n", "toolchain-env-name"),
    ("run.cmd", "echo args=%*>>log.txt\n", "cmd-digit-redirect"),
    ("run.cmd", "call x.cmd %V%2>nul\n", None),           # %V% then a literal 2>nul
    ("calc.py", "if n%2>0:\n    odd = True\n", None),       # arithmetic, not batch text
    ("calc.sh", "echo $((n%2>0))\n", None),
    ("build.sh", "CC=1 make all\n", "toolchain-env-name"),
    ("build.cmd", "set LIB=%DEPS_LIB%\nset LINK=-debug\nset CC=%CC_BIN%\ncmake -B out\n", None),
    ("build.sh", "export CC=$MYCC\nmake\n", None),
    ("build.cmd", "set RC=0\necho make sure the output exists\n", None),
]


def self_test() -> list[str]:
    """Planted cases: each positive must fire its rule (and only on its line
    set), each negative must stay quiet. Returns failure descriptions."""
    failures = []
    for name, text, rule in PLANTED:
        fired = [h.rule for h in scan(name, text)]
        if rule is None and fired:
            failures.append(f"hazards: safe form flagged {fired} in {name}: {text!r}")
        elif rule is not None and fired != [rule]:
            failures.append(f"hazards: {rule} expected, got {fired} in {name}: {text!r}")
    # a hit is reported on its own line, after `;;&` / `;&` clause ends too
    text = "case $x in\n" + "".join(f"  p{i}) echo {i};;&\n" for i in range(40)) \
        + "  ??*) exit 1 ;;\nesac\n"
    lines = [h.line for h in scan("g.sh", text)]
    if lines != [42]:
        failures.append(f"hazards: glob-length-guard after ;;& clauses reported on {lines}, not [42]")
    # every pattern stays linear: a long hostile line of each language is
    # scanned in well under the time a quadratic pattern needs on it
    blank = " " * 20000
    for name, text in (("a.py", "x = int(" + blank), ("a.py", "int(" * 20000 + ")" * 20000),
                       ("a.py", "x = int(a) if " + blank), ("a.ps1", "[int](" + blank),
                       ("a.cmd", '"' * 20000 + "%A%>" * 20000),
                       ("a.sh", "case x in (" + blank + "esac\n"),
                       ("a.sh", "case a in " * 1500 + "esac " * 1500),
                       ("b.cmd", "set RC=%" + "_" * 20000 + "%\n"),
                       ("b.cmd", "set RC=%" + "a_" * 10000 + "%\n"),
                       ("a.py", "int(a) if x " * 1800)):
        started = time.perf_counter()
        scan(name, text)
        if time.perf_counter() - started > 2.0:
            failures.append(f"hazards: super-linear scan of a long line in {name}")
    return failures
