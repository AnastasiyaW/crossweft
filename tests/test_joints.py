"""Joint-kind plugins (crossweft/joints.py): a repository's own kinds of joint,
checked like crossweft's built-in ones. Every test plants one way a plugin or
its model can go wrong and fails if crossweft lets it pass silently.

Run: python -m unittest discover -s tests -v   (stdlib only)
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_engine_review import SHARED, Fixture, blocks, link, run  # noqa: E402

from crossweft import engine  # noqa: E402

CONFIG = {"joints": {"dir": "tools/joints"}}

# A small, honest plugin: every "port" entry names a file and the port that
# file must contain; render/impact/generated are exercised too.
PORTS = '''
import json
SECTION = "ports"
PAGE = "ports.md"
TITLE = {"en": "Ports", "ru": "Порты"}

def validate(entries):
    return [f"port {e!r} needs id, file, port" for e in entries
            if not (isinstance(e, dict) and {"id", "file", "port"} <= set(e))]

def check(root, entries, model):
    problems = []
    for e in entries:
        text = (root / e["file"]).read_text(encoding="utf-8") if (root / e["file"]).is_file() else ""
        if str(e["port"]) not in text:
            problems.append((f"port:{e['id']}", f"{e['file']} does not use port {e['port']}", False))
    return {"problems": problems, "items": len(entries), "files": {e["file"] for e in entries},
            "info": f"{len(entries)} ports"}

def render_markdown(entries):
    return "# Ports\\n\\n" + "\\n".join(f"- {e['id']}: {e['port']}" for e in entries)

def generated(root, entries):
    return {"server/ports.json": json.dumps({e["id"]: e["port"] for e in entries}) + "\\n"}

def impact(entries, changed):
    return [{"ref": f"ports:{e['id']}", "message": f"port {e['port']}", "other": [e["file"]]}
            for e in entries if e["file"] not in changed]

def self_test():
    return 0
'''

FILES = {"web/api.ts": "export const PORT = 8080;\n",
         "server/main.go": "package main\nconst Port = 8080\n"}
ENTRY = [{"id": "api", "file": "server/main.go", "port": 8080},
         {"id": "web", "file": "web/api.ts", "port": 8080}]


def shared_link() -> dict:
    return link(contract=SHARED)


class JointPlugins(Fixture):
    def setup_repo(self, plugin: str | None = PORTS, entries=ENTRY, pinned=("ports",),
                   config: dict | None = None, name: str = "joints_ports.py") -> None:
        model = {"blocks": blocks(), "links": [shared_link()]}
        if entries is not None:
            model["ports"] = entries
        meta = {"joint_kinds": list(pinned)} if pinned is not None else None
        self.make(dict(FILES), model, config=CONFIG if config is None else config, meta=meta)
        if plugin is not None:
            self.write(f"tools/joints/{name}", plugin)

    def test_a_working_plugin_passes_and_counts_its_items(self) -> None:
        self.setup_repo()
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn("ports: 2 ports", output)
        self.assertIn("joint kinds pinned: ports", output)

    def test_a_plugin_problem_fails_with_the_kind_in_its_key(self) -> None:
        self.setup_repo()
        self.write("server/main.go", "package main\nconst Port = 9090\n")
        code, output = self.check()
        self.assertEqual(code, 1, output)
        self.assertIn("ports:port:api", output)
        self.assertIn("[FAIL] joints: 1", output)

    def test_a_pinned_kind_whose_entries_were_deleted_fails(self) -> None:
        self.setup_repo(entries=None)
        code, output = self.check()
        self.assertEqual(code, 1, output)
        self.assertIn("joints:empty:ports", output)

    def test_a_pinned_kind_whose_plugin_was_deleted_fails(self) -> None:
        self.setup_repo(plugin=None, entries=None)
        self.write("tools/joints/.keep", "")
        code, output = self.check()
        self.assertEqual(code, 1, output)
        self.assertIn("joints:unknown:ports", output)

    def test_data_of_an_unpinned_kind_fails(self) -> None:
        for pinned in (None, ()):
            with self.subTest(pinned=pinned):
                self.setup_repo(pinned=pinned)
                self.assertIn("joints:unpinned:ports", self.keys())

    def test_a_crashing_check_is_a_fatal_problem_not_a_traceback(self) -> None:
        self.setup_repo(PORTS.replace("    problems = []", "    raise RuntimeError('boom')"))
        code, output = self.check()
        self.assertEqual(code, 1, output)
        self.assertIn("joints:broken:ports", output)
        self.assertIn("boom", output)

    def test_a_plugin_that_checked_nothing_is_not_green(self) -> None:
        self.setup_repo(PORTS.replace('"items": len(entries)', '"items": 0'))
        self.assertIn("joints:broken:ports", self.keys())

    def test_a_malformed_problem_row_is_not_dropped(self) -> None:
        self.setup_repo(PORTS.replace("problems = []", "problems = [('only-a-key',)]"))
        self.assertIn("joints:broken:ports", self.keys())

    def test_section_must_match_the_file_name(self) -> None:
        self.setup_repo(PORTS.replace('SECTION = "ports"', 'SECTION = "port"'))
        code, output = self.check()
        self.assertEqual(code, 2, output)
        self.assertIn("SECTION is 'port', expected 'ports'", output)

    def test_a_plugin_may_not_take_a_crossweft_model_key(self) -> None:
        self.setup_repo(PORTS.replace('SECTION = "ports"', 'SECTION = "sets"'),
                        entries=None, pinned=None, name="joints_sets.py")
        code, output = self.check()
        self.assertEqual(code, 2, output)
        self.assertIn("is a crossweft model key", output)

    def test_an_import_error_fails_loudly(self) -> None:
        self.setup_repo("import no_such_module_anywhere\n" + PORTS)
        code, output = self.check()
        self.assertEqual(code, 2, output)
        self.assertIn("failed to import", output)

    def test_a_section_without_joints_dir_names_the_fix(self) -> None:
        self.setup_repo(config={})
        code, output = self.check()
        self.assertEqual(code, 2, output)
        self.assertIn("unknown top-level key(s): ports", output)
        self.assertIn("joints.dir", output)

    def test_render_writes_the_page_and_generated_files_and_check_sees_drift(self) -> None:
        self.setup_repo(config={**CONFIG, "check_rendered_docs": True})
        code, output = run(["--root", str(self.root), "render"])
        self.assertEqual(code, 0, output)
        page = (self.root / "seams" / "ports.md").read_text(encoding="utf-8")
        self.assertTrue(engine.is_generated("ports.md", page), page)
        self.assertIn("- api: 8080", page)
        self.assertIn("[Ports](ports.md)",
                      (self.root / "seams" / "README.md").read_text(encoding="utf-8"))
        self.assertEqual(json.loads((self.root / "server/ports.json").read_text("utf-8")),
                         {"api": 8080, "web": 8080})
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.write("server/ports.json", "{}\n")
        code, output = self.check()
        self.assertEqual(code, 1, output)
        self.assertIn("server/ports.json", output)
        code, output = run(["--root", str(self.root), "render"])   # a second render rewrites
        self.assertEqual(code, 0, output)

    def test_a_plugin_may_not_generate_into_the_model(self) -> None:
        self.setup_repo(PORTS.replace('"server/ports.json"', '"seams/model/x.json"'))
        code, output = run(["--root", str(self.root), "render"])
        self.assertEqual(code, 2, output)
        self.assertIn("every file in model_dir is read as the model", output)

    def test_impact_lists_the_other_side(self) -> None:
        self.setup_repo()
        code, output = run(["--root", str(self.root), "impact", str(self.root / "web/api.ts")])
        self.assertEqual(code, 0, output)
        self.assertIn("ports: joints:ports  changed: web/api.ts", output)   # from the entries
        self.assertIn("ports:api", output)                                  # from impact()
        self.assertIn("other side: server/main.go", output)
        self.assertNotIn("Not on the map", output)

    def test_load_model_takes_the_repo_root(self) -> None:
        self.setup_repo()
        by_root = engine.load_model(self.root)
        by_cfg = engine.load_model(engine.load_config(self.root))
        self.assertEqual([p.name for p in by_root.files], [p.name for p in by_cfg.files])

    def test_impact_names_a_kind_it_cannot_place(self) -> None:
        self.setup_repo(PORTS.replace("def impact(", "def _unused_impact("),
                        entries=[{"id": "api", "file": "main", "port": 8080}])
        report = engine.build_impact(engine.load_model(engine.load_config(self.root)),
                                     ["server/main.go"])
        self.assertEqual([row["message"] for row in report["joints"]],
                         ["no file references or IMPACT_GLOBS -- impact cannot tell which "
                          "changes feed this kind"])

    # ---------------------------------------------------------- review fixes
    def _exit_plugin(self, site: str) -> str:
        """PORTS whose `site` hook ends the process like sys.exit(0)."""
        bodies = {
            "import": "import sys\nsys.exit(0)\n" + PORTS,
            "validate": PORTS.replace("def validate(entries):", "def validate(entries):\n    raise SystemExit(0)"),
            "check": PORTS.replace("    problems = []", "    raise SystemExit(0)"),
            "render_markdown": PORTS.replace("def render_markdown(entries):",
                                             "def render_markdown(entries):\n    raise SystemExit(0)"),
            "generated": PORTS.replace("def generated(root, entries):",
                                       "def generated(root, entries):\n    raise SystemExit(0)"),
            "impact": PORTS.replace("def impact(entries, changed):",
                                    "def impact(entries, changed):\n    raise SystemExit(0)"),
        }
        return bodies[site]

    def test_sys_exit_in_a_plugin_never_ends_the_run_quietly(self) -> None:
        for site in ("import", "validate", "check"):
            with self.subTest(site=site):
                self.setup_repo(self._exit_plugin(site))
                code, output = self.check()
                self.assertNotEqual(code, 0, output)
                self.assertIn("SystemExit", output)
        for site in ("render_markdown", "generated"):
            with self.subTest(site=site):
                self.setup_repo(self._exit_plugin(site))
                code, output = run(["--root", str(self.root), "render"])
                self.assertEqual(code, 2, output)
                self.assertIn("ports", output)
                self.assertIn("SystemExit", output)

    def test_sys_exit_in_impact_is_a_broken_row(self) -> None:
        self.setup_repo(self._exit_plugin("impact"))
        report = engine.build_impact(engine.load_model(engine.load_config(self.root)),
                                     ["web/api.ts"])
        self.assertIn("joints:broken:ports", [row["ref"] for row in report["joints"]])

    def test_generated_paths_must_be_normalised_and_off_crossweft_files(self) -> None:
        bad = ["./seams/model/x.json", "SEAMS/model/x.json", "a/./b.json", "a//b.json", "a/b/",
               "crossweft.json", "CrossWeft.json", "tools/joints/joints_ports.py",
               "tools/joints/other.py", "TOOLS/Joints/x.py", "seams/ports.md", "seams/x.json",
               "seams/pairs.lock.json", "a\\b.json", "/abs.json", "c:x.json", "a/../b.json"]
        for rel in bad:
            with self.subTest(rel=rel):
                self.setup_repo(PORTS.replace('"server/ports.json"', repr(rel)))
                code, output = run(["--root", str(self.root), "render"])
                self.assertEqual(code, 2, output)
                self.assertIn("generated failed", output)

    def test_a_planted_generated_path_cannot_replace_the_config(self) -> None:
        self.setup_repo(PORTS.replace('"server/ports.json"', '"./crossweft.json"'),
                        config={**CONFIG, "check_rendered_docs": True})
        before = (self.root / "crossweft.json").read_text(encoding="utf-8")
        run(["--root", str(self.root), "render"])
        self.assertEqual((self.root / "crossweft.json").read_text(encoding="utf-8"), before)

    def test_a_plugin_does_not_shadow_host_modules(self) -> None:
        # a module the host has NOT imported yet, so the test can really fail
        name = next((n for n in ("wave", "colorsys", "sched", "cmd", "fileinput", "mailbox")
                     if n not in sys.modules), None)
        self.assertIsNotNone(name, "every candidate host module is already imported")
        self.addCleanup(sys.modules.pop, name, None)
        self.setup_repo("import " + name + "\n" + PORTS)
        self.write(f"tools/joints/{name}.py", "PLANTED = True\n")
        before = list(sys.path)
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertEqual(sys.path, before)
        self.assertNotIn(name, sys.modules)          # the planted copy did not stay behind
        import importlib
        real = importlib.import_module(name)
        self.assertFalse(hasattr(real, "PLANTED"), real.__file__)

    def test_a_plugin_that_fails_to_import_leaves_no_modules_behind(self) -> None:
        name = next((n for n in ("sched", "cmd", "fileinput", "colorsys", "mailbox", "wave")
                     if n not in sys.modules), None)
        self.assertIsNotNone(name)
        self.addCleanup(sys.modules.pop, name, None)
        self.setup_repo("import " + name + "\nraise RuntimeError('late')\n" + PORTS)
        self.write(f"tools/joints/{name}.py", "PLANTED = True\n")
        code, output = self.check()
        self.assertEqual(code, 2, output)
        self.assertNotIn(name, sys.modules)

    def test_self_test_runs_each_plugins_self_test(self) -> None:
        import contextlib
        import io
        from unittest import mock
        for label, body, want in (
                ("passes", PORTS, 0),
                ("fails", PORTS.replace("    return 0\n", "    return 3\n"), 1),
                ("crashes", PORTS.replace("def self_test():\n    return 0",
                                          "def self_test():\n    raise SystemExit(0)"), 1)):
            with self.subTest(label):
                self.setup_repo(body)
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    code = engine.run_joint_self_tests(self.root)
                self.assertEqual(code, want, buffer.getvalue())
                self.assertIn("ports", buffer.getvalue())
        # the real command wires it in: a failing plugin fails `crossweft self-test`
        self.setup_repo(PORTS.replace("    return 0\n", "    return 3\n"))
        from crossweft import cli
        with mock.patch.object(engine, "self_test", return_value=0), \
                mock.patch.object(cli.runner, "_run_self_test", return_value=0):
            code, output = run(["--root", str(self.root), "self-test"])
        self.assertEqual(code, 1, output)
        self.assertIn("joint plugin ports", output)

    def test_an_edit_with_the_same_size_and_mtime_is_seen(self) -> None:
        # Python's bytecode cache is keyed by mtime (whole seconds) and size: an
        # edit inside the same second to the same length must still run
        import contextlib
        import io
        import os
        self.setup_repo(PORTS)
        path = self.root / "tools/joints/joints_ports.py"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(engine.run_joint_self_tests(self.root), 0)
        stamp = path.stat()
        edited = PORTS.replace("    return 0\n", "    return 3\n")
        self.assertEqual(len(edited), len(PORTS))
        path.write_bytes(edited.encode("utf-8"))
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = engine.run_joint_self_tests(self.root)
        self.assertEqual(code, 1, buffer.getvalue())
        self.assertFalse((path.parent / "__pycache__").exists(),
                         "plugins are compiled from source; no bytecode in the user's repo")

    def test_self_test_says_when_no_plugins_are_configured(self) -> None:
        import contextlib
        import io
        from unittest import mock
        for label, config, plugin in (("no joints.dir", {}, None),
                                      ("empty joints.dir", CONFIG, None)):
            with self.subTest(label):
                self.setup_repo(plugin=plugin, entries=None, pinned=None, config=config)
                self.write("tools/joints/.keep", "")
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    code = engine.run_joint_self_tests(self.root)
                self.assertEqual(code, 0)
                self.assertIn("joint plugins: none configured", buffer.getvalue())
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), mock.patch.object(
                engine, "find_repo_root", side_effect=engine.ModelError("no config")):
            code = engine.run_joint_self_tests(None)
        self.assertEqual(code, 0)
        self.assertIn("joint plugins: none configured", buffer.getvalue())

    def test_a_check_result_of_the_wrong_shape_is_broken_not_a_traceback(self) -> None:
        variants = {
            "problems=None": PORTS.replace("    problems = []", "    problems = None"),
            "problems is a str": PORTS.replace("    problems = []", "    problems = 'abc'"),
            "problems is an int": PORTS.replace("    problems = []", "    problems = 5"),
            "files=None": PORTS.replace('"files": {e["file"] for e in entries}', '"files": None'),
            "files is an int": PORTS.replace('"files": {e["file"] for e in entries}', '"files": 5'),
            "files hold ints": PORTS.replace('"files": {e["file"] for e in entries}', '"files": [1]'),
            "items=None": PORTS.replace('"items": len(entries)', '"items": None'),
            "items is a str": PORTS.replace('"items": len(entries)', '"items": "2"'),
        }
        for label, body in variants.items():
            with self.subTest(label):
                self.setup_repo(body)
                code, output = self.check()
                self.assertEqual(code, 1, output)
                self.assertIn("joints:broken:ports", output)

    def test_an_impact_row_with_a_bad_other_is_broken(self) -> None:
        for bad in ("5", "'abc'", "[1]", "{'a': 1}"):
            with self.subTest(other=bad):
                self.setup_repo(PORTS.replace('"other": [e["file"]]', f'"other": {bad}'))
                report = engine.build_impact(engine.load_model(engine.load_config(self.root)),
                                             ["web/api.ts"])
                self.assertIn("joints:broken:ports", [row["ref"] for row in report["joints"]])

    def test_a_crashing_render_is_an_error_naming_the_plugin(self) -> None:
        body = PORTS.replace("def render_markdown(entries):",
                             "def render_markdown(entries):\n    raise RuntimeError('page boom')")
        self.setup_repo(body, config={**CONFIG, "check_rendered_docs": True})
        code, output = run(["--root", str(self.root), "render"])
        self.assertEqual(code, 2, output)
        self.assertIn("ports", output)
        self.assertIn("page boom", output)
        code, output = self.check()                      # check_rendered_docs compares pages
        self.assertEqual(code, 2, output)
        self.assertIn("ports", output)
        self.assertIn("page boom", output)

    def test_a_crashing_generated_is_an_error_during_check_too(self) -> None:
        body = PORTS.replace("def generated(root, entries):",
                             "def generated(root, entries):\n    raise RuntimeError('gen boom')")
        self.setup_repo(body, config={**CONFIG, "check_rendered_docs": True})
        code, output = self.check()
        self.assertEqual(code, 2, output)
        self.assertIn("gen boom", output)

    def test_a_plugins_own_problem_with_zero_items_is_reported_as_itself(self) -> None:
        self.setup_repo(PORTS.replace('"items": len(entries)', '"items": 0')
                        .replace("    problems = []", "    problems = [('schema', 'planted schema error', True)]"))
        keys = self.keys()
        self.assertIn("ports:schema", keys)
        self.assertNotIn("joints:broken:ports", keys)


class UncategorisedProblems(Fixture):
    def test_a_problem_outside_the_listed_categories_is_printed(self) -> None:
        result = engine.CheckResult(new=[engine.Problem("x:1", "brand-new", "it broke", False,
                                                        None, None)])
        cfg = engine.Config(root=self.root)
        from test_engine_review import io, contextlib  # noqa: E402
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            engine.print_result(cfg, result)
        self.assertIn("[FAIL] brand-new: it broke", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
