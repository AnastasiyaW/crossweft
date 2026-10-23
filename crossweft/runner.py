"""`crossweft validators` -- run every validator and PROVE each did real work.

The motivating defect class: a validator can silently become a no-op -- a path
bug makes it scan 0 inputs and print a vacuous "[OK]", and the gate "passes"
precisely because it never ran. Each validator should self-guard (fail loud when
it scans nothing); this meta-runner is the OUTER guard and the wiring point: it
discovers every `validate_*.py` in the configured directory and, for each,
enforces four contracts:

  1. It HAS a `--self-test`, that self-test exits 0, AND its output carries a
     `SELF-TEST: checks=<n>` line with n > 0 -- proof the self-test path ran
     (a validator that merely mentions the flag would otherwise run its normal
     path and "pass" its self-test).
  2. On a normal run it exits 0 (no findings) -- a non-zero normal exit is a
     real finding and fails the suite.
  3. Its normal-run stdout contains a `SCANNED: files=<n> items=<n>` line with
     at least ONE count > 0 -- i.e. it provably examined real inputs.
  4. It finishes within its timeout (a timeout is a FAIL row, never a crash).

If any contract breaks, the suite FAILS (exit 1).

A validator may also be legitimately NOT APPLICABLE to the checkout it is
standing on (e.g. a release-only check running on a development branch). It
declares that by printing `APPLICABILITY: <reason>` on its normal run, and this
runner reports that row as **SKIP** -- never as PASS, because no real work was
proved. A skip excuses NOTHING: all three contracts above are evaluated first,
so an `APPLICABILITY:` line cannot mask a non-zero exit, a `SCANNED files=0
items=0`, a missing `SCANNED:` line, or a missing/failing `--self-test`. The
skip must also name its reason -- a bare `APPLICABILITY:` FAILS, since a skip
nobody can read is a silent one.

Wire this into CI (and pre-commit), so a broken / no-op validator surfaces on
every PR instead of silently protecting nothing.

Validators are evaluated CONCURRENTLY (bounded thread pool) because each one is
almost entirely spent waiting on child interpreters, not on this process. That
is a scheduling change only: the reported table, the summary and the exit code
are byte-identical to a strictly serial run, because results are collected in
DISCOVERY order (sorted by validator name) and never in completion order. Each
validator writes only into its own private temp tree, so two of them cannot
collide; `CROSSWEFT_JOBS=1` forces strictly-serial execution if that ever
stops being true. An unparseable CROSSWEFT_JOBS is an error, not a default.

Usage:
    crossweft validators                      # dir from crossweft.json validators.dir
    crossweft validators --dir scripts/validators
    crossweft validators --self-test          # proves the runner catches a no-op
    CROSSWEFT_JOBS=1 crossweft validators     # force strictly serial

ASCII-only output (Windows cp1252 safe). Exit 0 = every validator either ran
real work and passed or declared itself not applicable (reported as SKIP, and
counted separately in the closing summary); exit 1 = at least one failed / did
nothing / lacks a self-test.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import vcache

# Fan-out ceiling. Each worker only babysits child interpreters, so the useful
# width is bounded by how many validators are slow rather than by cores -- and a
# cap keeps a 2-core CI runner (or a developer's laptop mid-build) from being
# starved by a check that is supposed to be cheap.
MAX_WORKERS = 8

JOBS_ENV = "CROSSWEFT_JOBS"
DEFAULT_TIMEOUT = 120

SCANNED_RE = re.compile(r"^SCANNED:\s*files=(\d+)\s+items=(\d+)\s*$", re.MULTILINE)
SELF_TEST_RE = re.compile(r"^SELF-TEST:\s*checks=(\d+)\s*$", re.MULTILINE)

# A validator declares "this checkout is not the thing I check" with an
# `APPLICABILITY: <reason>` line. Deliberately captures an EMPTY reason too, so
# a reasonless skip is a FAIL rather than falling through to a reported PASS.
APPLICABILITY_RE = re.compile(r"^APPLICABILITY:[ \t]*(.*?)[ \t\r]*$", re.MULTILINE)

# Verdicts that do not block the build. SKIP is here because a declared,
# reported non-applicability is honest -- but it is NOT "ran real work", and the
# table and the summary must keep the two apart. Single source so the suite
# result and the failure list cannot drift apart.
PASSING_VERDICTS = ("PASS", "SKIP")


class Result:
    __slots__ = ("name", "self_test", "scanned_files", "scanned_items",
                 "normal_exit", "verdict", "detail", "failure_output",
                 "applicability", "reused", "not_reusable", "ran_again")

    def __init__(self, name: str) -> None:
        self.name = name
        self.self_test = "?"        # PASS / FAIL / MISSING
        self.scanned_files = -1
        self.scanned_items = -1
        self.normal_exit = -1
        self.verdict = "?"          # PASS / SKIP / FAIL
        self.detail = ""
        self.failure_output = ""    # bounded captured output for FAIL reporting
        self.applicability = ""     # reason text when the validator declared SKIP
        self.reused: list[str] = []  # phases taken from the last pass (--incremental)
        self.not_reusable = ""      # why a phase that ran cannot be reused next time
        self.ran_again: list[str] = []  # why a kept phase was not reused this time


def _run(py_args: list[str], cwd: Path, timeout: int = DEFAULT_TIMEOUT,
         env: dict | None = None) -> subprocess.CompletedProcess:
    """Run `python <py_args>` capturing stdout+stderr as text. Force UTF-8 so a
    validator's SCANNED line is decodable regardless of console codepage.

    The timeout must hold even when the validator starts a child of its own.
    With pipes, reading the output after killing the validator waits until
    every process holding the pipe exits -- a grandchild that sleeps turns a 2s
    timeout into its whole sleep. So the output goes to temporary files (which
    nobody has to drain), and a timeout kills the validator's whole process
    tree, not just the validator.
    """
    command = [sys.executable, *py_args]
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(command, cwd=str(cwd), stdout=out, stderr=err,
                                stdin=subprocess.DEVNULL, env=env or _utf8_env(),
                                start_new_session=os.name != "nt")
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.wait()
            raise subprocess.TimeoutExpired(command, timeout, _read(out), _read(err)) from None
        except BaseException:
            # Ctrl-C: in their own session the validators do not get the
            # terminal's SIGINT, so they would outlive the runner
            _kill_tree(proc)
            proc.wait()
            raise
        return subprocess.CompletedProcess(command, proc.returncode, _read(out), _read(err))


def _read(stream) -> str:
    stream.seek(0)
    # the same newline translation text-mode pipes did
    return stream.read().decode("utf-8", errors="replace").replace("\r\n", "\n")


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill a timed-out validator and every process it started."""
    if os.name == "nt":
        # /T: the whole tree under the PID; /F: without asking it to close
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)   # start_new_session: pgid == pid
        except ProcessLookupError:
            pass                                  # the group is already gone
    proc.kill()   # the validator itself, whatever the tree kill managed


# Per-validator wall-clock bound: `validators.timeout` in crossweft.json (default
# 120s) with per-file overrides in `validators.timeouts`. A timeout is a FAIL row
# (see evaluate_validator), never a silent pass and never an unhandled crash.
TIMEOUTS: dict[str, int] = {}
TIMEOUT_DEFAULT = [DEFAULT_TIMEOUT]


def _timeout_for(script: Path) -> int:
    return TIMEOUTS.get(script.name, TIMEOUT_DEFAULT[0])


def _say(text: str = "", file=None) -> None:
    """print(), ASCII-only: a validator's output, a file name or a path may be
    any text, and a cp1252 console must not lose the table to an encode error."""
    print(text.encode("ascii", "backslashreplace").decode("ascii"), file=file)


def _utf8_env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# A failing validator must leave enough context for a CI reader to diagnose it,
# but the aggregate report must remain bounded even when a child is noisy.
# Preserve stream order explicitly: stdout first, then stderr; when it is
# truncated, retain both ends because a self-test commonly prints its failure
# only after its successful checks.
_FAILURE_OUTPUT_MAX_LINES = 12
_FAILURE_OUTPUT_MAX_CHARS = 2_000


def _failure_output(stdout: str | bytes | None,
                    stderr: str | bytes | None) -> str:
    lines: list[str] = []
    for stream_name, captured in (("stdout", stdout), ("stderr", stderr)):
        if isinstance(captured, bytes):
            text = captured.decode("utf-8", errors="replace")
        else:
            text = captured or ""
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            if line:
                lines.append(f"{stream_name}: {line}")

    display_lines = lines
    if len(lines) > _FAILURE_OUTPUT_MAX_LINES:
        # Reserve one advertised line for the omission marker, splitting the
        # remaining budget across the first context and the final failure.
        visible = _FAILURE_OUTPUT_MAX_LINES - 1
        head = (visible + 1) // 2
        tail = visible - head
        omitted = len(lines) - head - tail
        display_lines = (lines[:head]
                         + [f"... ({omitted} more output lines)"]
                         + lines[-tail:])

    excerpt = "\n".join(display_lines)
    if len(excerpt) > _FAILURE_OUTPUT_MAX_CHARS:
        head = (_FAILURE_OUTPUT_MAX_CHARS - 1) // 2
        tail = _FAILURE_OUTPUT_MAX_CHARS - 1 - head
        excerpt = excerpt[:head] + "..." + excerpt[-tail:]
    return excerpt


def _failure_report_lines(result: Result) -> list[str]:
    """Report-only text for failed rows; verdict calculation never consults it."""
    lines = [f"-> {result.detail}"] if result.detail else []
    lines.extend(f"   {line}" for line in result.failure_output.splitlines())
    return lines


def _has_self_test_flag(script: Path) -> bool:
    """A validator advertises --self-test if the literal flag appears in its
    source. (Cheaper + safer than probing exit codes for the distinction
    between 'no flag' and 'flag present but failed'.)
    """
    try:
        return "--self-test" in script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


class Reuse:
    """--incremental: the store of passing phases and the environment children get."""

    def __init__(self, root: Path) -> None:
        self.env = _utf8_env()
        self.env[vcache.INCREMENTAL_ENV] = "1"
        self.path = _reuse_record_path(root)
        self.store = vcache.Store(self.path, vcache.runtime_key())
        self.lock = threading.Lock()
        self.checked: dict[str, tuple[dict | None, str]] = {}

    def prepare(self, scripts: list[Path]) -> None:
        """Check every kept phase before any validator runs, in parallel, each path
        stated and hashed once (validators share most of what they read)."""
        jobs = [(f"{s.name}:{phase}", s) for s in scripts for phase in ("self-test", "normal")
                if self.store.get(f"{s.name}:{phase}") is not None]
        probe = vcache.Probe()

        def check(job: tuple[str, Path]) -> tuple[str, tuple[dict | None, str]]:
            key, script = job
            entry = self.store.get(key)
            why = vcache.changed(entry, script, self.env, probe)
            if why or not isinstance(entry.get("outcome"), dict):
                return key, (None, why)
            return key, (entry["outcome"], "")

        if jobs:
            with ThreadPoolExecutor(max_workers=min(16, len(jobs))) as pool:
                self.checked = dict(pool.map(check, jobs))

    def lookup(self, key: str, script: Path) -> tuple[dict | None, str]:
        """(the kept outcome, "") to reuse, or (None, why it runs again)."""
        if key in self.checked:
            return self.checked[key]
        with self.lock:
            entry = self.store.get(key)
        if entry is None:
            return None, ""
        why = vcache.changed(entry, script, self.env)
        if why or not isinstance(entry.get("outcome"), dict):
            return None, why
        return entry["outcome"], ""

    def record(self, key: str, entry: dict | None) -> None:
        with self.lock:
            self.store.put(key, entry)


def _reuse_record_path(root: Path) -> Path | None:
    """Per work tree, in the git directory; None outside git (nothing is kept)."""
    try:
        done = subprocess.run(["git", "rev-parse", "--git-path", vcache.RECORD_NAME],
                              cwd=str(root), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0 or not done.stdout.strip():
        return None
    path = Path(done.stdout.strip())
    return path if path.is_absolute() else root / path


class _Phase:
    """One run of a validator (its self-test or its normal run). Under --incremental it
    is either taken from the last pass (`outcome` set, nothing runs) or run traced and,
    when it passes, recorded for next time."""

    def __init__(self, script: Path, cwd: Path, args: list[str], reuse: "Reuse | None",
                 name: str) -> None:
        self.script, self.reuse, self.key = script, reuse, f"{script.name}:{name}"
        self.name, self.trace_dir, self.outcome, self.why = name, None, None, ""
        if reuse is not None:
            self.outcome, self.why = reuse.lookup(self.key, script)
            if self.outcome is not None:
                self.done = None
                return
            self.trace_dir = vcache.new_trace_dir()
        env = vcache.traced_env(reuse.env, self.trace_dir) if reuse is not None else None
        try:
            self.done = _run([str(script), *args], cwd, _timeout_for(script), env)
        except BaseException:
            self.forget()
            raise

    def forget(self) -> None:
        if self.reuse is not None:
            vcache.discard(self.trace_dir)
            self.reuse.record(self.key, None)

    def keep(self, outcome: dict, r: Result) -> None:
        if self.reuse is None or self.trace_dir is None:
            return
        entry, reason = vcache.snapshot(self.trace_dir, self.script, self.reuse.env)
        self.trace_dir = None
        if entry is None:
            r.not_reusable = f"{self.name}: {reason}"
            self.reuse.record(self.key, None)
            return
        entry["outcome"] = outcome
        self.reuse.record(self.key, entry)


def evaluate_validator(script: Path, cwd: Path, reuse: "Reuse | None" = None) -> Result:
    r = Result(script.name)

    # --- contract 1: has --self-test, and it passes -------------------------
    if not _has_self_test_flag(script):
        r.self_test = "MISSING"
        r.verdict = "FAIL"
        r.detail = "no --self-test flag in source"
        return r

    try:
        st = _Phase(script, cwd, ["--self-test"], reuse, "self-test")
    except subprocess.TimeoutExpired as exc:
        r.self_test = "FAIL"
        r.verdict = "FAIL"
        r.detail = f"self-test timed out after {exc.timeout:.0f}s"
        r.failure_output = _failure_output(exc.output, exc.stderr)
        return r
    if st.why:
        r.ran_again.append(f"self-test: {st.why}")
    if st.outcome is not None:
        r.reused.append("self-test")
    else:
        done = st.done
        if done.returncode != 0:
            st.forget()
            r.self_test = "FAIL"
            r.verdict = "FAIL"
            r.detail = f"self-test exit {done.returncode}"
            r.failure_output = _failure_output(done.stdout, done.stderr)
            return r
        marker = SELF_TEST_RE.search((done.stdout or "") + "\n" + (done.stderr or ""))
        if marker is None or int(marker.group(1)) <= 0:
            st.forget()
            r.self_test = "FAIL"
            r.verdict = "FAIL"
            r.detail = ("self-test exited 0 but printed no 'SELF-TEST: checks=<n>' line with n > 0 "
                        "-- cannot prove the self-test path ran")
            r.failure_output = _failure_output(done.stdout, done.stderr)
            return r
        st.keep({"checks": int(marker.group(1))}, r)
    r.self_test = "PASS"

    # --- contracts 2 & 3: normal run exits 0 AND proves work via SCANNED ----
    try:
        nr = _Phase(script, cwd, [], reuse, "normal")
    except subprocess.TimeoutExpired as exc:
        r.verdict = "FAIL"
        r.detail = f"normal run timed out after {exc.timeout:.0f}s"
        r.failure_output = _failure_output(exc.output, exc.stderr)
        return r
    if nr.why:
        r.ran_again.append(f"normal: {nr.why}")
    if nr.outcome is not None:
        r.reused.append("normal")
        r.normal_exit = 0
        r.scanned_files = int(nr.outcome.get("files", -1))
        r.scanned_items = int(nr.outcome.get("items", -1))
        r.applicability = str(nr.outcome.get("applicability", ""))
        r.verdict = "SKIP" if r.applicability else "PASS"
        return r
    done = nr.done
    r.normal_exit = done.returncode
    out = (done.stdout or "") + "\n" + (done.stderr or "")
    m = SCANNED_RE.search(out)
    if m:
        r.scanned_files = int(m.group(1))
        r.scanned_items = int(m.group(2))

    if m is None:
        nr.forget()
        r.verdict = "FAIL"
        r.detail = "no 'SCANNED:' line (cannot prove it examined anything)"
        r.failure_output = _failure_output(done.stdout, done.stderr)
        return r
    if r.scanned_files <= 0 and r.scanned_items <= 0:
        nr.forget()
        r.verdict = "FAIL"
        r.detail = "SCANNED files=0 items=0 (did nothing)"
        r.failure_output = _failure_output(done.stdout, done.stderr)
        return r
    if done.returncode != 0:
        nr.forget()
        r.verdict = "FAIL"
        r.detail = f"normal run exit {done.returncode} (finding)"
        r.failure_output = _failure_output(done.stdout, done.stderr)
        return r

    # --- applicability: only reachable once all three contracts above HELD ----
    # Deliberately last: a validator cannot buy its way out of exit-0, a
    # non-vacuous SCANNED line, or a passing --self-test by declaring a skip.
    am = APPLICABILITY_RE.search(out)
    if am is not None:
        reason = am.group(1).strip()
        if not reason:
            nr.forget()
            r.verdict = "FAIL"
            r.detail = ("bare 'APPLICABILITY:' line with no reason -- a skip "
                        "nobody can read is a silent one")
            r.failure_output = _failure_output(done.stdout, done.stderr)
            return r
        r.applicability = reason
        r.verdict = "SKIP"
    else:
        r.verdict = "PASS"
    nr.keep({"files": r.scanned_files, "items": r.scanned_items,
             "applicability": r.applicability}, r)
    return r


def discover(directory: Path, exclude=()) -> list[Path]:
    return sorted(p for p in directory.glob("validate_*.py") if p.name not in exclude)


def _print_table(results: list[Result]) -> None:
    name_w = max((len(r.name) for r in results), default=4)
    name_w = max(name_w, len("validator"))
    header = (f"  {'validator':<{name_w}}  {'self-test':<9}  "
              f"{'scanned':<22}  verdict")
    _say(header)
    _say("  " + "-" * (len(header) - 2))
    for r in results:
        if r.scanned_files >= 0 or r.scanned_items >= 0:
            scanned = f"files={r.scanned_files} items={r.scanned_items}"
        else:
            scanned = "(none)"
        line = (f"  {r.name:<{name_w}}  {r.self_test:<9}  "
                f"{scanned:<22}  {r.verdict}")
        if r.reused:
            line += "  (reused: " + ", ".join(r.reused) + ")"
        _say(line)
        if r.verdict == "SKIP":
            _say(f"  {'':<{name_w}}  -> not applicable to this checkout, so it "
                 f"proved nothing here: {r.applicability}")
        elif r.verdict == "FAIL":
            for detail_line in _failure_report_lines(r):
                _say(f"  {'':<{name_w}}  {detail_line}")


def summary_lines(results: list[Result]) -> list[str]:
    """Closing summary for a suite in which nothing FAILED.

    A pure function on purpose: classifying a row as SKIP and then telling the
    reader "all N ran real work" is the same lie, so the self-test asserts this
    TEXT, not only the verdicts. Keeping it out of main() is what makes that
    assertion possible without running a real suite.
    """
    skipped = [r for r in results if r.verdict == "SKIP"]
    if not skipped:
        return [f"[OK] all {len(results)} validators ran real work and passed."]
    lines = [f"[OK] {len(results) - len(skipped)} of {len(results)} validators ran "
             f"real work and passed; {len(skipped)} did NOT run real work here and "
             "reported APPLICABILITY (SKIP):"]
    lines.extend(f"       SKIP {r.name}: {r.applicability}" for r in skipped)
    return lines


def reuse_lines(results: list[Result]) -> list[str]:
    """What --incremental took from the last pass, and what it could not keep."""
    phases = 2 * len(results)
    reused = sum(len(r.reused) for r in results)
    lines = [f"[INFO] incremental: {reused} of {phases} phases reused from the last pass "
             "(everything they read is unchanged); the rest ran"]
    lines += [f"[INFO]   ran again: {r.name} ({why})" for r in results for why in r.ran_again]
    lines += [f"[INFO]   not reusable next time: {r.name} ({r.not_reusable})"
              for r in results if r.not_reusable]
    return lines


class ConfigError(Exception):
    """The runner was configured wrongly (bad env var, stale timeout entry)."""


def worker_count(job_count: int) -> int:
    """How many validators to have in flight at once.

    `CROSSWEFT_JOBS` is the escape hatch: set it to 1 to force strictly serial
    execution (the honest fix if some validator needs the machine to itself),
    or to a specific width on a runner where the default guess is wrong. An
    unparseable value is an ERROR: a mistyped variable must be seen, not
    quietly replaced by a default the person did not ask for.
    """
    raw = os.environ.get(JOBS_ENV, "").strip()
    if raw:
        if not raw.isdigit() or int(raw) <= 0:
            raise ConfigError(f"{JOBS_ENV}={raw!r} is not a positive integer")
        requested = int(raw)
    else:
        requested = min(MAX_WORKERS, os.cpu_count() or 1)
    return max(1, min(requested, job_count))


def run_suite(directory: Path, cwd: Path, max_workers: int | None = None,
              exclude=(), reuse: "Reuse | None" = None) -> tuple[list[Result], bool]:
    """Run every validator in `directory`. Returns (results, nothing_failed).

    `nothing_failed` is True when no validator FAILED -- a declared SKIP does not
    block the build, but it is not a claim of real work either (see _print_table
    and the closing summary, which report the two separately).

    Validators run concurrently, but `results` is ALWAYS in `discover()` order
    (sorted by name), never in completion order -- a table whose row order
    depended on which validator happened to finish first would make two runs of
    the same tree disagree, and an unstable gate is one nobody can read. Each
    validator is a separate child process building its own private fixtures, so
    concurrency changes only WHEN they run, never WHAT they conclude.
    """
    scripts = discover(directory, exclude)
    if not scripts:
        # An empty directory is a misconfiguration, not a pass -- and there is no
        # pool to build (a zero-width ThreadPoolExecutor is an error).
        return [], False
    workers = worker_count(len(scripts)) if max_workers is None else max(
        1, min(max_workers, len(scripts)))
    if workers == 1:
        results = [evaluate_validator(s, cwd, reuse) for s in scripts]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # .map yields in ARGUMENT order, which is what keeps the table stable.
            results = list(pool.map(lambda s: evaluate_validator(s, cwd, reuse), scripts))
    all_passed = bool(results) and all(r.verdict in PASSING_VERDICTS for r in results)
    return results, all_passed


def _rows(results: list[Result]) -> list[tuple]:
    """Every reported field of every row -- the unit for comparing two runs of
    one tree. Comparing verdicts alone would miss a concurrency bug that shuffled
    the scanned counts or the skip reasons between rows.
    """
    return [(r.name, r.self_test, r.scanned_files, r.scanned_items, r.normal_exit,
             r.verdict, r.detail, r.applicability) for r in results]


# --------------------------------------------------------------------------- #
# Self-test: prove the meta-runner FAILS when a planted validator is a no-op,
# is missing --self-test, or exits non-zero -- and PASSES a well-behaved one.
# Also prove a planted validator that declares `APPLICABILITY:` is reported as
# SKIP rather than PASS, and that the declaration buys it no relief from the
# exit-0 / SCANNED / --self-test contracts. Finally, prove that running the
# validators concurrently changes only WHEN they run: same rows, same order,
# same suite result as a strictly serial run of the same tree.
# --------------------------------------------------------------------------- #

_GOOD_VALIDATOR = '''\
import sys
def main(argv):
    if "--self-test" in argv:
        print("[ok] self-test"); print("SELF-TEST: checks=2"); return 0
    print("[OK] good"); print("SCANNED: files=5 items=10"); return 0
sys.exit(main(sys.argv[1:]))
'''

# No-op: prints a vacuous [OK] but SCANNED is 0/0 (the exact defect class).
_NOOP_VALIDATOR = '''\
import sys
def main(argv):
    if "--self-test" in argv:
        print("[ok] self-test"); print("SELF-TEST: checks=2"); return 0
    print("[OK] (but scanned nothing)"); print("SCANNED: files=0 items=0"); return 0
sys.exit(main(sys.argv[1:]))
'''

# Missing --self-test entirely.
_NO_SELFTEST_VALIDATOR = '''\
import sys
print("[OK] no self-test here"); print("SCANNED: files=3 items=3")
sys.exit(0)
'''

# Has self-test + SCANNED but normal run exits non-zero (a real finding).
_FINDING_VALIDATOR = '''\
import sys
def main(argv):
    if "--self-test" in argv:
        print("[ok] self-test"); print("SELF-TEST: checks=2"); return 0
    print("regression found", file=sys.stderr); print("SCANNED: files=2 items=2"); return 1
sys.exit(main(sys.argv[1:]))
'''

# Three-state validators exit 2 when the check
# could not be performed. That must stay red here: a runner that ever read 2 as
# a pass or a skip would turn "not checked" into green.
_UNKNOWN_VALIDATOR = '''\
import sys
def main(argv):
    if "--self-test" in argv:
        print("[ok] self-test"); print("SELF-TEST: checks=2"); return 0
    print("[UNKNOWN] owner file missing - not checked"); print("SCANNED: files=3 items=3"); return 2
sys.exit(main(sys.argv[1:]))
'''

# Mentions --self-test (here, in help text) but has no self-test path: running
# it with the flag runs the NORMAL path, which exits 0. Must FAIL -- the flag's
# presence in the source proves nothing.
_FAKE_SELFTEST_VALIDATOR = '''\
import sys
USAGE = "usage: validate_fake.py [--self-test]"
print("[OK] fake"); print("SCANNED: files=2 items=2")
sys.exit(0)
'''

# Has --self-test flag but the self-test FAILS.
_BAD_SELFTEST_VALIDATOR = '''\
import sys
def main(argv):
    if "--self-test" in argv:
        print("boom", file=sys.stderr); return 1
    print("[OK]"); print("SCANNED: files=1 items=1"); return 0
sys.exit(main(sys.argv[1:]))
'''

# Legitimately not applicable to this checkout: honours every contract (exit 0,
# real SCANNED line, passing self-test) and SAYS it did no real work here.
# Must be reported as SKIP -- reporting it as PASS is the defect this guards.
_APPLICABILITY_VALIDATOR = '''\
import sys
def main(argv):
    if "--self-test" in argv:
        print("[ok] self-test"); print("SELF-TEST: checks=2"); return 0
    print("[OK] NOT_APPLICABLE: this checkout is not a release build")
    print("APPLICABILITY: not-a-release-build")
    print("SCANNED: files=1 items=1"); return 0
sys.exit(main(sys.argv[1:]))
'''

# Tries to hide a real finding (exit 1) behind an APPLICABILITY line. Must FAIL.
_APPL_MASKING_FINDING = '''\
import sys
def main(argv):
    if "--self-test" in argv:
        print("[ok] self-test"); print("SELF-TEST: checks=2"); return 0
    print("regression found", file=sys.stderr)
    print("APPLICABILITY: not-a-release-build")
    print("SCANNED: files=2 items=2"); return 1
sys.exit(main(sys.argv[1:]))
'''

# Tries to excuse scanning nothing by declaring a skip. Must FAIL.
_APPL_MASKING_NOOP = '''\
import sys
def main(argv):
    if "--self-test" in argv:
        print("[ok] self-test"); print("SELF-TEST: checks=2"); return 0
    print("APPLICABILITY: not-a-release-build")
    print("SCANNED: files=0 items=0"); return 0
sys.exit(main(sys.argv[1:]))
'''

# Tries to excuse a broken self-test by declaring a skip. Must FAIL.
_APPL_MASKING_SELFTEST = '''\
import sys
def main(argv):
    if "--self-test" in argv:
        print("boom", file=sys.stderr); return 1
    print("APPLICABILITY: not-a-release-build")
    print("SCANNED: files=1 items=1"); return 0
sys.exit(main(sys.argv[1:]))
'''

# Tries to excuse emitting no SCANNED line at all by declaring a skip. Must FAIL.
_APPL_MASKING_NO_SCANNED = '''\
import sys
def main(argv):
    if "--self-test" in argv:
        print("[ok] self-test"); print("SELF-TEST: checks=2"); return 0
    print("APPLICABILITY: not-a-release-build"); return 0
sys.exit(main(sys.argv[1:]))
'''

# Declares a skip without saying what was skipped -- unreadable, so FAIL.
_APPL_BLANK_REASON = '''\
import sys
def main(argv):
    if "--self-test" in argv:
        print("[ok] self-test"); print("SELF-TEST: checks=2"); return 0
    print("APPLICABILITY:")
    print("SCANNED: files=1 items=1"); return 0
sys.exit(main(sys.argv[1:]))
'''


def _timed_validator(delay: float, files: int, items: int) -> str:
    """A well-behaved validator that takes a known time to answer.

    Used to invert finish order against name order, so a runner that collected
    rows as they completed would visibly reorder the table.
    """
    return ("import sys, time\n"
            "def main(argv):\n"
            '    if "--self-test" in argv:\n'
            '        print("[ok] self-test"); print("SELF-TEST: checks=2"); return 0\n'
            f"    time.sleep({delay})\n"
            f'    print("[OK] timed"); print("SCANNED: files={files} items={items}")\n'
            "    return 0\n"
            "sys.exit(main(sys.argv[1:]))\n")


def _run_self_test() -> int:
    import contextlib
    import io
    import tempfile

    failures: list[str] = []
    ran = [0]

    def check(desc: str, ok: bool) -> None:
        ran[0] += 1
        if ok:
            print(f"  [ok]   {desc}")
        else:
            failures.append(f"  [FAIL] {desc}")

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "validate_good.py").write_text(_GOOD_VALIDATOR, encoding="utf-8")
        (d / "validate_noop.py").write_text(_NOOP_VALIDATOR, encoding="utf-8")
        (d / "validate_no_selftest.py").write_text(_NO_SELFTEST_VALIDATOR, encoding="utf-8")
        (d / "validate_finding.py").write_text(_FINDING_VALIDATOR, encoding="utf-8")
        (d / "validate_unknown.py").write_text(_UNKNOWN_VALIDATOR, encoding="utf-8")
        (d / "validate_bad_selftest.py").write_text(_BAD_SELFTEST_VALIDATOR, encoding="utf-8")
        (d / "validate_applicability.py").write_text(_APPLICABILITY_VALIDATOR, encoding="utf-8")
        (d / "validate_appl_finding.py").write_text(_APPL_MASKING_FINDING, encoding="utf-8")
        (d / "validate_appl_noop.py").write_text(_APPL_MASKING_NOOP, encoding="utf-8")
        (d / "validate_appl_selftest.py").write_text(_APPL_MASKING_SELFTEST, encoding="utf-8")
        (d / "validate_appl_no_scanned.py").write_text(_APPL_MASKING_NO_SCANNED, encoding="utf-8")
        (d / "validate_appl_blank.py").write_text(_APPL_BLANK_REASON, encoding="utf-8")
        (d / "validate_fake_selftest.py").write_text(_FAKE_SELFTEST_VALIDATOR, encoding="utf-8")

        results, all_passed = run_suite(d, d)
        by = {r.name: r for r in results}

        check("discovered all 13 planted validators", len(results) == 13)
        check("well-behaved validator PASSES", by["validate_good.py"].verdict == "PASS")
        check("no-op (SCANNED 0/0) validator FAILS",
              by["validate_noop.py"].verdict == "FAIL")
        check("validator missing --self-test FAILS",
              by["validate_no_selftest.py"].verdict == "FAIL"
              and by["validate_no_selftest.py"].self_test == "MISSING")
        check("validator that exits non-zero on normal run FAILS",
              by["validate_finding.py"].verdict == "FAIL")
        check("validator exiting 2 (UNKNOWN: could not check) FAILS, never PASS or SKIP",
              by["validate_unknown.py"].verdict == "FAIL"
              and by["validate_unknown.py"].normal_exit == 2)
        check("validator with a FAILING self-test FAILS",
              by["validate_bad_selftest.py"].verdict == "FAIL"
              and by["validate_bad_selftest.py"].self_test == "FAIL")
        check("a validator that only MENTIONS --self-test FAILS (no SELF-TEST line)",
              by["validate_fake_selftest.py"].verdict == "FAIL"
              and by["validate_fake_selftest.py"].self_test == "FAIL")
        report = io.StringIO()
        with contextlib.redirect_stdout(report):
            _print_table(results)
        check("failed self-test output survives the formatted report",
              "boom" in report.getvalue())
        capped_output = _failure_output(
            "\n".join(f"line-{n}" for n in range(_FAILURE_OUTPUT_MAX_LINES + 1)),
            "boom")
        check("failure excerpt preserves tail evidence within its fixed line cap",
              len(capped_output.splitlines()) == _FAILURE_OUTPUT_MAX_LINES
              and "... (3 more output lines)" in capped_output
              and capped_output.endswith("stderr: boom"))
        check("suite overall result is FAIL when any validator fails", all_passed is False)

        # --- applicability: a declared skip is SKIP, never a reported PASS ---
        check("validator declaring APPLICABILITY is SKIP, not PASS",
              by["validate_applicability.py"].verdict == "SKIP")
        check("the SKIP row carries its reason (so the skip is readable)",
              by["validate_applicability.py"].applicability == "not-a-release-build")
        check("a SKIP row still had to pass its self-test and emit SCANNED",
              by["validate_applicability.py"].self_test == "PASS"
              and by["validate_applicability.py"].scanned_files == 1)

        # --- and it excuses NOTHING: each contract still bites through it ---
        check("APPLICABILITY cannot mask a non-zero exit (still FAILS)",
              by["validate_appl_finding.py"].verdict == "FAIL")
        check("APPLICABILITY cannot mask SCANNED files=0 items=0 (still FAILS)",
              by["validate_appl_noop.py"].verdict == "FAIL")
        check("APPLICABILITY cannot mask a failing --self-test (still FAILS)",
              by["validate_appl_selftest.py"].verdict == "FAIL"
              and by["validate_appl_selftest.py"].self_test == "FAIL")
        check("APPLICABILITY cannot mask a missing SCANNED line (still FAILS)",
              by["validate_appl_no_scanned.py"].verdict == "FAIL")
        check("a reasonless bare 'APPLICABILITY:' FAILS (unreadable skip)",
              by["validate_appl_blank.py"].verdict == "FAIL")

        # And a directory of ONLY the good validator must pass overall.
        d2 = d / "onlygood"
        d2.mkdir()
        (d2 / "validate_good.py").write_text(_GOOD_VALIDATOR, encoding="utf-8")
        _, only_good_passed = run_suite(d2, d2)
        check("a directory of only well-behaved validators PASSES overall",
              only_good_passed is True)

        # A legitimate skip alongside real work does not block the build, but it
        # is still counted apart from the row that actually did work.
        d4 = d / "goodplusskip"
        d4.mkdir()
        (d4 / "validate_good.py").write_text(_GOOD_VALIDATOR, encoding="utf-8")
        (d4 / "validate_applicability.py").write_text(_APPLICABILITY_VALIDATOR, encoding="utf-8")
        mixed, mixed_passed = run_suite(d4, d4)
        verdicts = sorted(r.verdict for r in mixed)
        check("good + declared-skip validators do NOT block the suite",
              mixed_passed is True)
        check("the skipped one stays counted apart from the one that ran work",
              verdicts == ["PASS", "SKIP"])

        # The closing summary is part of the contract, so assert its TEXT:
        # classifying a row as SKIP and then reporting "all N ran real work"
        # would be the same lie, and verdict-only checks cannot see it.
        mixed_summary = " ".join(summary_lines(mixed))
        check("summary does NOT claim all validators ran real work when one skipped",
              "all 2 validators ran real work" not in mixed_summary
              and "1 of 2 validators ran real work and passed" in mixed_summary)
        check("summary names the skipped validator and its reason",
              "validate_applicability.py" in mixed_summary
              and "not-a-release-build" in mixed_summary)
        check("summary of an all-real-work suite is unchanged",
              summary_lines([r for r in mixed if r.verdict == "PASS"])
              == ["[OK] all 1 validators ran real work and passed."])

        # --- concurrency changes WHEN validators run, never WHAT they report ---
        # Named so alphabetical order is the exact REVERSE of finish order: a
        # runner collecting rows as they completed would put validate_c first.
        d5 = d / "ordering"
        d5.mkdir()
        (d5 / "validate_a_slow.py").write_text(_timed_validator(0.9, 3, 30), encoding="utf-8")
        (d5 / "validate_b_mid.py").write_text(_timed_validator(0.45, 4, 40), encoding="utf-8")
        (d5 / "validate_c_fast.py").write_text(_timed_validator(0.0, 5, 50), encoding="utf-8")
        wide, wide_passed = run_suite(d5, d5, max_workers=3)
        serial, serial_passed = run_suite(d5, d5, max_workers=1)
        check("concurrent rows keep validator-name order, not finish order",
              [r.name for r in wide] == ["validate_a_slow.py", "validate_b_mid.py",
                                         "validate_c_fast.py"])
        check("concurrent run agrees with the serial run row for row",
              _rows(wide) == _rows(serial) and wide_passed is True
              and serial_passed is True)

        # The whole planted set -- passes, fails, skips and all -- must reach the
        # same verdicts either way. `results` above already ran concurrently.
        serial_all, serial_all_passed = run_suite(d, d, max_workers=1)
        check("every planted verdict is identical serial vs concurrent",
              _rows(serial_all) == _rows(results)
              and serial_all_passed == all_passed)

        # Pin CROSSWEFT_JOBS for these: reading whatever the ambient environment
        # happens to hold would make the self-test's verdict depend on the shell
        # that launched it, which is the flake these checks exist to prevent.
        prior = os.environ.get(JOBS_ENV)
        garbage_rejected = False
        try:
            os.environ.pop(JOBS_ENV, None)
            default_one, default_many = worker_count(1), worker_count(29)
            os.environ[JOBS_ENV] = "1"
            forced_serial = worker_count(29)
            os.environ[JOBS_ENV] = "not-a-number"
            try:
                worker_count(29)
            except ConfigError:
                garbage_rejected = True
        finally:
            if prior is None:
                os.environ.pop(JOBS_ENV, None)
            else:
                os.environ[JOBS_ENV] = prior
        check("worker count is bounded and never zero",
              default_one == 1 and 1 <= default_many <= MAX_WORKERS)
        check(f"{JOBS_ENV}=1 forces strictly serial execution", forced_serial == 1)
        check(f"an unparseable {JOBS_ENV} is an error, not a silent default", garbage_rejected)

        # An EMPTY directory must NOT pass (discovering zero validators is itself
        # a misconfiguration -- the meta-runner did no work).
        d3 = d / "empty"
        d3.mkdir()
        _, empty_passed = run_suite(d3, d3)
        check("an empty validator directory does NOT pass (no work done)",
              empty_passed is False)

    print()
    if failures:
        print("SELF-TEST FAILED:", file=sys.stderr)
        for f in failures:
            print(f, file=sys.stderr)
        return 1
    print("[OK] validators runner self-test passed: catches no-op, missing/failing/"
          "fake self-test, findings, and empty dirs; passes well-behaved validators; "
          "reports a declared APPLICABILITY as SKIP (not PASS) while still "
          "enforcing exit-0, SCANNED and --self-test through it; and reports "
          "the same rows in the same order whether validators run concurrently "
          "or strictly serially.")
    print(f"SELF-TEST: checks={ran[0]}")
    return 0


def run_validators(root: Path, directory: Path, timeout: int = DEFAULT_TIMEOUT,
                   timeouts: dict[str, int] | None = None,
                   exclude: dict[str, str] | None = None, incremental: bool = False) -> int:
    """Run the suite in `directory` with the repository root as cwd.

    `exclude` maps file names to the reason they are not suite validators (for
    example a commit-time gate kept in the same directory). They are listed with
    that reason, never dropped silently, and an entry naming no file is an error."""
    if not directory.is_dir():
        _say(f"[ERR] validators directory {directory} does not exist -- nothing ran.",
             file=sys.stderr)
        return 2
    exclude = exclude or {}
    present = {script.name for script in discover(directory)}
    stale = sorted(set(exclude) - present)
    if stale:
        _say(f"[ERR] validators.exclude names file(s) that do not exist: "
             f"{', '.join(stale)} -- remove them", file=sys.stderr)
        return 1
    scripts = discover(directory, exclude)
    names = {script.name for script in scripts}
    stale = sorted(set(timeouts or {}) - names)
    if stale:
        # an override for a validator that no longer exists (or is excluded) is a rotting allowlist
        _say(f"[ERR] validators.timeouts names validator(s) that do not exist or are "
             f"excluded: {', '.join(stale)} -- remove them", file=sys.stderr)
        return 1
    if not scripts:
        _say(f"[ERR] no validate_*.py to run in {directory} -- nothing ran. "
             "This is itself a misconfiguration.", file=sys.stderr)
        return 1
    TIMEOUTS.clear()
    TIMEOUTS.update(timeouts or {})
    TIMEOUT_DEFAULT[0] = timeout
    _say(f"=== validators: running {len(scripts)} validator(s) "
         f"(self-test + normal) in {directory} ===\n")
    for name in sorted(exclude):
        _say(f"  excluded (not run): {name} -- {exclude[name]}")
    if exclude:
        _say()
    reuse = None
    if incremental and not all((vcache.TRACER_DIR / name).is_file()
                               for name in ("sitecustomize.py", "crossweft_trace_sig.py")):
        _say(f"[WARN] incremental: the tracer {vcache.TRACER_DIR / 'sitecustomize.py'} is "
             "missing from this install, so nothing can be reused -- every validator runs")
    elif incremental:
        reuse = Reuse(root)
    if reuse is not None and reuse.path is None:
        _say("[INFO] incremental: not a git work tree, so nothing is kept between runs -- "
             "every validator runs")
    if reuse is not None:
        reuse.prepare(scripts)
    try:
        results, _ = run_suite(directory, root, exclude=exclude, reuse=reuse)
    except ConfigError as exc:
        _say(f"[ERR] {exc}", file=sys.stderr)
        return 2
    _print_table(results)
    if reuse is not None:
        reuse.store.save()
        for line in reuse_lines(results):
            _say(line)
    failed = [r for r in results if r.verdict not in PASSING_VERDICTS]
    _say()
    if failed:
        _say(f"FAIL: {len(failed)} of {len(results)} validator(s) did not pass. "
             "A validator that scanned nothing, lacks/fails --self-test, or "
             "reported a finding blocks the build.", file=sys.stderr)
        return 1
    # Never say "all N ran real work" while some declared themselves not
    # applicable -- that is the claim a skip must not be able to buy.
    for line in summary_lines(results):
        _say(line)
    return 0
