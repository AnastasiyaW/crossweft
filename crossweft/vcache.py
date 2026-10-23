"""Reuse of validator phases for `crossweft validators --incremental`.

A validator phase (its self-test, or its normal run) is reused only when everything it
observed is unchanged: the bytes of every file it read, the state (missing, directory,
or file with its size and modification time) of every path it checked, the names and
kinds in every directory it listed, every environment variable it read, the output of
every read-only git command it ran, the validator itself and the runtime. The runner
learns what a phase observed by running it under crossweft/_trace/sitecustomize.py;
anything the tracer cannot see (another program, a fork, a network, database or ctypes
call, a Python child that did not report) makes the phase untraceable, and an
untraceable phase is never reused. An input that changed while the phase ran makes it
untraceable too. Only phases that passed are recorded; a record older than MAX_AGE is
ignored, because a validator may read the clock.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import sysconfig
import tempfile
import time
from importlib import metadata
from pathlib import Path

from . import __version__

TRACE_ENV = "CROSSWEFT_TRACE_DIR"
INCREMENTAL_ENV = "CROSSWEFT_VALIDATORS_INCREMENTAL"
PACKAGE_DIR = Path(__file__).resolve().parent
TRACER_DIR = PACKAGE_DIR / "_trace"
RECORD_NAME = "crossweft-validators-reuse.json"
# simplification: a time-dependent validator (an expiry date, a clock) is trusted for at
# most this long; per-validator opt-out would be the next step if a repository needs it
MAX_AGE = 24 * 60 * 60
# where a temporary file lives is not an input: the runner and the stop hook pick them anew
VOLATILE_ENV = {"TEMP", "TMP", "TMPDIR", TRACE_ENV, "PYTHONPATH", "PYTHONIOENCODING"}
PYTHON_NAMES = {"python", "python3", "python.exe", "python3.exe", "pythonw.exe",
                os.path.basename(sys.executable).lower()}

_spec = importlib.util.spec_from_file_location("crossweft_trace_sig",
                                               TRACER_DIR / "crossweft_trace_sig.py")
_sig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sig)
path_state, listing_state = _sig.path_state, _sig.listing_state


def _sha(path: str) -> str | None:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _tree(path: str) -> str | None:
    """Every file under a directory a run copied as a whole (shutil.copytree)."""
    if not os.path.isdir(path):
        return None
    digest = hashlib.sha256()
    for folder, dirs, files in os.walk(path):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(folder, name)
            digest.update(os.path.relpath(full, path).encode("utf-8", "surrogateescape"))
            digest.update((_sha(full) or "-").encode())
    return digest.hexdigest()


def _env_digest(env: dict) -> str:
    items = sorted((k, v) for k, v in env.items() if k.upper() not in VOLATILE_ENV)
    return hashlib.sha256(json.dumps(items).encode("utf-8")).hexdigest()


def _runtime_roots() -> list[str]:
    """Files under these are the interpreter, the installed packages and crossweft: they
    are covered by the runtime key, not compared one by one."""
    roots = {sys.prefix, sys.base_prefix, sys.exec_prefix, str(PACKAGE_DIR)}
    for name in ("stdlib", "platstdlib", "purelib", "platlib"):
        try:
            roots.add(sysconfig.get_path(name))
        except KeyError:
            pass
    return sorted({os.path.normcase(os.path.abspath(r)) for r in roots if r})


def runtime_key() -> str:
    """Interpreter, installed distributions and every file of the crossweft build."""
    dists = sorted(f"{d.metadata['Name']}=={d.version}" for d in metadata.distributions()
                   if d.metadata and d.metadata["Name"])
    engine = hashlib.sha256()
    for path in sorted(p for p in PACKAGE_DIR.rglob("*")
                       if p.is_file() and "__pycache__" not in p.parts):
        engine.update(path.relative_to(PACKAGE_DIR).as_posix().encode())
        engine.update(path.read_bytes())
    blob = json.dumps([sys.version, sys.executable, sys.platform, __version__,
                       engine.hexdigest(), dists])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def traced_env(env: dict, trace_dir: str) -> dict:
    env = dict(env)
    env[TRACE_ENV] = trace_dir
    env["PYTHONPATH"] = os.pathsep.join([str(TRACER_DIR)] + ([env["PYTHONPATH"]]
                                                              if env.get("PYTHONPATH") else []))
    return env


def new_trace_dir() -> str:
    return tempfile.mkdtemp(prefix="crossweft-trace-")


def discard(trace_dir: str | None) -> None:
    if trace_dir:
        shutil.rmtree(trace_dir, ignore_errors=True)


# git subcommands that only read: the runner re-runs them and compares their output
GIT_READ_ONLY = {"ls-files", "ls-tree", "rev-parse", "diff", "show", "log", "cat-file",
                 "merge-base", "config", "grep", "blame", "for-each-ref", "describe",
                 "symbolic-ref", "check-ignore", "rev-list", "name-rev", "status"}
GIT_FORBIDDEN_ARGS = ("--batch", "--stdin", "--ext-diff", "--output", "-o", "--edit",
                      "--add", "--unset", "--replace-all", "--rename-section",
                      "--remove-section", "-w", "--write", "--no-index", "--git-dir",
                      "--work-tree", "--file", "-f", "--blob", "--contents")
# git a self-test runs on a fixture repository it created: compared through the git
# version and global configuration, never through the fixture's own (scratch) output
GIT_FIXTURE = {"init", "add", "commit", "checkout", "switch", "branch", "tag", "config",
               "rm", "mv", "status", "diff", "log", "show", "rev-parse", "ls-files", "merge",
               "reset", "update-index", "write-tree", "commit-tree", "hash-object", "cat-file",
               "symbolic-ref", "stash", "ls-tree", "rev-list", "for-each-ref", "blame",
               "check-ignore", "check-attr", "grep", "merge-base", "describe"}
# options that point a fixture call at another repository or program
GIT_FIXTURE_FORBIDDEN = ("--git-dir", "--work-tree", "--namespace", "--exec-path",
                         "--super-prefix", "--ext-diff", "--config-env")
# environment variables that name a path git uses: fine inside the scratch, never outside
# (a configuration file is never one: inside the scratch it could name any program)
GIT_PATH_ENV = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_COMMON_DIR"}
GIT_TOOLCHAIN = [["git", "--version"], ["git", "config", "--global", "--list"],
                 ["git", "config", "--system", "--list"]]
# environment variables that point git at another repository, index or configuration
GIT_LOCATION_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
                    "GIT_CONFIG", "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM")
# where git finds the global configuration: a fixture call must see the runner's
GIT_HOME_ENV = ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "HOMEDRIVE", "HOMEPATH", "APPDATA")
# `-c key=value` keys that cannot point git at other files
# configuration keys that name no program and no other file: the only ones a fixture
# repository's own config, `-c` or the environment may set (allow-list, not deny-list:
# filter/textconv/merge drivers, fsmonitor, editors, pagers, gpg and aliases all run
# programs the tracer may not see)
GIT_SAFE_CONFIG = {"user.name", "user.email", "init.defaultbranch", "core.autocrlf",
                   "core.safecrlf", "core.quotepath", "advice.detachedhead", "color.ui",
                   "core.filemode", "gc.auto", "maintenance.auto", "core.longpaths"}
# what `git init` itself writes into a new repository's config
GIT_INIT_CONFIG = {"core.repositoryformatversion", "core.filemode", "core.bare",
                   "core.logallrefupdates", "core.symlinks", "core.ignorecase",
                   "core.precomposeunicode", "extensions.objectformat",
                   "extensions.refstorage"}
# global/system keys that make a fixture call start a program on their own. Filter,
# textconv and merge drivers there (git-lfs, Git for Windows' astextplain) are treated as
# part of the git toolchain: their configuration is compared, not what the program reads
GIT_GLOBAL_PROGRAM = ("core.hookspath", "core.fsmonitor", "core.editor", "sequence.editor",
                      "commit.gpgsign", "tag.gpgsign", "gpg.", "include.", "includeif.")


def _argv(spawn: dict) -> list[str]:
    argv = list(spawn.get("argv") or [])
    if len(argv) == 1 and (" " in argv[0] or '"' in argv[0]):
        import shlex
        try:
            argv = [a.strip('"') for a in shlex.split(argv[0], posix=False)]
        except ValueError:
            return []
    return argv


def _program(spawn: dict) -> str:
    argv = _argv(spawn)
    first = spawn.get("exe") or (argv[0] if argv else "")
    return first.strip('"')


def _is_python(spawn: dict) -> bool:
    first = _program(spawn)
    return (os.path.basename(first).lower() in PYTHON_NAMES
            or os.path.normcase(first) == os.path.normcase(sys.executable))


def _inside(path: str, roots: set[str]) -> bool:
    p = os.path.normcase(os.path.abspath(path))
    return any(p == r or p.startswith(r + os.sep) for r in roots)


def _git_parts(spawn: dict) -> tuple[list[str], str, int] | None:
    """(argv, effective directory, index of the subcommand) of a git call whose global
    options are understood, or None."""
    argv = _argv(spawn)
    if not argv or os.path.basename(_program(spawn)).lower() not in ("git", "git.exe"):
        return None
    where = spawn.get("cwd") or os.getcwd()
    index = 1
    recorded = spawn.get("where")
    while index < len(argv) and argv[index].startswith("-"):
        option = argv[index]
        if option == "-C" and index + 1 < len(argv):
            where = os.path.join(where, argv[index + 1])
            index += 2
        elif option == "-c" and index + 1 < len(argv):
            if argv[index + 1].split("=", 1)[0].lower() not in GIT_SAFE_CONFIG:
                return None
            index += 2
        elif option in ("--no-pager", "--no-optional-locks", "-P"):
            index += 1
        else:
            return None
    if index >= len(argv):
        return None
    # the tracer resolved cwd and -C to the real directory when git started
    return argv, recorded or os.path.realpath(os.path.abspath(where)), index


def _forbidden(args: list[str]) -> bool:
    return any(a == f or a.startswith(f + "=") for a in args for f in GIT_FORBIDDEN_ARGS)


def _read_only_git(parts: tuple[list[str], str, int]) -> bool:
    argv, _, index = parts
    command, rest = argv[index], argv[index + 1:]
    if command not in GIT_READ_ONLY or _forbidden(rest):
        return False
    if command == "config" and not any(a in ("--get", "--get-all", "--get-regexp", "-l",
                                             "--list") for a in rest):
        return False
    return True


def _points_elsewhere(env: dict | None, where: str, made: set[str]) -> bool:
    """A git environment variable that sends git outside the scratch, or sets
    configuration that could."""
    import re
    for key, value in (env or {}).items():
        name = key.upper()
        if name in GIT_PATH_ENV:
            if not _inside(os.path.realpath(os.path.join(where, value)), made):
                return True
        elif name == "GIT_ALTERNATE_OBJECT_DIRECTORIES":
            if any(p and not _inside(os.path.realpath(os.path.join(where, p)), made)
                   for p in value.split(os.pathsep)):
                return True
        elif name.startswith("GIT_CONFIG_KEY_"):
            if value.lower() not in GIT_SAFE_CONFIG:
                return True
        elif name == "GIT_CONFIG_PARAMETERS":
            if any(k.lower() not in GIT_SAFE_CONFIG for k in re.findall(r"'([^'=]+)", value)):
                return True
        elif name.startswith(GIT_LOCATION_ENV) and name not in (
                "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_COUNT", "GIT_CEILING_DIRECTORIES",
                "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_NAMESPACE") \
                and not name.startswith("GIT_CONFIG_VALUE_"):
            return True
    return False


def _fixture_git(parts: tuple[list[str], str, int], made: set[str], spawn: dict,
                 env: dict) -> bool:
    """git run inside a directory the run created, naming no path outside it, with no
    environment variable pointing it at another repository."""
    argv, where, index = parts
    rest = argv[index + 1:]
    if argv[index] not in GIT_FIXTURE or not _inside(where, made):
        return False
    if any(a == f or a.startswith(f + "=") for a in rest for f in GIT_FIXTURE_FORBIDDEN):
        return False
    if _points_elsewhere(spawn.get("env"), where, made) or _points_elsewhere(env, where, made):
        return False
    seen = {k.upper(): v for k, v in (spawn.get("env") or {}).items()}
    ours = {k.upper(): v for k, v in env.items()}
    if any(seen.get(key) != ours.get(key) for key in GIT_HOME_ENV):
        return False    # the global configuration came from somewhere else
    # the repository git will use, found by the tracer when git started: `init` creates
    # one where it runs; anything else needs a plain .git directory inside the scratch
    marker = spawn.get("gitdir")
    if argv[index] != "init":
        if not marker or not marker.get("plain") or not _inside(marker["path"], made):
            return False
    for arg in argv[index + 1:]:
        value = arg.split("=", 1)[-1]
        looks_like_path = os.path.isabs(value) or os.sep in value or "/" in value or ".." in value
        if looks_like_path and not _inside(os.path.join(where, value), made):
            return False
    return True


def _git_config_get(key: str, env: dict) -> list[str]:
    import subprocess
    values = []
    for scope in ("--system", "--global"):
        try:
            done = subprocess.run(["git", "config", scope, "--path", "--get", key], env=env,
                                  capture_output=True, text=True, timeout=60,
                                  stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0 and done.stdout.strip():
            values.append(done.stdout.strip())
    return values


def _git_global_files(env: dict) -> list[str]:
    """Files outside the scratch that fixture git reads besides its configuration: the
    global and system ignore and attributes files and the init template directory."""
    import subprocess
    # git's own order (Git for Windows: HOMEDRIVE+HOMEPATH before USERPROFILE)
    drive_path = (env.get("HOMEDRIVE") or "") + (env.get("HOMEPATH") or "")
    home = (env.get("HOME") or (drive_path if env.get("HOMEPATH") else "")
            or env.get("USERPROFILE") or os.path.expanduser("~"))
    xdg = env.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    paths = [os.path.join(xdg, "git", "ignore"), os.path.join(xdg, "git", "attributes")]
    for key in ("core.excludesFile", "core.attributesFile", "init.templateDir"):
        paths += _git_config_get(key, env)
    try:
        done = subprocess.run(["git", "var", "GIT_ATTR_SYSTEM"], env=env, capture_output=True,
                              text=True, timeout=60, stdin=subprocess.DEVNULL)
        if done.returncode == 0 and done.stdout.strip():
            paths.append(done.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    exec_path = shutil.which("git")
    if exec_path:   # git without `git var GIT_ATTR_SYSTEM` (before 2.42): <prefix>/etc
        prefix = os.path.dirname(os.path.dirname(os.path.realpath(exec_path)))
        paths.append(os.path.join(prefix, "etc", "gitattributes"))
    return sorted({os.path.realpath(os.path.expanduser(p)) for p in paths})


def _global_program_key(env: dict) -> str | None:
    """A global or system configuration key that could make fixture git start a program
    (core.hooksPath, a filter such as git-lfs, an editor, ...), or None."""
    import subprocess
    nosystem = env.get("GIT_CONFIG_NOSYSTEM", "").lower() in ("1", "true", "yes", "on")
    for scope in ("--global",) + (() if nosystem else ("--system",)):
        try:
            done = subprocess.run(["git", "config", scope, "--name-only", "--list"],
                                  env=env, capture_output=True, text=True, timeout=60,
                                  stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            return f"git config {scope} could not be read"
        if done.returncode not in (0, 1):
            return f"git config {scope} could not be read"
        for key in done.stdout.split():
            key = key.lower()
            if key.startswith(GIT_GLOBAL_PROGRAM) or key in GIT_GLOBAL_PROGRAM:
                return key
    return None


def _config_file_keys(text: str) -> list[str] | None:
    """The keys a git config file sets, as git itself reads them (a key on the section
    line, comments, continuations, old [section.sub] syntax); None when git refuses it."""
    import subprocess
    fd, path = tempfile.mkstemp(prefix="crossweft-gitconfig-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        done = subprocess.run(["git", "config", "--file", path, "--no-includes",
                               "--name-only", "--list"], capture_output=True, text=True,
                              timeout=60, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return done.stdout.split() if done.returncode == 0 else None


def _fixture_config_problem(spawn: dict) -> str | None:
    """Why the fixture repository's own config may start a program, or None."""
    marker = spawn.get("gitdir") or {}
    if not marker:
        return None
    text = marker.get("config_text")
    keys = _config_file_keys(text) if isinstance(text, str) else None
    if keys is None:
        return "its config could not be read"
    for key in keys:
        if key.lower() not in GIT_SAFE_CONFIG | GIT_INIT_CONFIG:
            return f"its config sets {key}"
    return None


def git_output(command: list[str], cwd: str, env_overlay: dict | None, env: dict) -> str | None:
    import subprocess
    run_env = dict(env)
    run_env.update(env_overlay or {})
    try:
        done = subprocess.run(command, cwd=cwd, env=run_env, capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    digest = hashlib.sha256()
    for part in (str(done.returncode).encode(), b"\0", done.stdout, b"\0", done.stderr):
        digest.update(part)
    return digest.hexdigest()


def _load(trace_dir: str) -> tuple[dict, set] | str:
    try:
        directory = Path(trace_dir)
        finished = {int(p.stem): json.loads(p.read_text(encoding="utf-8"))
                    for p in directory.glob("*.json")}
        started = {int(p.stem) for p in directory.glob("*.start")}
    except (OSError, ValueError) as exc:
        return f"trace unreadable ({exc})"
    finally:
        shutil.rmtree(trace_dir, ignore_errors=True)
    return finished, started


def snapshot(trace_dir: str, script: Path, env: dict) -> tuple[dict | None, str]:
    """What a finished phase observed, as a record that `unchanged` can test later;
    (None, reason) when the phase cannot be reused."""
    loaded = _load(trace_dir)
    if isinstance(loaded, str):
        return None, loaded
    finished, started = loaded
    if not finished:
        return None, "the validator did not report what it read"
    if started - set(finished):
        return None, "a Python process of the validator exited without reporting what it read"
    children: dict[int, int] = {}
    for proc in finished.values():
        children[proc["ppid"]] = children.get(proc["ppid"], 0) + 1
    made = {os.path.normcase(m) for proc in finished.values() for m in proc.get("made", [])}
    reasons, gits, fixture_git = set(), [], False
    for pid, proc in finished.items():
        reasons.update(proc.get("untraceable", []))
        spawns = proc.get("spawns", [])
        expected = 0
        for spawn in spawns:
            if _is_python(spawn):
                expected += 1
                continue
            parts = _git_parts(spawn)
            if parts is not None and _inside(parts[1], made):
                if (spawn.get("gitdir") or {}).get("hooks"):
                    reasons.add("git in a fixture repository that has a hook")
                    continue
                problem = _fixture_config_problem(spawn)
                if problem:
                    reasons.add(f"git in a fixture repository: {problem}")
                    continue
                if _fixture_git(parts, made, spawn, env):
                    fixture_git = True
                    continue
            elif parts is not None and _read_only_git(parts):
                gits.append({"argv": parts[0], "cwd": spawn.get("cwd"), "env": spawn.get("env")})
                continue
            reasons.add(f"started {os.path.basename(_program(spawn))[:40] or '?'}"
                        + (" (not a read-only or fixture git call)" if parts else ""))
        # Windows: a process started without subprocess (multiprocessing) only shows here
        expected += max(0, proc.get("createprocess", 0) - len(spawns))
        if children.get(pid, 0) != expected:
            reasons.add("a Python child process did not report what it read")
    if fixture_git and not reasons:
        key = _global_program_key(env)
        if key:
            reasons.add(f"git in a fixture repository may start a program ({key} is configured)")
    if reasons:
        return None, "; ".join(sorted(reasons)[:3])

    roots = _runtime_roots()
    temp_root = os.path.normcase(os.path.abspath(tempfile.gettempdir()))

    def own(path: str) -> bool:
        p = os.path.normcase(path)
        return any(p == r or p.startswith(r + os.sep) for r in roots) or _inside(path, made)

    reads: dict[str, str | None] = {}
    read_states: dict[str, str] = {}
    states: dict[str, str] = {}
    lists: dict[str, str | None] = {}
    for proc in finished.values():
        for path, seen in proc.get("reads", {}).items():
            if own(path) or path in reads:
                continue
            now = path_state(path)
            if now == "missing" and seen != "missing" and _inside(path, {temp_root}):
                continue    # a scratch file in TEMP the run made and removed again
            if now != seen:
                return None, f"{path} changed while the validator ran"
            reads[path] = _sha(path) if now.startswith("file:") else now
            read_states[path] = now
        for path, seen in proc.get("stats", {}).items():
            if own(path):
                continue
            if path in states and states[path] != seen:
                return None, f"{path} changed while the validator ran"
            if path_state(path) != seen:
                if path_state(path) == "missing" and _inside(path, {temp_root}):
                    continue
                return None, f"{path} changed while the validator ran"
            states[path] = seen
        for path, seen in proc.get("lists", {}).items():
            if own(path):
                continue
            if listing_state(path) != seen:
                if listing_state(path) is None and _inside(path, {temp_root}):
                    continue
                return None, f"{path} changed while the validator ran"
            lists[path] = seen
    trees = {p: _tree(p) for proc in finished.values() for p in proc.get("trees", [])
             if not own(p)}
    if fixture_git:
        gits += [{"argv": argv, "cwd": os.getcwd(), "env": None} for argv in GIT_TOOLCHAIN]
        for path in _git_global_files(env):
            if os.path.isdir(path):
                trees[path] = _tree(path)
            else:
                state = path_state(path)
                reads[path] = _sha(path) if state.startswith("file:") else state
    env_keys: dict[str, str | None] = {}
    env_all = False
    for proc in finished.values():
        env_all = env_all or proc.get("env_all", False)
        for key, value in proc.get("env", {}).items():
            if key.upper() not in VOLATILE_ENV:
                env_keys.setdefault(key, value)
    return {"script": _sha(str(script)), "reads": reads, "read_states": read_states,
            "states": states, "lists": lists,
            "trees": trees, "env": env_keys,
            "env_all": _env_digest(env) if env_all else None,
            "git": [dict(g, out=git_output(g["argv"], g["cwd"], g["env"], env)) for g in gits],
            "at": time.time()}, ""


# a file whose modification time is this close to when its hash was taken may have
# changed again within the same clock tick: its hash is checked, not its stat
RACY_NS = 2_000_000_000


class Probe:
    """Path states, listings, hashes and trees, each computed once per lookup pass.
    Only valid while nothing runs: the runner checks every kept phase before any
    validator starts."""

    def __init__(self) -> None:
        import threading
        self._lock = threading.Lock()
        self._memo: dict[tuple[str, str], object] = {}

    def _get(self, kind: str, path: str, compute):
        key = (kind, path)
        with self._lock:
            if key in self._memo:
                return self._memo[key]
        value = compute(path)
        with self._lock:
            self._memo[key] = value
        return value

    def state(self, path: str) -> str:
        return self._get("state", path, path_state)

    def listing(self, path: str) -> str | None:
        return self._get("list", path, listing_state)

    def sha(self, path: str) -> str | None:
        return self._get("sha", path, _sha)

    def tree(self, path: str) -> str | None:
        return self._get("tree", path, _tree)

    def read_same(self, path: str, digest: str | None, seen: str | None, at: float) -> bool:
        """Whether a file read last time still has the bytes it had. An unchanged size
        and modification time (not within RACY_NS of the hash) stands for the bytes,
        as in git's index; anything else is hashed."""
        now = self.state(path)
        if not now.startswith("file:"):
            return now == digest
        if seen == now and int(now.rsplit(":", 1)[1]) < int(at * 1e9) - RACY_NS:
            return True
        return self.sha(path) == digest


def changed(entry: dict, script: Path, env: dict, probe: Probe | None = None) -> str:
    """Why a kept phase cannot be reused now ("" when everything it observed is the same);
    the first difference found, in plain words for the report."""
    probe = probe or Probe()
    if time.time() - entry.get("at", 0) > MAX_AGE:
        return "kept longer than 24 hours"
    if entry.get("script") != probe.sha(str(script)):
        return "the validator changed"
    if entry.get("env_all") is not None and entry["env_all"] != _env_digest(env):
        return "the environment changed"
    for key, value in entry.get("env", {}).items():
        if env.get(key) != value:
            return f"environment variable {key} changed"
    for path, state in entry.get("states", {}).items():
        if probe.state(path) != state:
            return f"{path} changed"
    for path, digest in entry.get("lists", {}).items():
        if probe.listing(path) != digest:
            return f"the listing of {path} changed"
    for path, digest in entry.get("trees", {}).items():
        if probe.tree(path) != digest:
            return f"files under {path} changed"
    seen_states = entry.get("read_states", {})
    for path, digest in entry.get("reads", {}).items():
        if not probe.read_same(path, digest, seen_states.get(path), entry.get("at", 0)):
            return f"{path} changed"
    for git in entry.get("git", []):
        now = git_output(git["argv"], git["cwd"], git["env"], env) if git.get("out") else None
        if now is None:
            return f"`{' '.join(git['argv'][:4])}` could not be run again"
        if now != git["out"]:
            return f"the output of `{' '.join(git['argv'][:4])}` changed"
    return ""


class Store:
    """Per work tree, in the git directory (never committed): {runtime, phases}."""

    def __init__(self, path: Path | None, runtime: str) -> None:
        self.path, self.runtime = path, runtime
        self.phases: dict[str, dict] = {}
        if path is None:
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict) and data.get("runtime") == runtime:
            self.phases = data.get("phases", {}) if isinstance(data.get("phases"), dict) else {}

    def get(self, key: str) -> dict | None:
        entry = self.phases.get(key)
        return entry if isinstance(entry, dict) else None

    def put(self, key: str, entry: dict | None) -> None:
        if entry is None:
            self.phases.pop(key, None)
        else:
            self.phases[key] = entry

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"runtime": self.runtime, "phases": self.phases}),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass
