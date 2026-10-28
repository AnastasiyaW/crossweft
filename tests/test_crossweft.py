"""End-to-end tests on the polyglot demo: break one side of each seam and prove
the right guard catches it -- and that an agent is told the right thing.

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
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "examples" / "polyglot-shop"
sys.path.insert(0, str(REPO))

from crossweft import cli, discover, engine, harness  # noqa: E402


def run(argv: list[str], stdin: str | None = None) -> tuple[int, str]:
    buffer = io.StringIO()
    old_stdin = sys.stdin
    try:
        if stdin is not None:
            sys.stdin = io.StringIO(stdin)
        with contextlib.redirect_stdout(buffer):
            code = cli.main(argv)
    finally:
        sys.stdin = old_stdin
    return code, buffer.getvalue()


class DemoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="crossweft-demo-")
        self.root = Path(self.tmp.name) / "shop"
        shutil.copytree(DEMO, self.root)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def edit(self, rel: str, old: str, new: str) -> None:
        path = self.root / rel
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text, f"test setup: {old!r} not in {rel}")
        path.write_text(text.replace(old, new), encoding="utf-8")

    def check(self, *extra: str) -> tuple[int, str]:
        return run(["--root", str(self.root), "check", *extra])

    # ------------------------------------------------------------- baseline
    def test_demo_is_consistent(self) -> None:
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn("RESULT: PASS", output)

    # ------------------------------------------------------ each guard bites
    def test_api_version_drift_on_one_side(self) -> None:
        self.edit("web/src/api.ts", '"2026-09-01"', '"2026-10-01"')
        code, output = self.check()
        self.assertEqual(code, 1)
        self.assertIn("join:api-version disagrees", output)

    def test_new_go_field_without_typescript(self) -> None:
        self.edit("server/main.go", '\tStatus     string   `json:"status"`',
                  '\tStatus     string   `json:"status"`\n\tCurrency   string   `json:"currency"`')
        code, output = self.check()
        self.assertEqual(code, 1)
        self.assertIn("set:order-fields:right-only:currency", output)

    def test_new_status_only_in_the_worker(self) -> None:
        self.edit("worker/worker.py", '{"pending", "paid", "shipped"}',
                  '{"pending", "paid", "shipped", "refunded"}')
        code, output = self.check()
        self.assertEqual(code, 1)
        self.assertIn("set:worker-statuses:left-only:refunded", output)

    def test_undeclared_env_var(self) -> None:
        self.edit("worker/worker.py", 'POLL_SECONDS = int(',
                  'TOKEN = os.environ["SHOP_WAREHOUSE_TOKEN"]\nPOLL_SECONDS = int(')
        code, output = self.check()
        self.assertEqual(code, 1)
        self.assertIn("set:env-worker-vars:right-only:SHOP_WAREHOUSE_TOKEN", output)

    def test_client_calls_a_route_nobody_mapped(self) -> None:
        self.edit("web/src/api.ts", "export async function createOrder",
                  'export const refund = () => fetch("/v1/refunds");\n\n'
                  "export async function createOrder")
        code, output = self.check()
        self.assertEqual(code, 1)
        self.assertIn("route:client-unexplained:/v1/refunds", output)

    def test_duplicated_algorithm_changed_on_one_side(self) -> None:
        self.edit("server/pricing.go", "+ 0.5", "+ 0.49")
        code, output = self.check()
        self.assertEqual(code, 1)
        self.assertIn("re-read web/src/pricing.ts", output)
        code, output = run(["--root", str(self.root), "attest", "price-rounding"])
        self.assertEqual(code, 2, "attest without --reason must be refused")
        reason = ["--reason", "Go now rounds down at .49 on purpose; TS updated in the same PR"]
        code, output = run(["--root", str(self.root), "attest", "price-rounding", *reason])
        self.assertEqual(code, 1, "only the Go region changed: attest must refuse " + output)
        self.assertIn("web/src/pricing.ts did not", output)
        self.edit("web/src/pricing.ts", "+ 0.5", "+ 0.49")
        code, output = run(["--root", str(self.root), "attest", "price-rounding", *reason])
        self.assertEqual(code, 0, output)
        self.assertEqual(self.check()[0], 0)
        self.edit("web/src/pricing.ts", "+ 0.49", "+ 0.49 ")
        code, output = self.check()
        self.assertEqual(code, 1, "the TS side changed after attestation -- must fail again")
        code, output = run(["--root", str(self.root), "attest", "price-rounding", "--reason",
                            "whitespace only; Go twin re-read, still equivalent",
                            "--other-side-unchanged"])
        self.assertEqual(code, 0, output)

    def test_github_annotations_point_at_the_file(self) -> None:
        self.edit("web/src/api.ts", '"2026-09-01"', '"2026-10-01"')
        code, output = self.check("--format", "github")
        self.assertEqual(code, 1)
        self.assertRegex(output, r"::error file=(web/src/api\.ts|web/public/app\.js|server/main\.go),"
                                 r"line=\d+")

    def test_sarif_locates_the_drift_and_keeps_the_exit_code(self) -> None:
        code, output = self.check("--format", "sarif")
        self.assertEqual(code, 0, output)
        run_ = json.loads(output)["runs"][0]
        self.assertEqual(run_["results"], [])
        self.assertTrue(run_["invocations"][0]["executionSuccessful"])
        self.edit("web/src/api.ts", '"2026-09-01"', '"2026-10-01"')
        code, output = self.check("--format", "sarif")
        self.assertEqual(code, 1)
        sarif = json.loads(output)
        self.assertEqual(sarif["version"], "2.1.0")
        rows = [r for r in sarif["runs"][0]["results"] if r["ruleId"] == "crossweft/joins"]
        self.assertTrue(rows, output)
        self.assertEqual(rows[0]["level"], "error")
        uri = rows[0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        self.assertIn(uri, ("web/src/api.ts", "web/public/app.js", "server/main.go"))
        self.assertTrue(rows[0]["partialFingerprints"]["crossweftKey/v1"].startswith(
            "join:api-version:"))
        for rule in sarif["runs"][0]["tool"]["driver"]["rules"]:
            self.assertTrue(rule["shortDescription"]["text"] and rule["help"]["text"])
        # no file of its own (a stale finding, an unloadable config): still located,
        # and an unreadable config is one failed SARIF run, never plain text or empty
        (self.root / "crossweft.json").write_text("{not json", encoding="utf-8")
        code, output = self.check("--format", "sarif")
        self.assertEqual(code, 2)
        run_ = json.loads(output)["runs"][0]
        self.assertFalse(run_["invocations"][0]["executionSuccessful"])
        self.assertTrue(run_["results"])
        for row in run_["results"]:
            region = row["locations"][0]["physicalLocation"]["region"]
            self.assertGreaterEqual(region["startLine"], 1)

    def test_baseline_records_old_drift_and_new_drift_still_fails(self) -> None:
        self.edit("web/src/api.ts", '"2026-09-01"', '"2026-10-01"')
        code, output = run(["--root", str(self.root), "baseline"])
        self.assertEqual(code, 2, "owner and next step are required")
        code, output = run(["--root", str(self.root), "baseline", "--owner", "web team",
                            "--next-step", "align the API version"])
        self.assertEqual(code, 0, output)
        code, output = run(["--root", str(self.root), "render"])
        self.assertEqual(code, 0, output)
        code, output = self.check()
        self.assertEqual(code, 0, output)
        self.assertIn("known=", output)
        code, output = run(["--root", str(self.root), "baseline", "--owner", "x",
                            "--next-step", "y"])
        self.assertEqual(code, 2, "an existing baseline file is never overwritten")
        self.edit("worker/worker.py", '"shipped"}', '"shipped", "refunded"}')
        code, output = self.check()
        self.assertEqual(code, 1, "drift after the baseline must still fail")
        self.edit("worker/worker.py", '"shipped", "refunded"}', '"shipped"}')
        self.edit("web/src/api.ts", '"2026-10-01"', '"2026-09-01"')
        code, output = self.check()
        self.assertEqual(code, 1, "a fixed baseline finding goes stale and fails")
        self.assertIn("stale", output)

    def test_baseline_refuses_problems_a_finding_cannot_excuse(self) -> None:
        self.edit("web/src/api.ts", "export async function getOrder", "export async function fetchOrder")
        code, output = run(["--root", str(self.root), "baseline", "--owner", "x",
                            "--next-step", "y"])
        self.assertEqual(code, 1, output)
        self.assertFalse((self.root / "seams/model/90-baseline.json").exists())

    def test_stale_generated_docs(self) -> None:
        self.edit("seams/model/10-system.json", '"Reads and places orders."',
                  '"Reads, places and lists orders."')
        code, output = self.check()
        self.assertEqual(code, 1)
        self.assertIn("generated docs are stale", output)

    # --------------------------------------------------------------- impact
    def test_impact_names_the_other_side(self) -> None:
        code, output = run(["--root", str(self.root), "impact", str(self.root / "web/src/api.ts")])
        self.assertEqual(code, 0, output)
        self.assertIn("link:web-orders", output)
        self.assertIn("re-check: server/main.go", output)

    def test_impact_paths_are_relative_to_the_working_directory(self) -> None:
        old = os.getcwd()
        try:
            os.chdir(self.root / "web")
            code, output = run(["impact", "src/api.ts"])
        finally:
            os.chdir(old)
        self.assertEqual(code, 0, output)
        self.assertIn("link:web-orders", output)

    def test_impact_on_an_unmapped_file_says_so_instead_of_fix_the_other_side(self) -> None:
        stray = self.root / "notes.txt"
        stray.write_text("not code of any block\n", encoding="utf-8")
        code, output = run(["--root", str(self.root), "impact", str(stray)])
        self.assertEqual(code, 0, output)
        self.assertIn("Not on the map (1): notes.txt", output)
        self.assertIn("Nothing on the map is touched", output)
        self.assertNotIn("Next: fix the other sides", output)

    def test_impact_outside_a_git_work_tree_is_a_clear_error(self) -> None:
        ceiling = str(self.root.parent)
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": ceiling}):
            code, output = run(["--root", str(self.root), "impact"])
        self.assertEqual(code, 2, output)
        self.assertIn("not inside a git work tree", output)
        self.assertNotIn("--no-index", output)

    def test_impact_in_a_monorepo_subdirectory(self) -> None:
        """crossweft.json below the git top level: diff paths must still match."""
        mono = Path(self.tmp.name) / "mono"
        shutil.copytree(self.root, mono / "shop")
        git = ["git", "-C", str(mono), "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(git + ["init", "-q"], check=True)
        subprocess.run(git + ["add", "-A"], check=True)
        subprocess.run(git + ["commit", "-q", "-m", "base"], check=True)
        api = mono / "shop/web/src/api.ts"
        api.write_text(api.read_text(encoding="utf-8").replace("2026-09-01", "2026-10-01"),
                       encoding="utf-8")
        code, output = run(["--root", str(mono / "shop"), "impact"])
        self.assertEqual(code, 0, output)
        self.assertIn("link:web-orders", output)
        self.assertNotIn("Not on the map", output)

    def test_malformed_model_is_an_error_not_a_crash(self) -> None:
        model = self.root / "seams/model/10-system.json"
        data = json.loads(model.read_text(encoding="utf-8"))
        data["blocks"][0]["kind"] = ["site"]
        data["joins"][0]["points"] = 5
        data["links"][0]["from"] = {"x": 1}
        model.write_text(json.dumps(data), encoding="utf-8")
        code, output = self.check()
        self.assertEqual(code, 2, output)  # the model did not load: no verdict
        self.assertIn("model schema", output)
        _, out = self.hook("claude-code", "stop", {"stop_hook_active": False})
        self.assertEqual(out["decision"], "block")
        self.assertIn("model schema", out["reason"])

    def test_empty_pair_region_is_refused(self) -> None:
        self.edit("server/pricing.go", "func totalCents(unitPrice float64, quantity int) int64 {\n"
                  "\treturn int64(math.Floor(unitPrice*float64(quantity)*100 + 0.5))\n}\n", "")
        code, output = self.check()
        self.assertEqual(code, 1)
        self.assertIn("region between the markers is empty", output)

    def test_planned_link_may_name_an_unserved_route(self) -> None:
        model = self.root / "seams/model/10-system.json"
        data = json.loads(model.read_text(encoding="utf-8"))
        data["links"].append({"id": "web-refunds", "from": "web", "to": "api", "transport": "https",
                              "status": "planned", "summary": "Refunds, next quarter.",
                              "identifiers": {"route": ["POST /v1/refunds"]}})
        model.write_text(json.dumps(data), encoding="utf-8")
        run(["--root", str(self.root), "render"])
        code, output = self.check()
        self.assertEqual(code, 0, output)

    def test_render_never_overwrites_a_foreign_file(self) -> None:
        (self.root / "seams/README.md").write_text("# our own notes\n", encoding="utf-8")
        self.edit("seams/model/10-system.json", '"Reads and places orders."', '"Reads orders."')
        code, output = run(["--root", str(self.root), "render"])
        self.assertEqual(code, 2, output)
        self.assertEqual((self.root / "seams/README.md").read_text(encoding="utf-8"),
                         "# our own notes\n")

    def test_attest_rejects_unknown_ids_before_writing(self) -> None:
        lock = self.root / "seams/pairs.lock.json"
        before = lock.read_text(encoding="utf-8")
        code, output = run(["--root", str(self.root), "attest", "--prune", "nosuch"])
        self.assertEqual(code, 2, output)
        self.assertNotIn("pruned", output)
        self.assertEqual(lock.read_text(encoding="utf-8"), before)

    # ---------------------------------------------------------------- hooks
    def hook(self, agent: str, event: str, payload: dict, *flags: str) -> tuple[int, dict | None]:
        payload = {"cwd": str(self.root), "session_id": "test-" + self._testMethodName, **payload}
        code, output = run(["hook", agent, event, *flags], json.dumps(payload))
        return code, (json.loads(output) if output.strip() else None)

    # ------------------------------------------------ the repository's harness
    def settings(self) -> dict:
        return json.loads((self.root / ".claude/settings.json").read_text(encoding="utf-8"))

    def write_settings(self, data: dict) -> None:
        (self.root / ".claude/settings.json").write_text(json.dumps(data), encoding="utf-8")

    def test_demo_carries_its_agent_harness(self) -> None:
        for rel in (".claude/settings.json", "AGENTS.md", ".claude/skills/crossweft/SKILL.md"):
            self.assertTrue((self.root / rel).is_file(), rel)
        code, output = run(["--root", str(self.root), "agents"])
        self.assertEqual(code, 0, output)
        self.assertIn("already in place", output)

    def test_removing_the_stop_hook_fails_the_check(self) -> None:
        data = self.settings()
        del data["hooks"]["Stop"]
        self.write_settings(data)
        code, output = self.check()
        self.assertEqual(code, 1, output)
        self.assertIn("agents:hooks:stop", output)

    def test_an_edited_agents_block_fails_the_check(self) -> None:
        self.edit("AGENTS.md", "Never weaken a guard", "Weaken a guard")
        code, output = self.check()
        self.assertEqual(code, 1, output)
        self.assertIn("agents:agents-md", output)

    def test_agents_repairs_the_harness_and_keeps_other_content(self) -> None:
        data = self.settings()
        del data["hooks"]["Stop"]
        data["hooks"]["PreToolUse"] = [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "./lint.sh"}]}]
        data["permissions"] = {"allow": ["Bash(make test)"]}
        self.write_settings(data)
        (self.root / "CLAUDE.md").write_text("# Team rules\n\nUse tabs.\n", encoding="utf-8")
        code, output = self.check()
        self.assertIn("agents:claude-md", output)
        code, output = run(["--root", str(self.root), "agents"])
        self.assertEqual(code, 0, output)
        repaired = self.settings()
        self.assertEqual(repaired["hooks"]["PreToolUse"], data["hooks"]["PreToolUse"])
        self.assertEqual(repaired["permissions"], data["permissions"])
        self.assertIn("hook claude-code stop", json.dumps(repaired["hooks"]["Stop"]))
        claude_md = (self.root / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertTrue(claude_md.startswith("# Team rules\n\nUse tabs.\n"))
        self.assertIn("@AGENTS.md", claude_md)
        code, output = self.check()
        self.assertEqual(code, 0, output)

    def test_agents_refuses_a_foreign_skill_and_writes_nothing(self) -> None:
        data = self.settings()
        del data["hooks"]["Stop"]
        self.write_settings(data)
        (self.root / ".claude/skills/crossweft/SKILL.md").write_text(
            "---\nname: crossweft\ndescription: ours\n---\nour own skill\n", encoding="utf-8")
        code, output = run(["--root", str(self.root), "agents"])
        self.assertEqual(code, 2, output)
        self.assertIn("not generated by crossweft", output)
        self.assertNotIn("Stop", self.settings()["hooks"])

    def test_invalid_settings_are_reported_not_overwritten(self) -> None:
        (self.root / ".claude/settings.json").write_text("{", encoding="utf-8")
        code, output = run(["--root", str(self.root), "agents"])
        self.assertEqual(code, 2, output)
        self.assertEqual((self.root / ".claude/settings.json").read_text(encoding="utf-8"), "{")
        code, output = self.check()
        self.assertEqual(code, 1, output)
        self.assertIn("agents:hooks", output)

    def test_plugin_hook_is_quiet_only_when_the_project_hooks_run(self) -> None:
        self.edit("web/src/api.ts", '"2026-09-01"', '"2026-10-01"')
        # the demo's hooks start `crossweft`: they run only where the hook shell
        # finds one that answers like crossweft, in a session Claude Code
        # started in the demo's root
        launcher_dir = Path(self.tmp.name) / "bin"
        launcher_dir.mkdir()
        launcher = launcher_dir / "crossweft"
        launcher.write_text('#!/bin/sh\necho "crossweft 9.9.9"\n', encoding="ascii")
        launcher.chmod(0o755)
        shell = shutil.which("bash") if os.name == "nt" else shutil.which("sh")
        if not shell or "system32" in shell.lower():
            self.skipTest("no shell to run the hook command in")
        session = {"CLAUDE_PROJECT_DIR": str(self.root), "CROSSWEFT_SHELL": shell}
        with mock.patch.dict(os.environ, {"PATH": str(launcher_dir), **session}):
            self.assertEqual(self.hook("claude-code", "stop", {}, "--from-plugin"), (0, None))
        with mock.patch.dict(os.environ, {"PATH": str(self.root), **session}):
            _, out = self.hook("claude-code", "stop", {}, "--from-plugin")
        self.assertEqual(out["decision"], "block", "no `crossweft` on PATH: the plugin speaks")
        (self.root / ".claude/settings.json").unlink()
        _, out = self.hook("claude-code", "stop", {}, "--from-plugin")
        self.assertEqual(out["decision"], "block")

    def test_init_installs_the_harness_unless_asked_not_to(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fresh, bare = Path(tmp) / "fresh", Path(tmp) / "bare"
            fresh.mkdir()
            bare.mkdir()
            code, output = run(["--root", str(fresh), "init"])
            self.assertEqual(code, 0, output)
            config = json.loads((fresh / "crossweft.json").read_text(encoding="utf-8"))
            self.assertEqual(config["agent"], {"harness": True})
            self.assertEqual(harness.problems(fresh, "seams/model"), [])
            code, output = run(["--root", str(bare), "init", "--no-agents"])
            self.assertEqual(code, 0, output)
            self.assertFalse((bare / ".claude").exists())
            self.assertFalse((bare / "AGENTS.md").exists())

    def test_claude_post_edit_tells_the_agent_the_other_side(self) -> None:
        code, out = self.hook("claude-code", "post-edit", {
            "hook_event_name": "PostToolUse", "tool_name": "Edit",
            "tool_input": {"file_path": str(self.root / "web/src/api.ts")}})
        self.assertEqual(code, 0)
        context = out["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("server/main.go", context)
        self.assertIn("join:api-version", context)

    def test_post_edit_reports_a_disagreement_immediately(self) -> None:
        self.edit("web/src/api.ts", '"2026-09-01"', '"2026-10-01"')
        _, out = self.hook("claude-code", "post-edit", {
            "tool_name": "Edit", "tool_input": {"file_path": "web/src/api.ts"}})
        self.assertIn("Right now these checks disagree", out["hookSpecificOutput"]["additionalContext"])

    def test_post_edit_is_silent_off_the_map(self) -> None:
        (self.root / "notes.md").write_text("hello\n", encoding="utf-8")
        code, out = self.hook("claude-code", "post-edit", {
            "tool_name": "Write", "tool_input": {"file_path": str(self.root / "notes.md")}})
        self.assertEqual((code, out), (0, None))

    def test_codex_apply_patch_paths(self) -> None:
        patch = "*** Begin Patch\n*** Update File: server/main.go\n@@\n*** End Patch\n"
        _, out = self.hook("codex", "post-edit", {"tool_name": "apply_patch",
                                                  "tool_input": {"command": patch}})
        self.assertIn("web/src/api.ts", out["hookSpecificOutput"]["additionalContext"])

    def test_stop_blocks_while_making_progress_then_warns(self) -> None:
        self.edit("web/src/api.ts", '"2026-09-01"', '"2026-10-01"')
        _, first = self.hook("claude-code", "stop", {"stop_hook_active": False})
        self.assertEqual(first["decision"], "block")
        self.assertIn("join:api-version", first["reason"])
        # the agent tried again but nothing changed: no endless loop, but the
        # user is told the check still fails
        _, second = self.hook("claude-code", "stop", {"stop_hook_active": True})
        self.assertNotIn("decision", second)
        self.assertIn("still fails", second["systemMessage"])

    def test_stop_passes_quietly_when_consistent(self) -> None:
        code, out = self.hook("claude-code", "stop", {"stop_hook_active": False})
        self.assertEqual((code, out), (0, None))

    def test_other_agents_stop_formats(self) -> None:
        self.edit("web/src/api.ts", '"2026-09-01"', '"2026-10-01"')
        self.assertEqual(self.hook("gemini", "stop", {})[1]["decision"], "deny")
        self.assertEqual(self.hook("copilot", "stop", {})[1]["decision"], "block")
        self.assertIn("followup_message", self.hook("cursor", "stop", {"loop_count": 0})[1])

    def test_hook_outside_an_adopted_repo_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as elsewhere:
            code, output = run(["hook", "claude-code", "stop"], json.dumps({"cwd": elsewhere}))
        self.assertEqual((code, output), (0, ""))

    def test_session_start_describes_the_map(self) -> None:
        _, out = self.hook("claude-code", "session-start", {})
        self.assertIn("current links", out["hookSpecificOutput"]["additionalContext"])

    # -------------------------------------------------------------- discover
    def test_discover_finds_the_unguarded_values(self) -> None:
        model = self.root / "seams/model/10-system.json"
        data = json.loads(model.read_text(encoding="utf-8"))
        data["joins"] = []                      # forget the guards...
        model.write_text(json.dumps(data), encoding="utf-8")
        (self.root / "seams/model/00-meta.json").write_text(
            json.dumps({"meta": {"schema": engine.SCHEMA_ID, "title": "t",
                                 "lanes": [{"id": "a", "name": "A"}]}}), encoding="utf-8")
        report = discover.discover(engine.load_config(self.root))
        found = {c["value"]: c for c in report["candidates"]}
        self.assertIn("X-Shop-Api-Version", found)
        self.assertIn("2026-09-01", found)
        join = found["2026-09-01"]["join"]
        self.assertIsNotNone(join, found["2026-09-01"])
        self.assertEqual({p["path"] for p in join["points"]},
                         {"web/src/api.ts", "web/public/app.js", "server/main.go"})
        self.assertIsNotNone(report["env"])

    def test_discover_suggested_joins_pass_check(self) -> None:
        """A suggested guard must be usable as-is: paste it and check passes."""
        model = self.root / "seams/model/10-system.json"
        data = json.loads(model.read_text(encoding="utf-8"))
        data["joins"] = []
        model.write_text(json.dumps(data), encoding="utf-8")
        report = discover.discover(engine.load_config(self.root))
        suggested = [c["join"] for c in report["candidates"] if c["join"]]
        self.assertTrue(suggested)
        data["joins"] = suggested
        model.write_text(json.dumps(data), encoding="utf-8")
        result = engine.evaluate(engine.load_config(self.root))
        joins = [p for p in result.new if p.category in ("joins", "anchors")]
        self.assertEqual(joins, [], [p.message for p in joins])


class SelfTests(unittest.TestCase):
    def test_engine_and_runner_self_tests(self) -> None:
        code, output = run(["self-test"])
        self.assertEqual(code, 0, output)
        self.assertEqual(output.count("SELF-TEST: checks="), 2)

    def test_basename_transform_takes_the_last_segment_of_any_separator(self) -> None:
        for raw, want in (("src/app/demo.h", "demo.h"), ("src\\app\\demo.h", "demo.h"),
                          ("src/app/", "app"), ("demo.h", "demo.h")):
            self.assertEqual(engine.Checker._transform(raw, ["basename"]), want, raw)


class PackagingTest(unittest.TestCase):
    def test_module_entry_point(self) -> None:
        proc = subprocess.run([sys.executable, "-m", "crossweft", "--version"], cwd=REPO,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("crossweft", proc.stdout)


if __name__ == "__main__":
    unittest.main()
