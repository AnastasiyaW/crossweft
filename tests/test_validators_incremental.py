"""`crossweft validators --incremental`: a passing phase is reused only while everything
it read is unchanged, and anything the tracer cannot see is never reused.

Each validator counts its own runs in a file outside the repository, so a test can tell
a reused phase from a run one. Run: python -m unittest tests.test_validators_incremental
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from crossweft import runner  # noqa: E402

HEAD = """import os, sys
from pathlib import Path
phase = "self" if "--self-test" in sys.argv else "normal"
with open(os.environ["CW_COUNTER"], "a") as fh:
    fh.write(Path(__file__).stem + ":" + phase + "\\n")
if phase == "self":
    print("SELF-TEST: checks=1")
    sys.exit(0)
"""


class Incremental(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="crossweft-vinc-")
        base = Path(self._tmp.name)
        self.root = base / "repo"
        self.vdir = self.root / "validators"
        self.vdir.mkdir(parents=True)
        self.counter = base / "runs.txt"
        self.counter.write_text("", encoding="utf-8")
        self._env = os.environ.get("CW_COUNTER")
        os.environ["CW_COUNTER"] = str(self.counter)
        self.git("init", "-q")

    def tearDown(self) -> None:
        if self._env is None:
            os.environ.pop("CW_COUNTER", None)
        else:
            os.environ["CW_COUNTER"] = self._env
        self._tmp.cleanup()

    def isolated_git_home(self) -> None:
        """A home without the machine's global git hooks, for the rest of the test, so a
        fixture-git rule is tested on its own (the runner and the validator both see it)."""
        home = Path(self._tmp.name) / "home"
        home.mkdir()
        (home / ".gitconfig").write_text("[user]\n\tname = t\n\temail = t@t\n", encoding="utf-8")
        for key, value in (("HOME", str(home)), ("USERPROFILE", str(home)),
                           ("XDG_CONFIG_HOME", str(home / ".config")), ("GIT_CONFIG_NOSYSTEM", "1")):
            saved = os.environ.get(key)
            self.addCleanup(lambda k=key, s=saved: os.environ.pop(k, None) if s is None
                            else os.environ.__setitem__(k, s))
            os.environ[key] = value

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def validator(self, name: str, body: str) -> None:
        (self.vdir / f"validate_{name}.py").write_text(HEAD + body, encoding="utf-8")

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def run_suite(self) -> tuple[int, list[str], str]:
        self.counter.write_text("", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = runner.run_validators(self.root, self.vdir, 60, incremental=True)
        runs = [line for line in self.counter.read_text(encoding="utf-8").splitlines() if line]
        return code, sorted(runs), out.getvalue()

    def test_a_read_file_decides(self) -> None:
        self.write("data.txt", "a\n")
        self.validator("reads", "open('data.txt').read()\nprint('SCANNED: files=1 items=1')\n")
        code, runs, out = self.run_suite()
        self.assertEqual((code, runs), (0, ["validate_reads:normal", "validate_reads:self"]), out)
        code, runs, out = self.run_suite()
        self.assertEqual((code, runs), (0, []), out)
        self.assertIn("(reused: self-test, normal)", out)
        self.write("data.txt", "b\n")
        code, runs, out = self.run_suite()
        self.assertEqual((code, runs), (0, ["validate_reads:normal"]), out)
        self.assertIn("ran again: validate_reads.py (normal: ", out)
        self.assertIn("data.txt changed", out)

    def test_a_listed_directory_decides(self) -> None:
        self.write("src/a.py", "")
        self.validator("lists", "print('SCANNED: files=%d items=1' % len(os.listdir('src')))\n")
        self.run_suite()
        self.assertEqual(self.run_suite()[1], [])
        self.write("src/b.py", "")
        self.assertEqual(self.run_suite()[1], ["validate_lists:normal"])

    def test_an_existence_check_decides(self) -> None:
        self.validator("exists", "print('SCANNED: files=1 items=%d' % (1 + os.path.exists('flag')))\n")
        self.run_suite()
        self.assertEqual(self.run_suite()[1], [])
        self.write("flag", "")
        self.assertEqual(self.run_suite()[1], ["validate_exists:normal"])

    def test_an_environment_variable_decides(self) -> None:
        os.environ["CW_MODE"] = "one"
        try:
            self.validator("env", "print(os.environ.get('CW_MODE'))\nprint('SCANNED: files=1 items=1')\n")
            self.run_suite()
            self.assertEqual(self.run_suite()[1], [])
            os.environ["CW_MODE"] = "two"
            self.assertEqual(self.run_suite()[1], ["validate_env:normal"])
        finally:
            os.environ.pop("CW_MODE", None)

    @unittest.skipUnless(hasattr(os, "posix_spawn"), "os.posix_spawn is POSIX-only")
    def test_a_child_subprocess_starts_with_posix_spawn_is_traced(self) -> None:
        # subprocess picks posix_spawn by itself on some Linux/Python versions (seen on
        # GitHub's ubuntu runner with 3.13); forced here so every POSIX run covers it
        self.write("data.txt", "a\n")
        self.validator("pspawn", "import subprocess\n"
                       "subprocess._USE_POSIX_SPAWN = True\n"
                       "subprocess.run([sys.executable, '-c', \"open('data.txt').read()\"],"
                       " check=True, close_fds=False)\n"
                       "print('SCANNED: files=1 items=1')\n")
        self.run_suite()
        _, runs, out = self.run_suite()
        self.assertEqual(runs, [], out)
        self.write("data.txt", "b\n")
        _, runs, out = self.run_suite()
        self.assertEqual(runs, ["validate_pspawn:normal"], out)

    @unittest.skipUnless(hasattr(os, "posix_spawn"), "os.posix_spawn is POSIX-only")
    def test_a_bare_posix_spawn_is_untraceable(self) -> None:
        self.validator("bare", "pid = os.posix_spawn(sys.executable, [sys.executable, '-c', 'pass'],"
                       " dict(os.environ))\n"
                       "os.waitpid(pid, 0)\n"
                       "print('SCANNED: files=1 items=1')\n")
        _, _, out = self.run_suite()
        line = [l for l in out.splitlines() if "not reusable next time: validate_bare.py" in l]
        self.assertTrue(line and "os.posix_spawn" in line[0], out)

    def test_a_python_child_is_traced(self) -> None:
        self.write("data.txt", "a\n")
        self.validator("child", "import subprocess\n"
                       "subprocess.run([sys.executable, '-c', \"open('data.txt').read()\"], check=True)\n"
                       "print('SCANNED: files=1 items=1')\n")
        self.run_suite()
        _, runs, out = self.run_suite()
        self.assertEqual(runs, [], out)
        self.write("data.txt", "b\n")
        _, runs, out = self.run_suite()
        self.assertEqual(runs, ["validate_child:normal"], out)

    def test_an_untraceable_child_is_never_reused(self) -> None:
        # -I ignores PYTHONPATH, so the child never reports what it read
        self.validator("blind", "import subprocess\n"
                       "subprocess.run([sys.executable, '-I', '-c', 'pass'], check=True)\n"
                       "print('SCANNED: files=1 items=1')\n")
        code, runs, out = self.run_suite()
        self.assertIn("not reusable next time: validate_blind.py", out)
        self.assertEqual(self.run_suite()[1], ["validate_blind:normal"])

    def test_a_program_that_is_not_read_only_git_is_never_reused(self) -> None:
        self.validator("tool", "import subprocess\n"
                       "subprocess.run(['git', '--version'], check=True, capture_output=True)\n"
                       "print('SCANNED: files=1 items=1')\n")
        self.run_suite()
        self.assertEqual(self.run_suite()[1], ["validate_tool:normal"])

    def test_read_only_git_output_decides(self) -> None:
        self.write("a.txt", "")
        self.git("add", "a.txt")
        self.validator("git", "import subprocess\n"
                       "files = subprocess.run(['git', 'ls-files'], capture_output=True, text=True,"
                       " check=True).stdout.split()\n"
                       "print('SCANNED: files=%d items=1' % len(files))\n")
        self.run_suite()
        self.assertEqual(self.run_suite()[1], [])
        self.write("b.txt", "")
        self.git("add", "b.txt")
        self.assertEqual(self.run_suite()[1], ["validate_git:normal"])

    def test_scratch_the_run_makes_is_not_an_input(self) -> None:
        # a self-test copies a fixture into its own temporary directory, reads the copy
        # and removes it: only the repository fixture is an input
        self.write("fixture.txt", "a\n")
        self.validator("scratch", "import shutil, tempfile\n"
                       "tmp = tempfile.mkdtemp()\n"
                       "shutil.copyfile('fixture.txt', os.path.join(tmp, 'f.txt'))\n"
                       "open(os.path.join(tmp, 'f.txt')).read()\n"
                       "shutil.rmtree(tmp)\n"
                       "print('SCANNED: files=1 items=1')\n")
        self.run_suite()
        code, runs, out = self.run_suite()
        self.assertEqual(runs, [], out)
        self.write("fixture.txt", "b\n")
        self.assertEqual(self.run_suite()[1], ["validate_scratch:normal"])

    def test_git_on_a_fixture_repository_is_reusable(self) -> None:
        self.isolated_git_home()
        self.validator("fixture_git", "import subprocess, tempfile\n"
                       "tmp = tempfile.mkdtemp()\n"
                       "subprocess.run(['git', 'init', '-q'], cwd=tmp, check=True)\n"
                       "subprocess.run(['git', 'status', '--short'], cwd=tmp, check=True,"
                       " capture_output=True)\n"
                       "print('SCANNED: files=1 items=1')\n")
        self.run_suite()
        code, runs, out = self.run_suite()
        self.assertEqual(runs, [], out)

    # --- the four false-green shapes found by the independent review (2026-10-23) ---

    def test_git_from_a_made_directory_into_the_repo_is_compared(self) -> None:
        self.write("data/d.txt", "a\n")
        self.git("add", "data/d.txt")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x")
        self.validator("fixgit", "import subprocess, tempfile\n"
                       "root = os.getcwd()\n"
                       "out = subprocess.run(['git', '-C', root, 'diff', '--', 'data/d.txt'],"
                       " cwd=tempfile.mkdtemp(), capture_output=True, text=True).stdout\n"
                       "print('SCANNED: files=1 items=1')\n"
                       "sys.exit(1 if out else 0)\n")
        self.run_suite()
        self.assertEqual(self.run_suite()[1], [])
        self.write("data/d.txt", "b\n")
        code, runs, out = self.run_suite()
        self.assertEqual((code, runs), (1, ["validate_fixgit:normal"]), out)

    def test_a_size_check_decides(self) -> None:
        self.write("data/b.txt", "small\n")
        self.validator("size", "print('SCANNED: files=1 items=1')\n"
                       "sys.exit(0 if os.path.getsize('data/b.txt') < 100 else 1)\n")
        self.run_suite()
        self.assertEqual(self.run_suite()[1], [])
        self.write("data/b.txt", "x" * 200)
        code, runs, out = self.run_suite()
        self.assertEqual((code, runs), (1, ["validate_size:normal"]), out)

    def test_a_file_read_then_rewritten_stays_an_input(self) -> None:
        self.write("data/c.txt", "good\n")
        self.validator("rw", "text = open('data/c.txt').read()\n"
                       "open('data/c.txt', 'w').write(text)\n"
                       "print('SCANNED: files=1 items=1')\n"
                       "sys.exit(0 if text == 'good\\n' else 1)\n")
        self.run_suite()
        self.run_suite()
        self.write("data/c.txt", "bad\n")
        code, runs, out = self.run_suite()
        self.assertEqual((code, runs), (1, ["validate_rw:normal"]), out)

    def test_a_size_read_through_scandir_decides(self) -> None:
        self.write("data/f/x.txt", "small\n")
        self.validator("direntry", "big = [e.name for e in os.scandir('data/f')"
                       " if e.stat().st_size > 100]\n"
                       "print('SCANNED: files=1 items=1')\n"
                       "sys.exit(1 if big else 0)\n")
        self.run_suite()
        self.assertEqual(self.run_suite()[1], [])
        self.write("data/f/x.txt", "x" * 200)
        code, runs, out = self.run_suite()
        self.assertEqual((code, runs), (1, ["validate_direntry:normal"]), out)

    # --- second review round (2026-10-23) ---

    def test_git_pointed_at_the_repo_by_environment_is_not_fixture_git(self) -> None:
        self.write("data/g.txt", "a\n")
        self.git("add", "data/g.txt")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x")
        self.validator("gitdir", "import subprocess, tempfile\n"
                       "root = os.getcwd()\n"
                       "env = dict(os.environ, GIT_DIR=os.path.join(root, '.git'), GIT_WORK_TREE=root)\n"
                       "out = subprocess.run(['git', 'diff', '--', 'data/g.txt'], cwd=tempfile.mkdtemp(),"
                       " env=env, capture_output=True, text=True).stdout\n"
                       "print('SCANNED: files=1 items=1')\n"
                       "sys.exit(1 if out else 0)\n")
        self.run_suite()
        self.write("data/g.txt", "b\n")
        code, runs, out = self.run_suite()
        self.assertEqual((code, runs), (1, ["validate_gitdir:normal"]), out)

    def test_a_link_from_scratch_is_never_reused(self) -> None:
        self.validator("link", "import tempfile\n"
                       "tmp = tempfile.mkdtemp()\n"
                       "try:\n"
                       "    os.symlink(os.getcwd(), os.path.join(tmp, 'j'), target_is_directory=True)\n"
                       "except OSError:\n"
                       "    import _winapi\n"
                       "    _winapi.CreateJunction(os.getcwd(), os.path.join(tmp, 'j'))\n"
                       "print('SCANNED: files=1 items=1')\n")
        code, runs, out = self.run_suite()
        self.assertIn("not reusable next time: validate_link.py", out)
        self.assertEqual(self.run_suite()[1], ["validate_link:normal"])

    def test_an_access_check_decides(self) -> None:
        self.validator("access", "print('SCANNED: files=1 items=1')\n"
                       "sys.exit(1 if os.access('data/forbidden.txt', os.F_OK) else 0)\n")
        self.run_suite()
        self.assertEqual(self.run_suite()[1], [])
        self.write("data/forbidden.txt", "")
        code, runs, out = self.run_suite()
        self.assertEqual((code, runs), (1, ["validate_access:normal"]), out)

    # --- third review round (2026-10-23) ---

    def _commit(self, rel: str, text: str) -> None:
        self.write(rel, text)
        self.git("add", rel)
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x")

    def test_git_dir_inherited_from_os_environ_is_not_fixture_git(self) -> None:
        self._commit("data/l.txt", "a\n")
        self.validator("inherit", "import subprocess, tempfile\n"
                       "root = os.getcwd()\n"
                       "os.environ['GIT_DIR'] = os.path.join(root, '.git')\n"
                       "os.environ['GIT_WORK_TREE'] = root\n"
                       "out = subprocess.run(['git', 'diff', '--', 'data/l.txt'], cwd=tempfile.mkdtemp(),"
                       " capture_output=True, text=True).stdout\n"
                       "print('SCANNED: files=1 items=1')\n"
                       "sys.exit(1 if out else 0)\n")
        self.run_suite()
        self.write("data/l.txt", "b\n")
        code, runs, out = self.run_suite()
        self.assertEqual((code, runs), (1, ["validate_inherit:normal"]), out)

    def test_putenv_is_never_reused(self) -> None:
        self.validator("putenv", "os.putenv('CW_X', '1')\nprint('SCANNED: files=1 items=1')\n")
        code, runs, out = self.run_suite()
        self.assertIn("not reusable next time: validate_putenv.py", out)
        self.assertEqual(self.run_suite()[1], ["validate_putenv:normal"])

    def test_a_git_file_pointing_at_the_repo_is_not_fixture_git(self) -> None:
        self._commit("data/m.txt", "good\n")
        self.validator("gitfile", "import subprocess, tempfile\n"
                       "root = os.getcwd()\n"
                       "tmp = tempfile.mkdtemp()\n"
                       "open(os.path.join(tmp, '.git'), 'w').write('gitdir: ' +"
                       " os.path.join(root, '.git').replace(os.sep, '/') + '\\n')\n"
                       "out = subprocess.run(['git', 'show', 'HEAD:data/m.txt'], cwd=tmp,"
                       " capture_output=True, text=True).stdout\n"
                       "print('SCANNED: files=1 items=1')\n"
                       "sys.exit(0 if out.strip() == 'good' else 1)\n")
        self.run_suite()
        self._commit("data/m.txt", "bad\n")
        code, runs, out = self.run_suite()
        self.assertEqual((code, runs), (1, ["validate_gitfile:normal"]), out)

    def test_git_with_another_home_is_not_fixture_git(self) -> None:
        # fourth review round: HOME/USERPROFILE moved into the repository makes git read
        # a repository file as its global configuration
        self.write("data/home/.gitconfig", "[x]\n\ty = good\n")
        self.validator("home", "import subprocess, tempfile\n"
                       "home = os.path.join(os.getcwd(), 'data', 'home')\n"
                       "env = dict(os.environ, HOME=home, USERPROFILE=home)\n"
                       "tmp = tempfile.mkdtemp()\n"
                       "subprocess.run(['git', 'init', '-q'], cwd=tmp, env=env, check=True)\n"
                       "out = subprocess.run(['git', 'config', '--get', 'x.y'], cwd=tmp, env=env,"
                       " capture_output=True, text=True).stdout\n"
                       "print('SCANNED: files=1 items=1')\n"
                       "sys.exit(0 if out.strip() == 'good' else 1)\n")
        self.run_suite()
        self.write("data/home/.gitconfig", "[x]\n\ty = bad\n")
        code, runs, out = self.run_suite()
        self.assertEqual((code, runs), (1, ["validate_home:normal"]), out)

    def test_a_hook_in_a_fixture_repository_is_not_fixture_git(self) -> None:
        # a hook is a program git starts; the tracer cannot see what a shell hook reads
        self.isolated_git_home()
        self.write("data/h.txt", "good\n")
        self.validator("hook", "import subprocess, tempfile\n"
                       "repo = os.path.join(os.getcwd(), 'data', 'h.txt').replace(os.sep, '/')\n"
                       "tmp = tempfile.mkdtemp()\n"
                       "run = lambda *a: subprocess.run(['git', '-c', 'user.name=t', '-c',"
                       " 'user.email=t@t', *a], cwd=tmp, capture_output=True)\n"
                       "run('init', '-q')\n"
                       "hook = os.path.join(tmp, '.git', 'hooks', 'pre-commit')\n"
                       "open(hook, 'w', newline='\\n').write('#!/bin/sh\\ngrep -q good \"' + repo + '\"\\n')\n"
                       "os.chmod(hook, 0o755)\n"
                       "open(os.path.join(tmp, 'f'), 'w').write('x')\n"
                       "run('add', 'f')\n"
                       "done = run('commit', '-q', '-m', 'x')\n"
                       "print('SCANNED: files=1 items=1')\n"
                       "sys.exit(done.returncode)\n")
        _, _, first = self.run_suite()
        kept = [l for l in first.splitlines() if "not reusable next time: validate_hook.py" in l]
        self.assertTrue(kept and "hook" in kept[0].split("validate_hook.py", 1)[1], first)
        self.write("data/h.txt", "bad\n")
        code, runs, out = self.run_suite()
        self.assertEqual((code, runs), (1, ["validate_hook:normal"]), out)

    def _filter_validator(self, name: str, set_filter: str) -> None:
        # a clean filter is a program git starts: it reads data/<name>.txt behind the tracer
        self.write(f"data/{name}.txt", "good\n")
        self.validator(name, "import subprocess, tempfile\n"
                       f"src = os.path.join(os.getcwd(), 'data', '{name}.txt').replace(os.sep, '/')\n"
                       "env = dict(os.environ, SRCFILE=src)\n"
                       "g = lambda *a: subprocess.run(['git', *a], cwd=tmp, env=env,"
                       " capture_output=True, text=True)\n"
                       "tmp = tempfile.mkdtemp()\n"
                       "g('init', '-q')\n" + set_filter +
                       "open(os.path.join(tmp, '.gitattributes'), 'w').write('f.txt filter=x\\n')\n"
                       "open(os.path.join(tmp, 'f.txt'), 'w').write('placeholder\\n')\n"
                       "g('add', 'f.txt')\n"
                       "ok = g('show', ':f.txt').stdout.strip() == 'good'\n"
                       "print('SCANNED: files=1 items=1')\n"
                       "sys.exit(0 if ok else 1)\n")

    def _assert_filter_rerun(self, name: str) -> None:
        _, _, first = self.run_suite()
        self.assertIn(f"not reusable next time: validate_{name}.py", first)
        self.assertIn("filter.x.clean", first)
        self.write(f"data/{name}.txt", "bad\n")
        code, runs, out = self.run_suite()
        self.assertEqual((code, runs), (1, [f"validate_{name}:normal"]), out)

    def test_a_filter_written_into_the_fixture_config_is_not_fixture_git(self) -> None:
        self.isolated_git_home()
        self._filter_validator("wf", "open(os.path.join(tmp, '.git', 'config'), 'a')"
                               ".write('[filter \"x\"]\\n\\tclean = cat \"' + src + '\"\\n')\n")
        self._assert_filter_rerun("wf")

    def test_a_filter_key_on_the_section_line_is_not_fixture_git(self) -> None:
        # git reads `[filter "x"] clean = ...` as filter.x.clean; `#]` keeps the line looking
        # like a bare section header to a hand-written parser
        self.isolated_git_home()
        self._filter_validator("sf", "open(os.path.join(tmp, '.git', 'config'), 'a')"
                               ".write('[filter \"x\"] clean = cat \"' + src + '\" #]\\n')\n")
        self._assert_filter_rerun("sf")

    def test_the_global_ignore_file_is_an_input_of_fixture_git(self) -> None:
        self.isolated_git_home()
        ignore = Path(os.environ["HOME"]) / ".config" / "git" / "ignore"
        ignore.parent.mkdir(parents=True)
        ignore.write_text("*.log\n", encoding="utf-8")
        self.validator("gi", "import subprocess, tempfile\n"
                       "tmp = tempfile.mkdtemp()\n"
                       "subprocess.run(['git', 'init', '-q'], cwd=tmp, check=True)\n"
                       "open(os.path.join(tmp, 'x.log'), 'w').write('x')\n"
                       "out = subprocess.run(['git', 'status', '--porcelain'], cwd=tmp,"
                       " capture_output=True, text=True).stdout\n"
                       "print('SCANNED: files=1 items=1')\n"
                       "sys.exit(1 if out.strip() else 0)\n")
        self.run_suite()
        code, runs, out = self.run_suite()
        self.assertEqual((code, runs), (0, []), out)
        ignore.write_text("", encoding="utf-8")
        code, runs, out = self.run_suite()
        self.assertEqual((code, runs), (1, ["validate_gi:normal"]), out)

    def test_a_filter_set_by_git_config_is_not_fixture_git(self) -> None:
        self.isolated_git_home()
        self._filter_validator("cf", "g('config', 'filter.x.clean', 'cat \"$SRCFILE\"')\n")
        self._assert_filter_rerun("cf")

    def test_a_global_hooks_path_makes_committing_fixture_git_not_reusable(self) -> None:
        self.isolated_git_home()
        hooks = Path(self._tmp.name) / "global-hooks"
        hooks.mkdir()
        with open(Path(os.environ["HOME"]) / ".gitconfig", "a", encoding="utf-8") as fh:
            fh.write("[core]\n\thooksPath = " + hooks.as_posix() + "\n")
        self.validator("ghook", "import subprocess, tempfile\n"
                       "tmp = tempfile.mkdtemp()\n"
                       "subprocess.run(['git', 'init', '-q'], cwd=tmp, check=True)\n"
                       "open(os.path.join(tmp, 'f'), 'w').write('x')\n"
                       "subprocess.run(['git', 'add', 'f'], cwd=tmp, check=True)\n"
                       "subprocess.run(['git', 'commit', '-q', '-m', 'x'], cwd=tmp, check=True)\n"
                       "print('SCANNED: files=1 items=1')\n")
        code, runs, out = self.run_suite()
        self.assertIn("not reusable next time: validate_ghook.py", out)
        self.assertIn("core.hookspath", out)

    def test_a_failure_is_never_kept(self) -> None:
        self.write("ok", "")
        self.validator("fails", "print('SCANNED: files=1 items=1')\n"
                       "sys.exit(0 if os.path.exists('ok') else 1)\n")
        self.run_suite()
        os.remove(self.root / "ok")
        code, runs, out = self.run_suite()
        self.assertEqual((code, runs), (1, ["validate_fails:normal"]), out)
        code, runs, out = self.run_suite()
        self.assertEqual((code, runs), (1, ["validate_fails:normal"]), out)

    def test_an_edited_validator_runs_again(self) -> None:
        self.validator("edit", "print('SCANNED: files=1 items=1')\n")
        self.run_suite()
        self.validator("edit", "print('SCANNED: files=1 items=2')\n")
        self.assertEqual(self.run_suite()[1], ["validate_edit:normal", "validate_edit:self"])

    def test_without_git_nothing_is_kept(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)  # keep ownership
        (self.root / ".git").rename(self.root / ".git-away")
        try:
            self.validator("nogit", "print('SCANNED: files=1 items=1')\n")
            code, runs, out = self.run_suite()
            self.assertIn("not a git work tree", out)
            self.assertEqual(len(self.run_suite()[1]), 2)
        finally:
            (self.root / ".git-away").rename(self.root / ".git")


if __name__ == "__main__":
    unittest.main()
