# Validators that cannot silently pass

Some seams are not "value A equals value B" but a rule over code: *this
identity constant has exactly one source*, *no file calls this deprecated API*,
*every migration has a down step*. Write those as small validators and let
`crossweft validators` run them.

The failure this runner exists for: a validator that quietly stops doing its
job -- a path bug makes it scan zero files, it prints `[OK]`, and the gate
"passes" for weeks precisely because it never ran. A check that can pass
without working is worse than no check.

## The contract

`crossweft validators` discovers every `validate_*.py` in `validators.dir` and
fails the suite when any of them:

1. has no `--self-test`, or its self-test exits non-zero, or its self-test does
   not print `SELF-TEST: checks=<n>` with n > 0 (proof the self-test path ran,
   not the normal one);
2. exits non-zero on a normal run (that is a finding);
3. prints no `SCANNED: files=<n> items=<n>` line, or prints `files=0 items=0`;
4. exceeds its timeout (`validators.timeout`, per-file `validators.timeouts`;
   a timeout entry for a validator that no longer exists is also an error).
   On a timeout the validator and every process it started are killed, so a
   child it left running cannot stretch the wait.

A `validate_*.py` that is not a suite validator -- for example a commit-time
gate that judges one staged change and would misjudge a shared working tree --
goes in `validators.exclude` with the reason: `{"validate_complexity.py":
"commit gate, runs on --staged"}`. Excluded files are listed with their reason
at the top of the report; an entry naming no file, or one without a reason, is
an error.

A validator that is legitimately not applicable to this checkout prints
`APPLICABILITY: <reason>` and is reported as **SKIP**, never PASS -- and only
after the three contracts above held. A bare `APPLICABILITY:` without a reason
fails.

Validators run in parallel (`CROSSWEFT_JOBS=1` forces serial; an invalid value
is an error, not a default). Rows are always reported in name order.

## Incremental runs (the stop hook)

A full suite can take many minutes -- too long to run every time an agent
stops. `crossweft validators --incremental` reuses a phase (the self-test or
the normal run of one validator) that passed last time **only when nothing it
observed has changed**. It learns what a phase observed by running it under a
tracer (Python audit hooks, loaded through `PYTHONPATH` into the validator and
every Python process it starts):

- the bytes of every file it read -- a file read and later overwritten by the run
  stays an input;
- the state of every path it checked (`exists`, `isfile`, `stat`, `getsize`,
  `getmtime`, `DirEntry.stat()`): missing, a directory, or a file with its size
  and modification time;
- the names and kinds in every directory it listed;
- every environment variable it read (copying the whole environment, e.g. for
  a child process, makes the whole environment the input);
- the output of every read-only `git` command it ran (`ls-files`, `diff`,
  `rev-parse`, `show`, `log`, ... -- the runner runs them again and compares);
- the validator file itself, the Python runtime, the installed packages (name
  and version) and every file of the crossweft build.

What the run creates for itself is not an input: files under a directory it made
(`tempfile.mkdtemp`, `os.mkdir`, a `shutil.copytree` target), files it wrote
before reading them, and scratch files in the system temporary directory that it
removed again. `git` run inside a directory the run made (a fixture repository a
self-test builds) is compared through the git version and the global and system
git configuration. Such a call may name no path outside that directory; its
`.git` must be a plain directory inside it (no `.git` file, shared object store
or hooks -- a hook is a program the tracer may not see); its config, `-c` and
environment config may only set keys from a short allow-list that names no
program and no file (a filter, textconv or merge driver, an editor or `gpg` would
run a program behind the tracer); git environment variables may only point inside
it, and never at a configuration file; and the home directories git reads its
global configuration from must be the runner's. A global or system key that
starts a program by itself (`core.hooksPath`, `core.fsmonitor`, an editor,
`commit.gpgsign`, `gpg.*`, an include) makes fixture git not reusable; global
filter, textconv and merge drivers (git-lfs, Git for Windows' `astextplain`) are
treated as part of the git toolchain -- their configuration is compared, not what
the program reads.

A phase is never reused when the tracer cannot see everything: it started any
other program, forked, used a network socket, a database driver or `ctypes`, a
Python child did not report what it read (for example one started with `-I`),
or something it observed changed while it ran. Failed phases are never kept,
and a kept phase expires after 24 hours, because a validator may read the clock.
The report marks reused rows (`(reused: self-test, normal)`), counts them, says
why a kept phase ran again (`ran again: validate_x.py (normal: data/x.json
changed)`), and names every validator whose phase could not be kept, with the
reason.

Before any validator starts, every kept phase is checked in parallel, each path
once, so reuse is decided as of the start of the run: a file a validator writes
into the repository during the run does not make another phase run again. A file read last time whose size and modification time are unchanged (and
not within two seconds of when its hash was taken) is taken as unchanged without
reading it again, as git's index does.

Known limits, on purpose: a file rewritten with the same size and its old
modification time restored is not seen as changed; files inside installed packages are covered by their
name and version only (an edit in `site-packages` without a version change is
not seen); a validator that deliberately reads around Python (`os.environ._data`,
native code without `ctypes`) is not traced; a repository that itself lives
under the system temporary directory loses the "removed scratch file" rule's
precision; a filter, textconv or merge driver from the global or system git
configuration (git-lfs, `astextplain`) is trusted like git itself, so what it
reads (git-lfs reads `.lfsconfig` in the fixture) is not traced. The stop hook is
a convenience -- CI runs the full suite.

The record lives in the git directory (per work tree, never committed); outside
a git work tree nothing is kept and everything runs. Validators see
`CROSSWEFT_VALIDATORS_INCREMENTAL=1`, so one that wraps `crossweft check` can
run `check --incremental` there. CI and releases run plain
`crossweft validators`.

## Template

```python
"""validate_single_service_name.py -- the service name has one source."""
import re, sys
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = next((p for p in HERE.parents if (p / "crossweft.json").is_file()), None)
if ROOT is None:                                  # robust root, not __file__ depth
    sys.exit("[ERR] no crossweft.json above this validator -- misconfigured")
CANON = "src/common/names.h"
LITERAL = re.compile(r'"MyService"')

def scan(files):
    return [str(f) for f in files if LITERAL.search(f.read_text(errors="replace"))
            and f.as_posix() != (ROOT / CANON).as_posix()]

def self_test():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        bad, good = Path(tmp, "bad.cpp"), Path(tmp, "good.cpp")
        bad.write_text('auto n = "MyService";'); good.write_text("auto n = kName;")
        if not scan([bad]) or scan([good]):
            print("[SELF-TEST FAIL] planted bad input not flagged, or good input flagged")
            return 1
    print("SELF-TEST: checks=2")
    return 0

def main(argv):
    if "--self-test" in argv:
        return self_test()
    files = sorted((ROOT / "src").rglob("*.cpp")) + sorted((ROOT / "src").rglob("*.h"))
    if not files:
        print("[ERR] scanned nothing -- misconfigured (expected sources under src/)")
        return 1
    offenders = scan(files)
    for path in offenders:
        print(f"[FAIL] {path}: re-types the service name; use the constant from {CANON}")
    print(f"SCANNED: files={len(files)} items={len(offenders)}")
    return 1 if offenders else 0

sys.exit(main(sys.argv[1:]))
```

Rules of thumb: fail on zero inputs; find the root by walking up to an anchor;
plant one known-bad and one known-good input in the self-test; give every
allowlist entry a reason and fail when an entry is no longer needed.
