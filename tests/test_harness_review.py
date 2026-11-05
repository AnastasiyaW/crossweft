"""Regressions from the independent review of the hooks, the repository harness,
the validator runner and the CLI (2026-09-30). Each test fails on the code the
review looked at and names the finding it pins down.

Run: python -m unittest discover -s tests -v   (stdlib only)
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "examples" / "polyglot-shop"
sys.path.insert(0, str(REPO))

from crossweft import cli, harness, runner  # noqa: E402

def _crossweft_importable_without_pythonpath() -> bool:
    """True when this interpreter imports crossweft from site-packages (an installed
    copy), so a test cannot produce 'crossweft is not importable from the project'."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    probe = subprocess.run([sys.executable, "-c", "import crossweft"], cwd=tempfile.gettempdir(),
                           env=env, capture_output=True)
    return probe.returncode == 0


CYRILLIC = "Проект"          # a non-ASCII directory name
GREETING = "Привет"


def run(argv: list[str], stdin: str | None = None) -> tuple[int, str, str]:
    """cli.main in-process: (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    old_stdin = sys.stdin
    try:
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
    finally:
        sys.stdin = old_stdin
    return code, out.getvalue(), err.getvalue()


def subprocess_env(**extra: str) -> dict:
    env = dict(os.environ, PYTHONPATH=str(REPO), PYTHONDONTWRITEBYTECODE="1")
    env.update(extra)
    return env


def git_bash() -> str | None:
    """bash that runs our launcher the way Claude Code does (never WSL's)."""
    bash = shutil.which("bash")
    if bash and os.name == "nt" and "system32" in bash.lower():
        return None
    return bash


# the shell Claude Code runs hook commands in: Git Bash on Windows, sh elsewhere
SHELL = git_bash() if os.name == "nt" else shutil.which("sh")
# a loaded test machine can start a shell slower than the 10 s production limit
PROBE_TIMEOUT = {"CROSSWEFT_LAUNCHER_PROBE_TIMEOUT": "25"}


class DemoCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="crossweft-review-")
        self.root = Path(self.tmp.name) / "shop"
        shutil.copytree(DEMO, self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def drift(self) -> None:
        api = self.root / "web/src/api.ts"
        api.write_text(api.read_text(encoding="utf-8").replace("2026-09-01", "2026-10-01"),
                       encoding="utf-8")

    def hook(self, event: str, *flags: str, agent: str = "claude-code", **payload):
        payload = {"cwd": str(self.root), "session_id": "review-" + self._testMethodName,
                   **payload}
        code, out, err = run(["hook", agent, event, *flags], json.dumps(payload))
        return code, (json.loads(out) if out.strip() else None), err

    def check(self) -> tuple[int, str]:
        code, out, _ = run(["--root", str(self.root), "check"])
        return code, out

    def agents(self, *extra: str) -> tuple[int, str]:
        code, out, _ = run(["--root", str(self.root), "agents", *extra])
        return code, out

    @property
    def settings_path(self) -> Path:
        return self.root / ".claude/settings.json"

    def settings(self) -> dict:
        return json.loads(self.settings_path.read_text(encoding="utf-8"))

    def write_settings(self, data: dict) -> None:
        self.settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def fake_launcher_dir(self, name: str = "crossweft") -> Path:
        """A directory holding a script bash runs as `crossweft` (also Git Bash)
        that answers --version like crossweft does."""
        directory = Path(self.tmp.name) / "fakebin"
        directory.mkdir(exist_ok=True)
        script = directory / name
        script.write_text('#!/bin/sh\necho "crossweft 9.9.9"\n', encoding="ascii")
        script.chmod(0o755)
        return directory

    def session_env(self, **extra: str) -> dict:
        """Claude Code started in the demo root, hooks run by SHELL, with a
        `crossweft` on PATH."""
        return {"PATH": str(self.fake_launcher_dir()), "CLAUDE_PROJECT_DIR": str(self.root),
                "CROSSWEFT_SHELL": SHELL or "", **PROBE_TIMEOUT, **extra}


# 1 -- the plugin must not go quiet for project hooks that cannot run ----------
class PluginDefersOnlyToHooksThatRun(DemoCase):
    def test_plugin_blocks_when_the_project_launcher_is_not_on_path(self) -> None:
        self.drift()
        empty = Path(self.tmp.name) / "emptybin"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            code, out, _ = self.hook("stop", "--from-plugin")
        self.assertEqual(code, 0)
        self.assertIsNotNone(out, "plugin-only user: the project hook exits 127, the plugin "
                                  "must not stay quiet")
        self.assertEqual(out["decision"], "block")

    @unittest.skipUnless(SHELL, "no shell to run the hook command in")
    def test_plugin_is_quiet_when_the_project_launcher_resolves(self) -> None:
        self.drift()
        with mock.patch.dict(os.environ, self.session_env()):
            code, out, _ = self.hook("stop", "--from-plugin")
            report = "" if (code, out) == (0, None) else self.launcher_probe_report()
        self.assertEqual((code, out), (0, None), report)

    def test_probe_timeout_override_is_bounded_and_strict(self) -> None:
        cases = {None: 10, "": 10, "3": 3.0, "25": 25.0, "600": harness.LAUNCHER_PROBE_TIMEOUT_MAX}
        for raw, want in cases.items():
            env = {} if raw is None else {"CROSSWEFT_LAUNCHER_PROBE_TIMEOUT": raw}
            with mock.patch.dict(os.environ, env, clear=False):
                if raw is None:
                    os.environ.pop("CROSSWEFT_LAUNCHER_PROBE_TIMEOUT", None)
                self.assertEqual(harness.launcher_probe_timeout(), want, raw)
        for bad in ("ten", "0", "-1"):
            with mock.patch.dict(os.environ, {"CROSSWEFT_LAUNCHER_PROBE_TIMEOUT": bad}):
                with self.assertRaises(harness.HarnessError):
                    harness.launcher_probe_timeout()

    def launcher_probe_report(self) -> str:
        """The launcher probe once more, with what it answered: this test failed
        about 1 run in 4 on 2026-10-10 and then not in 12 class runs on
        2026-11-05, so a failure has to bring its own evidence."""
        started = time.monotonic()
        try:
            probe = subprocess.run([SHELL, "-c", "crossweft --version"], cwd=self.root,
                                   stdin=subprocess.DEVNULL, capture_output=True,
                                   timeout=harness.launcher_probe_timeout())
            answer = f"rc={probe.returncode} stdout={probe.stdout!r} stderr={probe.stderr[-300:]!r}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            answer = repr(exc)
        return (f"launcher probe rerun ({SHELL}, PATH={os.environ.get('PATH')}): {answer} "
                f"in {time.monotonic() - started:.1f}s")

    def test_a_cmd_shim_is_not_what_bash_runs(self) -> None:
        # Git Bash does not run `crossweft.cmd` for `crossweft` (shutil.which
        # on Windows would, through PATHEXT)
        self.drift()
        shim = Path(self.tmp.name) / "cmdbin"
        shim.mkdir()
        (shim / "crossweft.cmd").write_text("@exit /b 0\r\n", encoding="ascii")
        with mock.patch.dict(os.environ, self.session_env(PATH=str(shim))):
            _, out, _ = self.hook("stop", "--from-plugin")
        self.assertIsNotNone(out, "the project hook exits 127 under bash: the plugin must speak")
        self.assertEqual(out["decision"], "block")

    @unittest.skipUnless(SHELL, "no shell to run the hook command in")
    @unittest.skipIf(_crossweft_importable_without_pythonpath(),
                     "crossweft is installed for this interpreter, so 'not importable from the "
                     "project' cannot be produced here")
    def test_python_module_launcher_needs_an_importable_module(self) -> None:
        launcher = f'"{Path(sys.executable).as_posix()}" -m crossweft'
        self.assertEqual(self.agents("--command", launcher)[0], 0)
        self.drift()
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env.update(CLAUDE_PROJECT_DIR=str(self.root), CROSSWEFT_SHELL=SHELL or "", **PROBE_TIMEOUT)
        with mock.patch.dict(os.environ, env, clear=True):
            _, out, _ = self.hook("stop", "--from-plugin")
        self.assertIsNotNone(out, "crossweft is not importable from the project: its hook fails")
        self.assertEqual(out["decision"], "block")
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(REPO), "CROSSWEFT_SHELL": SHELL or "",
                                          "CLAUDE_PROJECT_DIR": str(self.root), **PROBE_TIMEOUT}):
            self.assertEqual(self.hook("stop", "--from-plugin")[:2], (0, None))

    @unittest.skipIf(_crossweft_importable_without_pythonpath(),
                     "crossweft is installed for this interpreter: a regular package in "
                     "site-packages wins over the project's namespace directory")
    def test_a_namespace_package_is_not_a_runnable_module(self) -> None:
        launcher = f'"{Path(sys.executable).as_posix()}" -m crossweft'
        self.assertEqual(self.agents("--command", launcher)[0], 0)
        (self.root / "crossweft").mkdir()   # no __init__.py: imports, but -m fails
        (self.root / "crossweft" / "notes.txt").write_text("x\n", encoding="utf-8")
        self.drift()
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env.update(CLAUDE_PROJECT_DIR=str(self.root), CROSSWEFT_SHELL=SHELL or "")
        with mock.patch.dict(os.environ, env, clear=True):
            _, out, _ = self.hook("stop", "--from-plugin")
        self.assertIsNotNone(out, "`python -m crossweft` fails on a namespace package")
        self.assertEqual(out["decision"], "block")

    def test_plugin_speaks_when_the_session_started_elsewhere(self) -> None:
        # Claude Code reads .claude/settings.json from the directory the session
        # started in (https://code.claude.com/docs/en/settings)
        self.drift()
        elsewhere = Path(self.tmp.name) / "monorepo-top"
        elsewhere.mkdir()
        with mock.patch.dict(os.environ, self.session_env(CLAUDE_PROJECT_DIR=str(elsewhere))):
            _, out, _ = self.hook("stop", "--from-plugin")
        self.assertIsNotNone(out, "the demo's settings are not loaded: the plugin must speak")
        self.assertEqual(out["decision"], "block")

    def test_plugin_speaks_when_the_launcher_is_not_crossweft(self) -> None:
        # declared and consistent, so `check` accepts it -- but the launcher
        # does not answer --version like crossweft: the project hook checks nothing
        self.drift()
        python = Path(sys.executable).as_posix()
        for launcher in ("echo", f'"{python}" -c pass'):
            self.assertEqual(self.agents("--command", launcher)[0], 0)
            with mock.patch.dict(os.environ, self.session_env()):
                _, out, _ = self.hook("stop", "--from-plugin")
            self.assertIsNotNone(out, f"`{launcher} hook claude-code stop` checks nothing: "
                                      "the plugin must speak")
            self.assertEqual(out["decision"], "block")

    def test_plugin_speaks_without_a_shell_to_prove_the_launcher(self) -> None:
        self.drift()
        with mock.patch.dict(os.environ, self.session_env(CROSSWEFT_SHELL="")),                 mock.patch.object(harness.shutil, "which", return_value=None):
            _, out, _ = self.hook("stop", "--from-plugin")
        self.assertEqual(out["decision"], "block")

    def test_plugin_speaks_when_the_project_disables_all_hooks(self) -> None:
        self.drift()
        data = self.settings()
        data["disableAllHooks"] = True
        self.write_settings(data)
        with mock.patch.dict(os.environ, self.session_env()):
            _, out, _ = self.hook("stop", "--from-plugin")
        self.assertEqual(out["decision"], "block")


# 2 -- hook stdin is UTF-8 whatever the console code page ----------------------
class HookStdinIsUtf8(DemoCase):
    def test_non_ascii_repository_path_on_a_legacy_code_page(self) -> None:
        root = Path(self.tmp.name) / CYRILLIC / "shop"
        shutil.copytree(self.root, root)
        api = root / "web/src/api.ts"
        api.write_text(api.read_text(encoding="utf-8").replace("2026-09-01", "2026-10-01"),
                       encoding="utf-8")
        payload = json.dumps({"cwd": str(root), "session_id": "review-cp",
                              "stop_hook_active": False}, ensure_ascii=False).encode("utf-8")
        for code_page in ("cp1252", "cp1251"):
            proc = subprocess.run([sys.executable, "-m", "crossweft", "hook", "claude-code",
                                   "stop"], input=payload, capture_output=True, cwd=root,
                                  env=subprocess_env(PYTHONIOENCODING=code_page))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(b'"decision": "block"', proc.stdout, (code_page, proc.stderr))

    def test_a_missing_working_directory_is_reported_not_skipped(self) -> None:
        missing = Path(self.tmp.name) / "gone"
        code, out, err = run(["hook", "claude-code", "stop"], json.dumps({"cwd": str(missing)}))
        self.assertEqual(code, 1, (out, err))
        self.assertIn("does not exist", err)
        self.assertIn("NOT checked", err)


# 3 -- the plugin launcher runs its own package, whatever PYTHONPATH holds -----
@unittest.skipUnless(git_bash(), "needs bash (Git Bash on Windows)")
class PluginLauncher(unittest.TestCase):
    def test_launcher_with_pythonpath_set_and_unset_and_spaces_in_the_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="crossweft launcher ") as tmp:
            plugin = Path(tmp) / "plugin dir with spaces"
            shutil.copytree(REPO / "bin", plugin / "bin")
            shutil.copytree(REPO / "crossweft", plugin / "crossweft",
                            ignore=shutil.ignore_patterns("__pycache__"))
            # a foreign `crossweft` on PYTHONPATH and in the working directory
            # (a vendored older copy) must not be what the plugin runs
            decoy = Path(tmp) / "decoy"
            (decoy / "crossweft").mkdir(parents=True)
            (decoy / "crossweft" / "__init__.py").write_text("raise SystemExit('DECOY')\n",
                                                             encoding="utf-8")
            base = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            for pythonpath in (None, str(decoy), f"{decoy}{os.pathsep}{tmp}"):
                env = dict(base, PYTHONDONTWRITEBYTECODE="1")
                if pythonpath:
                    env["PYTHONPATH"] = pythonpath
                proc = subprocess.run([git_bash(), (plugin / "bin" / "crossweft").as_posix(),
                                       "--version"], env=env, cwd=decoy, capture_output=True,
                                      text=True)
                self.assertEqual(proc.returncode, 0, (pythonpath, proc.stdout, proc.stderr))
                self.assertIn("crossweft 0.", proc.stdout, (pythonpath, proc.stderr))


# 4 -- a validator timeout is enforced even when it leaves a child behind ------
GOOD_VALIDATOR = ("import sys\n"
                  "if '--self-test' in sys.argv:\n"
                  "    print('SELF-TEST: checks=1'); sys.exit(0)\n"
                  "print('SCANNED: files=1 items=1')\n")
BROKEN_GATE = "import sys\nprint('needs a tool CI does not have'); sys.exit(2)\n"


class ValidatorExclude(unittest.TestCase):
    def suite(self, exclude, timeouts=None):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "v"
            d.mkdir()
            (d / "validate_good.py").write_text(GOOD_VALIDATOR, encoding="utf-8")
            (d / "validate_gate.py").write_text(BROKEN_GATE, encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = runner.run_validators(Path(tmp), d, 60, timeouts, exclude)
            return code, out.getvalue()

    def test_an_excluded_file_is_not_run_and_is_reported_with_its_reason(self) -> None:
        code, output = self.suite(None)
        self.assertEqual(code, 1, output)          # negative control: the gate fails when run
        code, output = self.suite({"validate_gate.py": "commit gate, runs on --staged"})
        self.assertEqual(code, 0, output)
        self.assertIn("excluded (not run): validate_gate.py -- commit gate, runs on --staged",
                      output)
        self.assertIn("running 1 validator(s)", output)

    def test_a_stale_exclude_entry_is_an_error(self) -> None:
        code, output = self.suite({"validate_gone.py": "removed long ago"})
        self.assertEqual(code, 1, output)
        self.assertIn("validators.exclude names file(s) that do not exist: validate_gone.py",
                      output)

    def test_a_timeout_for_an_excluded_file_is_an_error(self) -> None:
        code, output = self.suite({"validate_gate.py": "commit gate"},
                                  {"validate_gate.py": 300})
        self.assertEqual(code, 1, output)
        self.assertIn("validators.timeouts names validator(s) that do not exist or are "
                      "excluded: validate_gate.py", output)

    def test_excluding_every_file_ran_nothing(self) -> None:
        code, output = self.suite({"validate_gate.py": "gate", "validate_good.py": "also"})
        self.assertEqual(code, 1, output)
        self.assertIn("nothing ran", output)

    def test_an_exclude_without_a_reason_is_refused_by_the_config(self) -> None:
        from crossweft import engine
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for bad in ({"validate_gate.py": ""}, {"validate_gate.py": "  "},
                        ["validate_gate.py"]):
                (root / "crossweft.json").write_text(
                    json.dumps({"validators": {"dir": "v", "exclude": bad}}), encoding="utf-8")
                with self.assertRaises(engine.ModelError, msg=repr(bad)):
                    engine.load_config(root)


class ValidatorTimeout(unittest.TestCase):
    def test_timeout_kills_the_validator_and_its_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            beat = Path(tmp) / "grandchild-heartbeat"
            child = ("import pathlib, time\n"
                     f"p = pathlib.Path({str(beat)!r})\n"
                     "for _ in range(120):\n"
                     "    with p.open('a') as f: f.write('x')\n"
                     "    time.sleep(0.25)\n")
            script = Path(tmp) / "validate_hang.py"
            script.write_text(
                "import subprocess, sys, time\n"
                "if '--self-test' in sys.argv:\n"
                "    print('SELF-TEST: checks=1'); sys.exit(0)\n"
                f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
                "time.sleep(60)\n"
                "print('SCANNED: files=1 items=1')\n", encoding="utf-8")
            old = runner.TIMEOUT_DEFAULT[0]
            runner.TIMEOUT_DEFAULT[0] = 2
            try:
                started = time.monotonic()
                result = runner.evaluate_validator(script, Path(tmp))
                elapsed = time.monotonic() - started
            finally:
                runner.TIMEOUT_DEFAULT[0] = old
            self.assertEqual(result.verdict, "FAIL")
            self.assertIn("timed out", result.detail)
            self.assertLess(elapsed, 15, "the timeout waited for a child that held the output")
            size = beat.stat().st_size if beat.exists() else 0
            time.sleep(1.5)
            after = beat.stat().st_size if beat.exists() else 0
            self.assertEqual(size, after, "the validator's child survived the timeout")

    def test_an_interrupt_kills_the_validator_and_its_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            beat = Path(tmp) / "grandchild-heartbeat"
            child = ("import pathlib, time\n"
                     f"p = pathlib.Path({str(beat)!r})\n"
                     "for _ in range(120):\n"
                     "    with p.open('a') as f: f.write('x')\n"
                     "    time.sleep(0.25)\n")
            script = Path(tmp) / "validate_slow.py"
            script.write_text("import subprocess, sys, time\n"
                              f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
                              "time.sleep(60)\n", encoding="utf-8")
            real_wait = subprocess.Popen.wait
            interrupted = []

            def wait(proc, timeout=None):   # Ctrl-C while the runner waits
                if not interrupted:
                    interrupted.append(True)
                    deadline = time.monotonic() + 10
                    while not beat.exists() and time.monotonic() < deadline:
                        time.sleep(0.1)
                    raise KeyboardInterrupt
                return real_wait(proc, timeout)

            with mock.patch.object(subprocess.Popen, "wait", wait), \
                    self.assertRaises(KeyboardInterrupt):
                runner._run([str(script)], Path(tmp), 60)
            self.assertTrue(beat.exists(), "test setup: the grandchild never started")
            time.sleep(0.5)
            size = beat.stat().st_size
            time.sleep(1.5)
            self.assertEqual(size, beat.stat().st_size, "the validator's child survived Ctrl-C")


# 5, 6 -- `agents` is idempotent and leaves what is not its own alone ----------
class AgentsPreservesAndIsIdempotent(DemoCase):
    def test_custom_command_without_the_word_crossweft_is_idempotent(self) -> None:
        self.settings_path.unlink()
        self.assertEqual(self.agents("--command", "./tools/sg")[0], 0)
        code, out = self.agents("--command", "./tools/sg")
        self.assertEqual(code, 0, out)
        self.assertIn("already in place", out)
        self.assertEqual(len(self.settings()["hooks"]["Stop"]), 1)
        self.assertEqual(self.check()[0], 0)

    def commands(self) -> dict:
        return {name: [h["command"] for g in groups for h in g["hooks"]]
                for name, groups in self.settings()["hooks"].items()}

    def test_changing_the_command_updates_the_hooks_in_place(self) -> None:
        self.assertEqual(self.agents("--command", "python -m crossweft")[0], 0)
        self.assertEqual(self.commands()["Stop"], ["python -m crossweft hook claude-code stop"])

    def test_agents_and_init_record_the_command(self) -> None:
        self.assertEqual(self.agents("--command", "python -m crossweft")[0], 0)
        config = json.loads((self.root / "crossweft.json").read_text(encoding="utf-8"))
        self.assertEqual(config["agent"]["command"], "python -m crossweft")
        self.assertEqual(self.check()[0], 0)
        fresh = Path(self.tmp.name) / "fresh"
        fresh.mkdir()
        code, out, _ = run(["--root", str(fresh), "init", "--command", "./tools/sg"])
        self.assertEqual(code, 0, out)
        config = json.loads((fresh / "crossweft.json").read_text(encoding="utf-8"))
        self.assertEqual(config["agent"], {"harness": True, "command": "./tools/sg"})
        self.assertEqual(harness.problems(fresh, "seams/model"), [])

    def test_a_plain_rerun_keeps_the_launcher_the_hooks_use(self) -> None:
        # the check's own advice is "run `crossweft agents`": that must not swap
        # a vendored `python -m crossweft` for a `crossweft` that may not exist
        self.assertEqual(self.agents("--command", "python -m crossweft")[0], 0)
        code, out = self.agents()
        self.assertEqual(code, 0, out)
        self.assertIn("already in place", out)
        self.assertEqual(self.commands()["Stop"], ["python -m crossweft hook claude-code stop"])

    def test_a_partial_set_is_migrated_not_left_behind(self) -> None:
        data = self.settings()
        del data["hooks"]["Stop"]
        self.write_settings(data)
        self.assertEqual(self.agents("--command", "./tools/sg")[0], 0)
        self.assertEqual(self.commands(), {
            "SessionStart": ["./tools/sg hook claude-code session-start"],
            "PostToolUse": ["./tools/sg hook claude-code post-edit"],
            "Stop": ["./tools/sg hook claude-code stop"]})

    def test_another_tools_hooks_of_the_same_shape_are_left_alone(self) -> None:
        theirs = {"SessionStart": [{"hooks": [{"type": "command",
                                               "command": "othertool hook claude-code session-start"}]}],
                  "Stop": [{"hooks": [{"type": "command",
                                       "command": "othertool hook claude-code stop"}]}]}
        for extra in ((), ("--command", "crossweft")):
            self.write_settings({"hooks": json.loads(json.dumps(theirs))})
            self.assertEqual(self.agents(*extra)[0], 0)
            self.assertEqual(self.commands(), {
                "SessionStart": ["othertool hook claude-code session-start",
                                 "crossweft hook claude-code session-start"],
                "Stop": ["othertool hook claude-code stop", "crossweft hook claude-code stop"],
                "PostToolUse": ["crossweft hook claude-code post-edit"]}, extra)

    def test_a_rerun_keeps_the_complete_set_and_repairs_a_stray(self) -> None:
        launcher = '"$CLAUDE_PROJECT_DIR"/bin/crossweft'
        self.assertEqual(self.agents("--command", launcher)[0], 0)
        data = self.settings()
        data["hooks"]["Stop"].append({"hooks": [{"type": "command",
                                                 "command": "echo crossweft hook claude-code stop"}]})
        self.write_settings(data)
        self.assertEqual(self.agents()[0], 0)
        self.assertEqual(self.commands()["Stop"], [f"{launcher} hook claude-code stop"] * 2)
        self.assertEqual(self.commands()["SessionStart"],
                         [f"{launcher} hook claude-code session-start"])

    def test_settings_keep_their_indentation(self) -> None:
        data = self.settings()
        del data["hooks"]["Stop"]
        self.settings_path.write_text(json.dumps(data, indent=4) + "\n", encoding="utf-8")
        self.assertEqual(self.agents()[0], 0)
        text = self.settings_path.read_text(encoding="utf-8")
        self.assertIn('\n    "hooks": {', text)
        self.assertIn('"Stop"', text)

    def test_a_read_only_file_is_an_error_without_leftovers(self) -> None:
        path = self.root / "AGENTS.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn("Never weaken a guard", text, "test setup: the block must change")
        path.write_text(text.replace("Never weaken a guard", "Weaken a guard"), encoding="utf-8")
        before = path.read_bytes()
        path.chmod(0o444)
        try:
            code, out = self.agents()
        finally:
            path.chmod(0o644)
        self.assertEqual(code, 2, out)
        self.assertIn("AGENTS.md", out)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([p.name for p in self.root.rglob("*crossweft-tmp")], [])

    def test_a_symlinked_claude_md_stays_a_link(self) -> None:
        link = self.root / "CLAUDE.md"
        try:
            link.symlink_to("AGENTS.md")
        except (OSError, NotImplementedError):
            self.skipTest("cannot create a symlink here")
        self.assertEqual(self.check()[0], 0, "CLAUDE.md is AGENTS.md: nothing to import")
        self.assertEqual(self.agents()[0], 0)
        self.assertTrue(link.is_symlink())
        self.assertNotIn("@AGENTS.md", (self.root / "AGENTS.md").read_text(encoding="utf-8"))

    def test_a_shared_group_that_already_matches_edits_is_left_alone(self) -> None:
        data = self.settings()
        ours = data["hooks"]["PostToolUse"][0]["hooks"][0]
        shared = {"matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash",
                  "hooks": [{"type": "command", "command": "my-audit-log"}, ours]}
        data["hooks"]["PostToolUse"] = [shared]
        self.write_settings(data)
        code, out = self.agents()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.settings()["hooks"]["PostToolUse"], [shared])

    def test_a_shared_group_that_misses_edits_gets_a_sibling_not_a_new_matcher(self) -> None:
        data = self.settings()
        ours = data["hooks"]["PostToolUse"][0]["hooks"][0]
        shared = {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-audit-log"},
                                               ours]}
        data["hooks"]["PostToolUse"] = [shared]
        self.write_settings(data)
        self.assertEqual(self.agents()[0], 0)
        groups = self.settings()["hooks"]["PostToolUse"]
        self.assertEqual(groups[0], shared, "the user's group lost its matcher")
        self.assertEqual(len(groups), 2)
        self.assertEqual(self.check()[0], 0)

    def test_non_ascii_user_settings_stay_literal(self) -> None:
        data = self.settings()
        data["env"] = {"GREETING": GREETING}
        del data["hooks"]["Stop"]
        self.settings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
        self.assertEqual(self.agents()[0], 0)
        text = self.settings_path.read_text(encoding="utf-8")
        self.assertIn(GREETING, text)
        self.assertNotIn("\\u041f", text)

    def test_newline_style_of_existing_files_is_kept(self) -> None:
        agents_md = self.root / "AGENTS.md"
        agents_md.write_bytes(b"# House rules\n\nUse tabs.\n")
        claude_md = self.root / "CLAUDE.md"
        claude_md.write_bytes(b"# Notes\r\n\r\nWindows file.\r\n")
        self.settings_path.unlink()
        self.assertEqual(self.agents()[0], 0)
        lf = agents_md.read_bytes()
        self.assertNotIn(b"\r\n", lf, "an LF file became CRLF")
        self.assertIn(b"crossweft-agents:begin", lf)
        crlf = claude_md.read_bytes()
        self.assertIn(b"@AGENTS.md", crlf)
        self.assertEqual(crlf.count(b"\n"), crlf.count(b"\r\n"), "a CRLF file got bare LFs")
        self.assertNotIn(b"\r\n", self.settings_path.read_bytes(), "a new file is not LF")


# 7 -- the harness guard catches what disables it, and only that ---------------
class HarnessGuard(DemoCase):
    def assert_fails(self, key: str) -> None:
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn(key, out)

    def test_disable_all_hooks_fails_the_check(self) -> None:
        data = self.settings()
        data["disableAllHooks"] = True
        self.write_settings(data)
        self.assert_fails("agents:hooks")

    def test_a_stop_hook_that_does_not_start_crossweft_fails_the_check(self) -> None:
        data = self.settings()
        data["hooks"]["Stop"][0]["hooks"][0]["command"] = "echo crossweft hook claude-code stop"
        self.write_settings(data)
        self.assert_fails("agents:hooks")

    def test_a_second_stale_block_fails_and_agents_removes_it(self) -> None:
        path = self.root / "AGENTS.md"
        path.write_text(path.read_text(encoding="utf-8")
                        + "\n<!-- crossweft-agents:begin x -->\nstale rules\n"
                          "<!-- crossweft-agents:end -->\n", encoding="utf-8")
        self.assert_fails("agents:agents-md")
        self.assertEqual(self.agents()[0], 0)
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("crossweft-agents:begin"), 1)
        self.assertNotIn("stale rules", text)
        self.assertEqual(self.check()[0], 0)

    def test_hooks_that_do_not_start_the_declared_command_fail(self) -> None:
        # all three rewritten alike, so no "launchers differ" signal is left
        for launcher in ("echo crossweft", "echo", "true"):
            data = self.settings()
            for groups in data["hooks"].values():
                for group in groups:
                    for hook in group["hooks"]:
                        hook["command"] = hook["command"].replace("crossweft hook",
                                                                  launcher + " hook", 1)
            self.write_settings(data)
            code, out = self.check()
            self.assertEqual(code, 1, (launcher, out))
            self.assertIn("agents:hooks", out, launcher)
            self.assertEqual(self.agents()[0], 0)
            self.assertEqual(self.check()[0], 0, launcher)

    def test_the_declared_command_is_the_one_checked(self) -> None:
        config_path = self.root / "crossweft.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["agent"]["command"] = "python -m crossweft"
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("python -m crossweft hook claude-code stop", out)
        self.assertEqual(self.agents()[0], 0)
        self.assertEqual(self.check()[0], 0)
        self.assertIn('"python -m crossweft hook claude-code stop"',
                      self.settings_path.read_text(encoding="utf-8"))

    def test_a_matcher_that_matches_every_tool_passes(self) -> None:
        for matcher in ("*", "", None):
            data = self.settings()
            group = data["hooks"]["PostToolUse"][0]
            group.pop("matcher", None)
            if matcher is not None:
                group["matcher"] = matcher
            self.write_settings(data)
            code, out = self.check()
            self.assertEqual(code, 0, (matcher, out))

    def test_a_regex_matcher_is_read_as_a_regex(self) -> None:
        data = self.settings()
        data["hooks"]["PostToolUse"][0]["matcher"] = "^(Edit|Write)$"
        self.write_settings(data)
        self.assertEqual(self.check()[0], 0)
        data["hooks"]["PostToolUse"][0]["matcher"] = "Bash|Read"
        self.write_settings(data)
        self.assert_fails("agents:hooks:post-edit")

    def test_claude_local_md_hides_agents_md_unless_something_imports_it(self) -> None:
        # https://code.claude.com/docs/en/memory#agents-md: a CLAUDE.local.md
        # counts, so Claude Code then reads the CLAUDE.md files only
        local = self.root / "CLAUDE.local.md"
        local.write_text("# my notes\n", encoding="utf-8")
        self.assert_fails("agents:claude-md")
        self.assertEqual(self.agents()[0], 0)
        self.assertIn("@AGENTS.md", local.read_text(encoding="utf-8"))
        self.assertEqual(self.check()[0], 0)

    def test_a_marker_quoted_in_prose_is_not_a_block(self) -> None:
        path = self.root / "AGENTS.md"
        prose = ("Our notes mention the `<!-- crossweft-agents:begin` marker in passing;\n"
                 "this sentence is ours and must survive.\n\n")
        path.write_text(prose + path.read_text(encoding="utf-8"), encoding="utf-8")
        self.assertEqual(self.check()[0], 0)
        self.assertEqual(self.agents()[0], 0)
        self.assertTrue(path.read_text(encoding="utf-8").startswith(prose))

    def test_one_import_among_the_claude_files_is_enough(self) -> None:
        (self.root / "CLAUDE.local.md").write_text("# my notes\n", encoding="utf-8")
        (self.root / "CLAUDE.md").write_text("@AGENTS.md\n", encoding="utf-8")
        self.assertEqual(self.check()[0], 0)


# 8 -- "scanned nothing" is not green at Stop -----------------------------------
class StopOnAnEmptyModel(unittest.TestCase):
    def test_stop_does_not_pass_an_empty_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "fresh"
            root.mkdir()
            self.assertEqual(run(["--root", str(root), "init"])[0], 0)
            self.assertEqual(run(["--root", str(root), "check"])[0], 2)
            code, out, _ = run(["hook", "claude-code", "stop"],
                               json.dumps({"cwd": str(root), "session_id": "review-empty"}))
            self.assertEqual(code, 0)
            self.assertTrue(out.strip(), "check exits 2 (scanned nothing); the Stop hook "
                                         "passed silently")
            reply = json.loads(out)
            # the user sees it; the agent is not sent back on every turn of an
            # adoption that has not been mapped yet
            self.assertNotIn("decision", reply)
            self.assertIn("scanned nothing", reply["systemMessage"])


# 9 -- a non-UTF-8 AGENTS.md is a clear error, never a traceback ---------------
class NonUtf8AgentsMd(DemoCase):
    def test_agents_refuses_and_writes_nothing_check_reports(self) -> None:
        path = self.root / "AGENTS.md"
        path.write_bytes(("Заметки\n".encode("cp1251"))
                         + path.read_bytes())
        data = self.settings()
        del data["hooks"]["Stop"]
        self.write_settings(data)
        before = self.settings_path.read_bytes()
        code, out = self.agents()
        self.assertEqual(code, 2, out)
        self.assertIn("UTF-8", out)
        self.assertEqual(self.settings_path.read_bytes(), before)
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("agents:agents-md", out)
        self.assertNotIn("crashed", out)


# 10 -- the runner's table survives a cp1252 console ----------------------------
class RunnerOutputIsAsciiSafe(unittest.TestCase):
    def test_non_ascii_validator_output_on_cp1252(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "v").mkdir()
            (root / "crossweft.json").write_text('{"validators": {"dir": "v"}}',
                                                 encoding="utf-8")
            (root / "v" / "validate_u.py").write_text(
                "import sys\n"
                "if '--self-test' in sys.argv:\n"
                "    print('SELF-TEST: checks=1'); sys.exit(0)\n"
                "print('\\u2717 \\u0434\\u0440\\u0435\\u0439\\u0444 found')\n"
                "print('SCANNED: files=1 items=1'); sys.exit(1)\n", encoding="utf-8")
            proc = subprocess.run([sys.executable, "-m", "crossweft", "validators"], cwd=root,
                                  capture_output=True,
                                  env=subprocess_env(PYTHONIOENCODING="cp1252"))
            stdout = proc.stdout.decode("cp1252")
            stderr = proc.stderr.decode("cp1252", "replace")
            self.assertEqual(proc.returncode, 1, stderr)
            self.assertNotIn("Traceback", stderr)
            self.assertIn("validate_u.py", stdout)
            self.assertIn("found", stdout)


# 11 -- discover refuses a limit that shows nothing -----------------------------
class DiscoverLimit(DemoCase):
    def test_limit_below_one_is_a_usage_error(self) -> None:
        for limit in ("0", "-3"):
            with self.assertRaises(SystemExit) as caught, \
                    contextlib.redirect_stderr(io.StringIO()):
                cli.main(["--root", str(self.root), "discover", "--limit", limit])
            self.assertEqual(caught.exception.code, 2, limit)


# 12 -- an explicit agent.harness = false is respected --------------------------
class HarnessOptOut(DemoCase):
    def test_agents_keeps_an_explicit_false(self) -> None:
        config_path = self.root / "crossweft.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["agent"]["harness"] = False
        config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        code, out = self.agents()
        self.assertEqual(code, 0, out)
        self.assertFalse(json.loads(config_path.read_text(encoding="utf-8"))["agent"]["harness"])
        self.assertIn("agent.harness is false", out)


# 13 -- version skew between the harness and crossweft never blocks at Stop ----
class HookIsForwardCompatible(DemoCase):
    def test_unknown_agent_event_or_flag_is_not_exit_2(self) -> None:
        for argv in (["hook", "claude-code", "pre-compact"], ["hook", "new-agent", "stop"],
                     ["hook"]):
            code, out, err = run(argv, json.dumps({"cwd": str(self.root)}))
            # 1, not 0: exit 0 hides stderr, so the skip would be invisible
            self.assertEqual((code, out), (1, ""), argv)
            self.assertIn("crossweft hook", err, argv)
        code, out, err = self.hook("stop", "--a-future-flag")
        self.assertEqual((code, out), (0, None), err)
        self.assertIn("--a-future-flag", err)

    def test_a_malformed_hook_flag_is_not_exit_2(self) -> None:
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(err):
            cli.main(["hook", "claude-code", "stop", "--from-plugin=yes"])
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("--from-plugin", err.getvalue())

    def test_a_top_level_usage_error_in_a_hook_call_is_not_exit_2(self) -> None:
        err = io.StringIO()
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(err):
            cli.main(["--root", "hook", "claude-code", "stop"])   # --root lost its value
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("skipped", err.getvalue())

    def test_unknown_arguments_elsewhere_are_still_usage_errors(self) -> None:
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
            cli.main(["--root", str(self.root), "check", "--bogus"])
        self.assertEqual(caught.exception.code, 2)


# 14 -- the GitHub Action does not paste inputs into its shell -----------------
class ActionInputs(unittest.TestCase):
    def test_no_expression_is_interpolated_into_a_run_script(self) -> None:
        text = (REPO / "action.yml").read_text(encoding="utf-8")
        runs = [line for line in text.splitlines() if line.strip().startswith("run:")]
        self.assertTrue(runs)
        for line in runs:
            self.assertNotIn("${{", line, line)


# 15 -- a failed write leaves the previous file intact --------------------------
class AtomicWrites(DemoCase):
    def test_interrupted_write_keeps_the_old_file_and_leaves_no_temp(self) -> None:
        data = self.settings()
        del data["hooks"]["Stop"]
        self.write_settings(data)
        before = self.settings_path.read_bytes()
        with mock.patch.object(harness.os, "replace", side_effect=OSError("disk full")):
            code, out = self.agents()
        self.assertEqual(code, 2, out)
        self.assertIn("disk full", out)
        self.assertEqual(self.settings_path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.settings_path.parent.iterdir()),
                         ["settings.json", "skills"])


if __name__ == "__main__":
    unittest.main()
