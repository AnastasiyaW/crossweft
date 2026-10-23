"""Input tracer for `crossweft validators --incremental` (not imported by crossweft itself).

The runner puts this directory first on PYTHONPATH of a validator it traces and sets
CROSSWEFT_TRACE_DIR. Every Python process of the validator's tree then records what it
observed -- each file it read (with its state when first read), the state of each path it
checked, each directory listing, the environment variables it read -- and which
processes it started, and writes `<pid>.json` into that directory when it exits. A
process that dies without writing it leaves only `<pid>.start`, which the runner treats
as untraceable.
"""
import os
import sys

_DIR = os.environ.get("CROSSWEFT_TRACE_DIR")


def _load_next_sitecustomize() -> None:
    # this file shadows any sitecustomize the environment already has: run that one too
    import importlib.machinery
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    path = [p for p in sys.path if os.path.abspath(p or ".") != here]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", path)
    if spec is not None and spec.loader is not None:
        module = importlib.util.module_from_spec(spec)
        sys.modules["_crossweft_next_sitecustomize"] = module
        spec.loader.exec_module(module)


def _install(trace_dir: str) -> None:
    import atexit
    import json
    import genericpath
    import ntpath
    import posixpath
    from crossweft_trace_sig import listing_state, path_state

    real_stat, real_lstat, real_scandir = os.stat, os.lstat, os.scandir
    record = {"pid": os.getpid(), "ppid": os.getppid(), "exe": sys.executable,
              "argv": list(sys.argv), "reads": {}, "writes": set(), "made": set(),
              "trees": set(), "lists": {}, "stats": {}, "env": {}, "env_all": False,
              "spawns": [], "untraceable": set()}
    busy = [False]

    def norm(path):
        if isinstance(path, int):
            return None
        path = os.fspath(path)
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        return os.path.realpath(os.path.abspath(path))

    def note_read(path):
        # a path written earlier by this process is its own; a path read first is an
        # input even if the process overwrites it later
        if path in record["reads"] or path in record["writes"]:
            return
        record["reads"][path] = path_state(path, real_stat, real_lstat)

    def note_stat(path):
        if path not in record["stats"]:
            record["stats"][path] = path_state(path, real_stat, real_lstat)

    def note_list(path):
        if path not in record["lists"]:
            record["lists"][path] = listing_state(path, real_scandir)

    def git_marker(where):
        """The first .git above `where`, as git would find it, and whether it can lead
        git anywhere else (a .git file, a shared common dir, alternates, a work tree or
        includes in its config)."""
        current = where
        while True:
            candidate = os.path.join(current, ".git")
            if os.path.lexists(candidate):
                info = {"path": os.path.realpath(candidate),
                        "plain": os.path.isdir(candidate) and not os.path.islink(candidate)}
                if info["plain"]:
                    raw_config = ""
                    try:
                        with open(os.path.join(candidate, "config"), encoding="utf-8",
                                  errors="replace") as fh:
                            raw_config = fh.read()
                    except OSError:
                        pass
                    config = raw_config.lower()
                    try:
                        hooks = [n for n in os.listdir(os.path.join(candidate, "hooks"))
                                 if not n.endswith(".sample")]
                    except OSError:
                        hooks = []
                    # a hook is a program git starts that the tracer may not see
                    info["hooks"] = bool(hooks) or "hookspath" in config
                    info["config_text"] = raw_config
                    info["plain"] = not (
                        os.path.exists(os.path.join(candidate, "commondir"))
                        or os.path.exists(os.path.join(candidate, "objects", "info", "alternates"))
                        or "worktree" in config or "include" in config or "hookspath" in config
                        or hooks)
                return info
            parent = os.path.dirname(current)
            if parent == current:
                return None
            current = parent

    def hook(event, args):
        if busy[0]:
            return
        busy[0] = True
        try:
            if event == "open":
                path = norm(args[0])
                if path is None:
                    return
                mode, flags = args[1], args[2]
                if isinstance(mode, str):
                    reading = "r" in mode or ("a" in mode and "+" in mode)
                    writing = any(c in mode for c in "wax+")
                else:
                    flags = flags if isinstance(flags, int) else 0
                    access = flags & (os.O_RDONLY | os.O_WRONLY | os.O_RDWR)
                    reading = access in (os.O_RDONLY, os.O_RDWR) and not flags & os.O_TRUNC
                    writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT))
                if reading:
                    note_read(path)
                if writing:
                    record["writes"].add(path)
            elif event in ("os.putenv", "os.unsetenv"):
                # changes the environment children inherit without going through os.environ
                record["untraceable"].add(event)
            elif event in ("os.symlink", "_winapi.CreateJunction"):
                # a link from the run's scratch into the repository would make repository
                # files look like scratch: such a run is not reused
                record["untraceable"].add(event)
            elif event in ("os.rename", "shutil.copyfile", "shutil.move", "os.link"):
                # the source is read (Windows copies without an `open` event), the
                # destination is the run's own file
                source, target = norm(args[0]), norm(args[1])
                if source is not None:
                    note_read(source)
                if target is not None:
                    record["writes"].add(target)
            elif event == "shutil.copytree":
                source, target = norm(args[0]), norm(args[1])
                if source is not None:
                    record["trees"].add(source)
                if target is not None:
                    record["made"].add(target)
            elif event == "os.mkdir":
                path = norm(args[0])
                if path is not None:
                    record["made"].add(path)
            elif event == "os.truncate":
                path = norm(args[0])
                if path is not None:
                    note_read(path)
                    record["writes"].add(path)
            elif event in ("os.listdir", "os.scandir"):
                target = args[0] if args and args[0] is not None else "."
                path = norm(target)
                if path is not None:
                    note_list(path)
                else:
                    record["untraceable"].add(event + " on a file descriptor")
            elif event == "subprocess.Popen":
                executable, argv, cwd, env = args
                if isinstance(argv, (str, bytes)):
                    argv = [os.fsdecode(argv)]
                git_env = None
                where = os.path.abspath(os.fsdecode(cwd)) if cwd else os.getcwd()
                tokens = [os.fsdecode(a) if isinstance(a, bytes) else str(a) for a in (argv or [])]
                if len(tokens) == 1 and (" " in tokens[0] or '"' in tokens[0]):
                    # Windows reports the command line as one string
                    import shlex
                    try:
                        tokens = [t.strip('"') for t in shlex.split(tokens[0], posix=False)]
                    except ValueError:
                        record["untraceable"].add("unparsable command line")
                index = 1
                while index < len(tokens) and tokens[index].startswith("-"):
                    if tokens[index] == "-C" and index + 1 < len(tokens):
                        where = os.path.join(where, tokens[index + 1])
                        index += 2
                    else:
                        index += 1
                if env is not None:
                    source = dict(env)
                else:
                    # what the child inherits; read raw so the tracer's own read is not
                    # mistaken for the validator copying the whole environment
                    source = {os.environ.decodekey(k): os.environ.decodevalue(v)
                              for k, v in os.environ._data.items()}
                git_env = {str(k): str(v) for k, v in source.items()
                           if str(k).upper().startswith("GIT_")
                           or str(k).upper() in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME",
                                                 "HOMEDRIVE", "HOMEPATH", "APPDATA")} or None
                record["spawns"].append({
                    "exe": os.fsdecode(executable) if executable else None,
                    "argv": [os.fsdecode(a) if isinstance(a, bytes) else str(a) for a in (argv or [])],
                    "cwd": os.path.abspath(os.fsdecode(cwd)) if cwd else os.getcwd(),
                    "where": os.path.realpath(where),
                    "gitdir": git_marker(os.path.realpath(where)),
                    "env": git_env, "env_explicit": env is not None})
            elif event == "_winapi.CreateProcess":
                # every Popen on Windows also lands here; the runner compares the counts
                record["createprocess"] = record.get("createprocess", 0) + 1
            elif event in ("os.system", "os.fork", "os.forkpty", "os.exec", "os.posix_spawn",
                           "os.spawn", "os.startfile", "pty.spawn"):
                record["untraceable"].add(event)
            elif event.startswith(("socket.connect", "socket.getaddrinfo", "sqlite3.connect",
                                   "urllib.Request", "ctypes.dlopen", "mmap.__new__",
                                   "winreg.")):
                record["untraceable"].add(event)
        except Exception:  # noqa: BLE001 -- a tracer bug must not change the validator
            record["untraceable"].add("tracer error on " + event)
        finally:
            busy[0] = False

    sys.addaudithook(hook)

    def watch(path):
        if busy[0] or isinstance(path, int):
            return
        busy[0] = True
        try:
            target = norm(path)
            if target is not None:
                note_stat(target)
        except Exception:  # noqa: BLE001
            record["untraceable"].add("tracer error on stat")
        finally:
            busy[0] = False

    def stat(path, *a, **k):
        watch(path)
        return real_stat(path, *a, **k)
    os.stat = stat

    def lstat(path, *a, **k):
        watch(path)
        return real_lstat(path, *a, **k)
    os.lstat = lstat
    real_access = os.access

    def access(path, *a, **k):
        watch(path)
        return real_access(path, *a, **k)
    os.access = access
    for module in (ntpath, posixpath, genericpath):
        for name in ("exists", "lexists", "isfile", "isdir", "islink", "getsize", "getmtime",
                     "getctime", "getatime", "isjunction", "isdevdrive", "samefile"):
            real = getattr(module, name, None)
            if real is None:
                continue

            def wrap(real):
                def checked(path, *a, **k):
                    watch(path)
                    for other in a:
                        if isinstance(other, (str, bytes, os.PathLike)):
                            watch(other)
                    return real(path, *a, **k)
                return checked
            setattr(module, name, wrap(real))

    class Entry:
        """A DirEntry whose stat() is recorded (DirEntry.stat raises no audit event)."""

        def __init__(self, entry):
            self._entry = entry

        def __getattr__(self, name):
            return getattr(self._entry, name)

        def __fspath__(self):
            return self._entry.__fspath__()

        def __repr__(self):
            return repr(self._entry)

        def stat(self, *a, **k):
            watch(self._entry.path)
            return self._entry.stat(*a, **k)

    class Scan:
        def __init__(self, it):
            self._it = it

        def __iter__(self):
            return self

        def __next__(self):
            return Entry(next(self._it))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._it.__exit__(*exc)

        def close(self):
            self._it.close()

    def scandir(path=".", *a, **k):
        return Scan(real_scandir(path, *a, **k))
    os.scandir = scandir

    environ_class = type(os.environ)
    real_getitem = environ_class.__getitem__
    watched = [os.environ] + ([os.environb] if hasattr(os, "environb") else [])

    def key_text(key):
        return os.fsdecode(key) if isinstance(key, bytes) else str(key)

    def value_text(value):
        return os.fsdecode(value) if isinstance(value, bytes) else value

    def getitem(self, key):
        try:
            value = real_getitem(self, key)
        except KeyError:
            if any(self is w for w in watched):
                record["env"].setdefault(key_text(key), None)
            raise
        if any(self is w for w in watched):
            record["env"].setdefault(key_text(key), value_text(value))
        return value
    environ_class.__getitem__ = getitem

    def contains(self, key):
        try:
            getitem(self, key)
            return True
        except KeyError:
            return False
    environ_class.__contains__ = contains

    real_iter = environ_class.__iter__

    def iterate(self):
        if any(self is w for w in watched):
            # copying the environment (dict(os.environ), for a child process) reads all
            # of it: the runner then compares the whole environment, not single keys
            record["env_all"] = True
        return real_iter(self)
    environ_class.__iter__ = iterate

    with open(os.path.join(trace_dir, f"{os.getpid()}.start"), "w", encoding="utf-8"):
        pass

    def dump():
        busy[0] = True
        out = dict(record)
        for key in ("writes", "made", "trees", "untraceable"):
            out[key] = sorted(out[key])
        try:
            with open(os.path.join(trace_dir, f"{os.getpid()}.json"), "w", encoding="utf-8") as fh:
                json.dump(out, fh)
        except OSError:
            pass
    atexit.register(dump)


if _DIR:
    _install(_DIR)
_load_next_sitecustomize()
