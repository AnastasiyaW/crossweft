"""Fallbacks (crossweft/fallbacks.py): the scanner finds every listed shape and
stays quiet on code that raises again; `check` ties the sites to the registry
and fails on unregistered, refused, changed and stale entries; `propose` and
`decide` move an entry through its states; with signers, only a signed
decision counts.

Run: python -m unittest discover -s tests -v   (stdlib only)
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from crossweft import cli, fallbacks  # noqa: E402


def run(argv: list) -> tuple:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.main(argv)
    return code, out.getvalue()


def kinds(path: str, text: str) -> list:
    result = fallbacks.scan(path, text)
    assert not isinstance(result, str), result
    return [(s.kind, s.symbol) for s in result]


class Scanner(unittest.TestCase):
    def test_python_shapes(self):
        text = ("import os, contextlib\n"
                "def load():\n"
                "    try:\n        a()\n    except ValueError:\n        pass\n"
                "    try:\n        a()\n    except KeyError as e:\n        raise RuntimeError() from e\n"
                "    with contextlib.suppress(OSError):\n        a()\n"
                "    os.environ.get('A', '1')\n    os.environ.get('B')\n    os.getenv('C', 'x')\n")
        self.assertEqual(kinds("a.py", text), [("swallow", "load"), ("suppress", "load"),
                                               ("env-default", "load"), ("env-default", "load")])

    def test_python_handler_that_exits_is_not_a_fallback(self):
        text = "﻿try:\n    a()\nexcept OSError as exc:\n    print(exc)\n    sys.exit(2)\n"
        self.assertEqual(kinds("a.py", text), [])

    def test_go_non_error_nil_check_is_not_an_error_check(self):
        text = "package x\nfunc F() {\n\tif body != nil {\n\t\tclose()\n\t}\n}\n"
        self.assertEqual(kinds("a.go", text), [])

    def test_go_control_flow_decides(self):
        handled = ("package x\nfunc H(w W) {\n\tif err := decode(); err != nil {\n"
                   "\t\twriteError(w, 400, err.Error())\n\t\treturn\n\t}\n}\n")
        self.assertEqual(kinds("a.go", handled), [])
        hides = ("package x\nfunc Send() error {\n\tif err := c.Quit(); err != nil {\n"
                 "\t\tslog.Warn(\"quit\", \"err\", err)\n\t}\n\treturn nil\n}\n"
                 "func Load() error {\n\tif err := read(); err != nil {\n\t\treturn nil\n\t}\n"
                 "\treturn nil\n}\nfunc All() {\n\tfor _, x := range xs {\n"
                 "\t\tif err := do(x); err != nil {\n\t\t\tcontinue\n\t\t}\n\t}\n}\n")
        self.assertEqual(kinds("a.go", hides), [("swallow", "Send"), ("swallow", "Load"),
                                                ("swallow", "All")])

    def test_go_error_position_decides(self):
        text = ("package x\nfunc Load() (config, error) {\n"
                "\tif err != nil {\n\t\treturn config{}, failure(\"PORT\")\n\t}\n"
                "\tif err != nil {\n\t\treturn config{}, nil\n\t}\n"
                "\tif err != nil { return config{}, nil }\n\treturn c, nil\n}\n")
        self.assertEqual([s.line for s in fallbacks.scan("a.go", text)], [6, 9])

    def test_catch_that_surfaces_or_codes_the_error(self):
        cpp = ("void F() {\n  try { g(); } catch (...) { if (r) *r = -1; }\n"
               "  try { g(); } catch (...) { ShowError(\"x\"); }\n"
               "  try { g(); } catch (...) { log.Error(\"x\"); }\n}\n")
        self.assertEqual([s.line for s in fallbacks.scan("a.cpp", cpp)], [4])

    def test_catch_that_returns_a_failure_status_translates_it(self):
        cpp = ("Status F() {\n  try { g(); } catch (...) { return Status::Fail(\"oom\"); }\n"
               "  try { g(); } catch (...) { result = Result::worker_failed; }\n"
               "  try { g(); } catch (...) { return false; }\n}\n")
        self.assertEqual(kinds("a.cpp", cpp), [("swallow", "F")])

    def test_declared_stops_end_the_failure(self):
        ps = "function F {\n  try { a } catch { Fail \"broken: $_\" }\n}\n"
        self.assertEqual(kinds("a.ps1", ps), [("swallow", "F")])
        self.assertEqual(fallbacks.scan("a.ps1", ps, frozenset({"Fail"})), [])
        py = "try:\n    a()\nexcept OSError as e:\n    die(str(e))\n"
        self.assertEqual(fallbacks.scan("a.py", py, frozenset({"die"})), [])

    def test_catch_that_exits_is_not_a_fallback(self):
        cs = "int F() {\n  try { G(); } catch (Exception) { Environment.Exit(3); }\n}\n"
        self.assertEqual(kinds("a.cs", cs), [])
        ps = "function F {\n  try { a } catch {\n    Write-Host $_\n    exit 1\n  }\n}\n"
        self.assertEqual(kinds("a.ps1", ps), [])

    def test_python_raise_in_nested_function_does_not_count(self):
        text = "try:\n    a()\nexcept Exception:\n    def later():\n        raise\n"
        self.assertEqual(kinds("a.py", text), [("swallow", "<module>")])

    def test_python_that_does_not_parse_is_not_clean(self):
        self.assertIsInstance(fallbacks.scan("a.py", "def (:\n"), str)

    def test_go_propagating_and_swallowing(self):
        text = ("package x\nfunc Load() error {\n"
                "\tif err != nil {\n\t\treturn fmt.Errorf(\"x: %w\", err)\n\t}\n"
                "\tif err := f(); err != nil {\n\t\treturn err\n\t}\n"
                "\tif err := g(); err != nil {\n\t\tlog.Printf(\"%v\", err)\n\t}\n"
                "\ts := \"if err != nil { }\"\n\treturn nil\n}\n")
        self.assertEqual(kinds("a.go", text), [("swallow", "Load")])

    def test_go_returning_another_value_is_a_fallback(self):
        text = "package x\nfunc Port() int {\n\tif err != nil {\n\t\treturn 8080\n\t}\n\treturn p\n}\n"
        self.assertEqual(kinds("a.go", text), [("swallow", "Port")])

    def test_catch_languages(self):
        js = ("function f() {\n  try { g() } catch (e) { console.log(e) }\n"
              "  try { g() } catch { throw new Error('x') }\n  p.catch(() => null)\n"
              "  q.catch(e => { throw e })\n}\n")
        self.assertEqual(kinds("a.ts", js), [("swallow", "f"), ("swallow", "f")])
        cs = "void F() {\n  try { G(); } catch (Exception) { }\n  try { G(); } catch { throw; }\n}\n"
        self.assertEqual(kinds("a.cs", cs), [("swallow", "F")])
        cpp = "int main() {\n  try { f(); } catch (...) { }\n}\n"
        self.assertEqual(kinds("a.cpp", cpp), [("swallow", "main")])

    def test_powershell(self):
        text = ("function Get-X {\n  Get-LocalUser x -ErrorAction 'SilentlyContinue'\n"
                "  # -ErrorAction SilentlyContinue in a comment\n"
                "  try { a } catch { Write-Host 'no' }\n  try { a } catch { throw }\n}\n")
        self.assertEqual(kinds("a.ps1", text), [("silent-continue", "Get-X"),
                                                ("swallow", "Get-X")])

    def test_marker_on_the_line_below(self):
        sites = fallbacks.scan("a.py", "try:\n    a()\nexcept OSError:\n    pass  # fallback: FB-0007\n")
        self.assertEqual(sites[0].marker, "FB-0007")

    def test_review_shapes_are_found(self):
        cs = "void F() {\n  try { G(); } catch (Exception e) when (Log(e)) { }\n}\n"
        self.assertEqual(kinds("a.cs", cs), [("swallow", "F")])
        js = ("function f() {\n  p.catch(async () => { await log() })\n  q.catch(noop)\n}\n")
        self.assertEqual(kinds("a.js", js), [("swallow", "f"), ("swallow", "f")])
        go = ("package x\nfunc F() int {\n\tif e := g(); e != nil {\n\t\tlog.Print(e)\n\t}\n"
              "\tif err != nil {\n\t\treturn cached\n\t}\n\treturn 1\n}\n")
        self.assertEqual(kinds("a.go", go), [("swallow", "F"), ("swallow", "F")])
        py = ("import os\nfrom os import environ as E\nfrom contextlib import suppress as S\n"
              "a = os.getenv('A') or 'd'\nb = E.get('B', 'x')\n"
              "with S(OSError):\n    pass\n"
              "def f():\n    try:\n        g()\n    finally:\n        return 1\n"
              "def h():\n    try:\n        g()\n    except OSError:\n        if False:\n"
              "            raise\n        return None\n")
        self.assertEqual([k for k, _ in kinds("a.py", py)],
                         ["env-default", "env-default", "suppress", "swallow", "swallow"])

    def test_names_that_merely_contain_error_do_not_excuse(self):
        js = "function f() {\n  try { g() } catch (e) { return defaultOnError(); }\n}\n"
        self.assertEqual(kinds("a.js", js), [("swallow", "f")])
        cpp = "void F() {\n  try { g(); } catch (...) { SetErrorMode(0); }\n}\n"
        self.assertEqual(kinds("a.cpp", cpp), [("swallow", "F")])

    def test_literals_do_not_hide_code(self):
        js = "const re = /\"/;\ntry { a() } catch (e) { }\nconst q = \"y\";\n"
        self.assertEqual(len(fallbacks.scan("a.js", js)), 1)
        cs = "void F() {\n  var p = @\"C:\\temp\\\";\n  try { G(); } catch { }\n  var q = \"y\";\n}\n"
        self.assertEqual(len(fallbacks.scan("a.cs", cs)), 1)

    def test_the_guarded_code_is_part_of_the_digest(self):
        a = "try:\n    cache()\nexcept Exception:\n    pass\n"
        b = "try:\n    charge_card()\nexcept Exception:\n    pass\n"
        self.assertNotEqual(fallbacks.scan("a.py", a)[0].digest, fallbacks.scan("a.py", b)[0].digest)
        ca = "void F() {\n  try { cache(); } catch (...) { }\n}\n"
        cb = "void F() {\n  try { charge(); } catch (...) { }\n}\n"
        self.assertNotEqual(fallbacks.scan("a.cpp", ca)[0].digest,
                            fallbacks.scan("a.cpp", cb)[0].digest)

    def test_a_marker_two_lines_up_belongs_to_another_site(self):
        text = ("try:\n    a()\nexcept OSError:  # fallback: FB-0001\n    pass\n"
                "try:\n    b()\nexcept OSError:\n    pass\n")
        markers = [s.marker for s in fallbacks.scan("a.py", text)]
        self.assertEqual(markers, ["FB-0001", None])

    def test_unknown_extension_is_reported(self):
        self.assertIsInstance(fallbacks.scan("a.rb", "begin; rescue; end"), str)


class Repo:
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="crossweft-fallbacks-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        run(["--root", str(self.root), "init"])
        self.write("app/config.py", "import os\n\ndef load():\n    try:\n        return read()\n"
                                    "    except OSError:\n        return {}\n")
        self.configure({"include": ["app/*.py"]})

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def configure(self, section: dict) -> None:
        path = self.root / "crossweft.json"
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg["fallbacks"] = section
        path.write_text(json.dumps(cfg), encoding="utf-8")

    def cw(self, *argv) -> tuple:
        return run(["--root", str(self.root), *argv])

    def problems(self) -> tuple:
        code, output = self.cw("check", "--format", "json")
        doc = json.loads(output)
        keys = sorted(p["key"] for p in doc["new"] if p["key"].startswith("fallback"))
        return code, keys, doc

    def registry(self) -> list:
        return json.loads((self.root / "seams/fallbacks.json").read_text(encoding="utf-8"))["fallbacks"]


class Workflow(Repo, unittest.TestCase):
    def test_unregistered_fails_and_propose_queues_it(self):
        code, keys, _ = self.problems()
        self.assertEqual(code, 1)
        self.assertEqual(len(keys), 1)
        self.assertTrue(keys[0].startswith("fallback:unregistered:app/config.py:load:swallow:"))
        code, out = self.cw("fallbacks", "propose", "--all", "--reason", "first run", "--by", "t")
        self.assertEqual(code, 0, out)
        code, keys, doc = self.problems()
        self.assertEqual((code, keys), (0, []), doc)
        self.assertTrue(any("1 of 1 wait for the owner's decision" in i for i in doc["info"]), doc)
        self.assertEqual(self.registry()[0]["status"], "proposed")

    def test_a_rejected_fallback_must_leave(self):
        self.cw("fallbacks", "propose", "--all", "--reason", "x")
        code, out = self.cw("fallbacks", "decide", "FB-0001", "reject", "--reason", "fail loud",
                            "--by", "owner")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.problems()[1], ["fallback:rejected:FB-0001"])
        self.write("app/config.py", "def load():\n    return read()\n")
        self.assertEqual(self.problems()[1], ["fallback:stale:FB-0001"])

    def test_an_agreed_fallback_may_not_change(self):
        self.cw("fallbacks", "propose", "--all", "--reason", "x")
        self.cw("fallbacks", "decide", "FB-0001", "approve", "--reason", "ok", "--by", "owner")
        self.assertEqual(self.problems()[:2], (0, []))
        self.write("app/config.py", "import os\n\ndef load():\n    try:\n        return read()\n"
                                    "    except Exception:\n        return {'mode': 'demo'}\n")
        self.assertEqual(self.problems()[1], ["fallback:changed:FB-0001"])
        code, out = self.cw("fallbacks", "decide", "FB-0001", "approve", "--reason", "ok",
                            "--by", "owner")
        self.assertEqual(code, 1, out)          # must be proposed again first
        self.cw("fallbacks", "propose", "FB-0001", "--reason", "wider catch")
        self.assertEqual(self.registry()[0]["status"], "proposed")
        self.assertEqual(self.problems()[:2], (0, []))

    def test_marker_keeps_the_tie_through_a_rename_and_a_wrong_marker_fails(self):
        self.cw("fallbacks", "propose", "--all", "--reason", "x")
        self.cw("fallbacks", "decide", "FB-0001", "approve", "--reason", "ok", "--by", "owner")
        self.write("app/config.py", "import os\n\ndef load_settings():\n    try:\n        return read()\n"
                                    "    except OSError:  # fallback: FB-0001\n        return {}\n")
        self.assertEqual(self.problems()[:2], (0, []))
        self.write("app/config.py", "import os\n\ndef load_settings():\n    try:\n        return read()\n"
                                    "    except OSError:  # fallback: FB-0099\n        return {}\n")
        keys = self.problems()[1]
        self.assertIn("fallback:stale:FB-0001", keys)
        self.assertTrue(any(k.startswith("fallback:unknown-marker:app/config.py:") for k in keys), keys)

    def test_a_rejected_wrapper_fails_while_it_is_called(self):
        self.write("app/config.py", "def read_or_default(f, d):\n    try:\n        return f()\n"
                                    "    except Exception:\n        return d\n")
        self.write("app/use.py", "from config import read_or_default\n\nx = read_or_default(g, 1)\n")
        _, keys, _ = self.problems()
        key = keys[0].split("fallback:unregistered:", 1)[1]
        self.cw("fallbacks", "propose", key, "--wrapper", "--reason", "helper")
        code, out = self.cw("fallbacks", "--json")
        row = json.loads(out)["fallbacks"][0]
        self.assertEqual((row["wrapper"], [c["path"] for c in row["calls"]]), (True, ["app/use.py"]))
        self.cw("fallbacks", "decide", "FB-0001", "reject", "--reason", "no", "--by", "owner")
        self.assertEqual(self.problems()[1], ["fallback:rejected:FB-0001"])

    def test_config_errors_are_loud(self):
        self.configure({"include": ["app/*.py", "nowhere/*.go"]})
        self.assertIn("fallback-config:empty:nowhere/*.go", self.problems()[1])
        self.write("app/broken.py", "def (:\n")
        self.configure({"include": ["app/*.py"]})
        self.assertIn("fallback-config:unscanned:app/broken.py", self.problems()[1])

    def test_one_approval_cannot_cover_a_copied_site(self):
        self.write("app/config.py", "def f():\n    try:\n        a()\n    except Exception:  "
                                    "# fallback: FB-0001\n        pass\n")
        self.cw("fallbacks", "propose", "--all", "--reason", "x")
        self.cw("fallbacks", "decide", "FB-0001", "approve", "--reason", "ok", "--by", "owner")
        self.write("app/config.py", "def f():\n    try:\n        a()\n    except Exception:  "
                                    "# fallback: FB-0001\n        pass\n\n"
                                    "def g():\n    try:\n        a()\n    except Exception:  "
                                    "# fallback: FB-0001\n        pass\n")
        keys = self.problems()[1]
        self.assertTrue(any(k.startswith("fallback:unknown-marker:app/config.py:") for k in keys),
                        keys)

    def test_identical_sites_in_one_function_register_separately(self):
        handler = "    try:\n        a()\n    except OSError:\n        pass\n"
        self.write("app/config.py", "def f():\n" + handler + handler)
        keys = self.problems()[1]
        self.assertEqual(len(keys), 2)
        self.assertTrue(keys[1].endswith("#2") or keys[0].endswith("#2"), keys)
        self.cw("fallbacks", "propose", "--all", "--reason", "x")
        self.assertEqual(len(self.registry()), 2)
        self.assertEqual(self.problems()[:2], (0, []))

    def test_strict_makes_a_waiting_fallback_fail(self):
        self.cw("fallbacks", "propose", "--all", "--reason", "x")
        self.configure({"include": ["app/*.py"], "strict": True})
        self.assertEqual(self.problems()[1], ["fallback:undecided:FB-0001"])

    def test_signing_cannot_be_switched_off_under_signed_decisions(self):
        self.cw("fallbacks", "propose", "--all", "--reason", "x")
        entries = self.registry()
        entries[0].update(status="approved", by="owner", sig="-----BEGIN SSH SIGNATURE-----")
        (self.root / "seams/fallbacks.json").write_text(json.dumps({"fallbacks": entries}),
                                                        encoding="utf-8")
        self.assertIn("fallback-config:signing-off", self.problems()[1])

    def test_ids_are_never_reused(self):
        self.write("app/b.py", "try:\n    a()\nexcept OSError:\n    pass\n")
        self.cw("fallbacks", "propose", "--all", "--reason", "x")
        self.assertEqual(sorted(e["id"] for e in self.registry()), ["FB-0001", "FB-0002"])
        self.write("app/b.py", "a()\n")
        path = self.root / "seams/fallbacks.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["fallbacks"] = [e for e in data["fallbacks"] if e["path"] != "app/b.py"]
        path.write_text(json.dumps(data), encoding="utf-8")
        self.write("app/c.py", "try:\n    a()\nexcept KeyError:\n    pass\n")
        self.cw("fallbacks", "propose", "--all", "--reason", "x")
        self.assertIn("FB-0003", [e["id"] for e in self.registry()])

    def test_an_older_decision_put_back_is_a_rollback(self):
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        git = lambda *a: subprocess.run(["git", "-C", str(self.root), *a], check=True,
                                        capture_output=True)
        git("init", "-q")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "0")
        self.cw("fallbacks", "propose", "--all", "--reason", "x")
        old = (self.root / "seams/fallbacks.json").read_text(encoding="utf-8")
        self.cw("fallbacks", "decide", "FB-0001", "reject", "--reason", "no", "--by", "owner")
        git("add", "-A")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "decided")
        (self.root / "seams/fallbacks.json").write_text(old, encoding="utf-8")
        self.assertIn("fallback:rollback:FB-0001", self.problems()[1])

    def test_unreadable_file_is_a_problem(self):
        from unittest import mock
        from crossweft import engine
        real = engine.Checker.text
        with mock.patch.object(engine.Checker, "text",
                               lambda self, rel: None if rel == "app/config.py" else real(self, rel)):
            keys = self.problems()[1]
        self.assertIn("fallback-config:unreadable:app/config.py", keys)

    def test_markdown_map(self):
        self.cw("fallbacks", "propose", "--all", "--reason", "x")
        code, _ = self.cw("fallbacks", "--md", "seams/fallbacks.md")
        page = (self.root / "seams/fallbacks.md").read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertIn("## proposed (1)", page)
        self.assertIn("FB-0001", page)


@unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen is not installed")
class Signed(Repo, unittest.TestCase):
    def setUp(self):
        super().setUp()
        keys = Path(tempfile.mkdtemp(prefix="crossweft-keys-"))
        self.addCleanup(shutil.rmtree, keys, ignore_errors=True)
        for name in ("owner", "agent"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name,
                            "-f", str(keys / name)], check=True)
        self.owner, self.agent = keys / "owner", keys / "agent"
        pub = (keys / "owner.pub").read_text(encoding="ascii").split()
        self.write("seams/allowed_signers", f"owner {pub[0]} {pub[1]}\n")
        self.configure({"include": ["app/*.py"], "signers": "seams/allowed_signers"})

    def test_only_the_owner_signature_counts(self):
        self.cw("fallbacks", "propose", "--all", "--reason", "x")
        code, out = self.cw("fallbacks", "decide", "FB-0001", "approve", "--reason", "ok",
                            "--by", "owner")
        self.assertEqual(code, 2, out)          # signers set: a key is required
        code, out = self.cw("fallbacks", "decide", "FB-0001", "approve", "--reason", "ok",
                            "--by", "owner", "--key", str(self.agent))
        self.assertEqual(code, 1, out)          # the agent's key does not verify
        code, out = self.cw("fallbacks", "decide", "FB-0001", "approve", "--reason", "ok",
                            "--by", "owner", "--key", str(self.owner))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.problems()[:2], (0, []))
        entries = self.registry()
        entries[0]["reason"] = "edited after signing"
        (self.root / "seams/fallbacks.json").write_text(json.dumps({"fallbacks": entries}),
                                                        encoding="utf-8")
        self.assertEqual(self.problems()[1], ["fallback:unsigned:FB-0001"])

    def test_hand_written_approval_without_signature_fails(self):
        self.cw("fallbacks", "propose", "--all", "--reason", "x")
        entries = self.registry()
        entries[0].update(status="approved", by="owner")
        (self.root / "seams/fallbacks.json").write_text(json.dumps({"fallbacks": entries}),
                                                        encoding="utf-8")
        self.assertEqual(self.problems()[1], ["fallback:unsigned:FB-0001"])


if __name__ == "__main__":
    unittest.main()
