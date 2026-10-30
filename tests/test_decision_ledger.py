"""examples/decision-ledger: a later chat that changes an agreed pipeline setting is stopped.

Changing the code alone fails a join; changing the code and current.json fails the ledger
validator; appending an entry signed with the agent's own key fails it too. Only an entry the
owner's key signed (propose -> approve) makes both green.

Needs `ssh-keygen` with `-Y sign` (OpenSSH 8.0+); skipped without it.
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
EXAMPLE = REPO / "examples" / "decision-ledger"
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(EXAMPLE / "tools"))

from crossweft import cli  # noqa: E402
import ledger as L  # noqa: E402


def has_ssh_keygen_sign() -> bool:
    try:
        done = subprocess.run(["ssh-keygen", "-Y", "sign"], capture_output=True, text=True)
    except OSError:
        return False
    return "Too few arguments" in done.stderr or "usage" in done.stderr.lower()


def check(root: Path) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = cli.main(["--root", str(root), "check"])
    return code, buf.getvalue()


@unittest.skipUnless(shutil.which("ssh-keygen") and has_ssh_keygen_sign(), "ssh-keygen -Y not available")
class LaterChat(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="cw-ledger-"))
        self.root = self.tmp / "repo"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("__pycache__"))
        self.owner, self.agent = self.tmp / "owner", self.tmp / "agent"
        for key in (self.owner, self.agent):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                           check=True, capture_output=True)
        # the repository carries no signatures: build the example ledger with a test owner key
        L.build_demo(self.root, self.owner)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, entries: list[dict]) -> None:
        d = self.root / "decisions"
        (d / "ledger.jsonl").write_bytes("".join(json.dumps(e, separators=(",", ":")) + "\n"
                                                 for e in entries).encode("utf-8"))
        (d / "current.json").write_bytes(L.dump_current(L.fold(entries)).encode("utf-8"))

    def validator(self) -> tuple[int, str]:
        done = subprocess.run([sys.executable, str(self.root / "validators" / "validate_decision_ledger.py")],
                              capture_output=True, text=True)
        return done.returncode, done.stdout + done.stderr

    def set_steps_in_code(self, steps: int) -> None:
        path = self.root / "pipeline" / "config.py"
        path.write_bytes(path.read_text(encoding="utf-8").replace("STEPS = 28", f"STEPS = {steps}")
                         .encode("utf-8"))

    def test_intact_is_green(self) -> None:
        self.assertEqual(check(self.root)[0], 0)
        self.assertEqual(self.validator()[0], 0)

    def test_changing_the_code_alone_fails_the_join(self) -> None:
        self.set_steps_in_code(40)
        code, output = check(self.root)
        self.assertEqual(code, 1, output)
        self.assertIn("imagegen-steps", output)

    def test_repointing_the_guard_at_another_line_fails_the_validator(self) -> None:
        model = self.root / "seams" / "model" / "10-system.json"
        model.write_bytes(model.read_text(encoding="utf-8").replace("^STEPS = (", "^OLD_STEPS = (", 1)
                          .encode("utf-8"))
        config = self.root / "pipeline" / "config.py"
        config.write_bytes(config.read_text(encoding="utf-8").replace("STEPS = 28", "OLD_STEPS = 28\nSTEPS = 50")
                           .encode("utf-8"))
        self.assertEqual(check(self.root)[0], 0, "the repointed join alone is satisfied")
        code, output = self.validator()
        self.assertEqual(code, 1, output)
        self.assertIn("changed after the decision was signed", output)

    def test_excusing_the_drift_with_a_finding_fails_the_validator(self) -> None:
        self.set_steps_in_code(50)
        model = self.root / "seams" / "model" / "10-system.json"
        data = json.loads(model.read_text(encoding="utf-8"))
        code, output = check(self.root)
        key = next(line.split("key: ")[1].split()[0] for line in output.splitlines() if "key: join:" in line)
        data["findings"] = [{"id": "SM-001", "title": "steps drift", "summary": "later", "severity": "minor",
                             "kind": "drift", "status": "open", "detected_by": [key], "owner": "agent",
                             "next_step": "later"}]
        model.write_bytes(json.dumps(data, indent=2).encode("utf-8"))
        code, output = self.validator()
        self.assertEqual(code, 1, output)
        self.assertIn("cannot be waived by a finding", output)

    def test_pointing_model_dir_at_an_edited_copy_fails_the_validator(self) -> None:
        shutil.copytree(self.root / "seams" / "model", self.root / "seams" / "model2")
        copy = self.root / "seams" / "model2" / "10-system.json"
        copy.write_bytes(copy.read_text(encoding="utf-8").replace("^STEPS = (", "^OLD_STEPS = (", 1)
                         .encode("utf-8"))
        config = self.root / "crossweft.json"
        data = json.loads(config.read_text(encoding="utf-8"))
        data["model_dir"] = "seams/model2"
        config.write_bytes(json.dumps(data, indent=2).encode("utf-8"))
        code, output = self.validator()
        self.assertEqual(code, 1, output)

    def test_an_expression_instead_of_the_value_fails_the_join(self) -> None:
        for spelling in ("STEPS = 28 * 2", 'SAMPLER = "dpmpp_2m_sde"[:5] + "a"'):
            with self.subTest(spelling=spelling):
                config = self.root / "pipeline" / "config.py"
                original = config.read_bytes()
                key = spelling.split(" = ")[0] + " = "
                text = original.decode("utf-8")
                line = next(l for l in text.splitlines() if l.startswith(key))
                config.write_bytes(text.replace(line, spelling).encode("utf-8"))
                self.assertEqual(check(self.root)[0], 1)
                config.write_bytes(original)

    def test_changing_code_and_current_json_fails_the_validator(self) -> None:
        self.set_steps_in_code(40)
        path = self.root / "decisions" / "current.json"
        path.write_bytes(path.read_text(encoding="utf-8").replace('"imagegen.steps": 28',
                                                                  '"imagegen.steps": 40').encode("utf-8"))
        self.assertEqual(check(self.root)[0], 0)
        code, output = self.validator()
        self.assertEqual(code, 1, output)
        self.assertIn("not the fold of the ledger", output)

    def test_an_entry_signed_by_the_agent_fails(self) -> None:
        entries = L.read_ledger(self.root / "decisions" / "ledger.jsonl")
        entries.append(L.make_entry(len(entries), entries[-1]["hash"],
                                    {"id": "D-0004", "op": "supersede", "subject": "imagegen.steps",
                                     "value": 40, "binds": ["join:imagegen-steps"], "text": "faster"},
                                    self.agent))
        self._write(entries)
        self.set_steps_in_code(40)
        self.assertEqual(check(self.root)[0], 0)
        code, output = self.validator()
        self.assertEqual(code, 1, output)
        self.assertIn("signature", output)

    def test_editing_an_old_decision_fails_even_when_re_signed_by_the_agent(self) -> None:
        entries = L.read_ledger(self.root / "decisions" / "ledger.jsonl")
        entries[2]["value"] = 40
        entries[2]["hash"] = L.entry_hash(entries[2])
        entries[2]["sig"] = L.sign(entries[2]["hash"], self.agent)
        self._write(entries)
        self.set_steps_in_code(40)
        code, output = self.validator()
        self.assertEqual(code, 1, output)

    def test_propose_then_owner_approve_makes_both_green(self) -> None:
        tool = str(self.root / "tools" / "ledger.py")
        propose = subprocess.run([sys.executable, tool, "--root", str(self.root), "propose",
                                  "--op", "supersede", "--subject", "imagegen.steps", "--value", "32",
                                  "--bind", "join:imagegen-steps", "--text", "owner asked", "--by", "agent"],
                                 capture_output=True, text=True)
        self.assertEqual(propose.returncode, 0, propose.stderr)
        self.assertEqual(self.validator()[0], 0, "a pending proposal is not in force")
        approve = subprocess.run([sys.executable, tool, "--root", str(self.root), "approve", "D-0004",
                                  "--key", str(self.owner)], capture_output=True, text=True)
        self.assertEqual(approve.returncode, 0, approve.stderr)
        self.assertEqual(check(self.root)[0], 1, "the code still says 28")
        self.set_steps_in_code(32)
        self.assertEqual(check(self.root)[0], 0)
        self.assertEqual(self.validator()[0], 0)


if __name__ == "__main__":
    unittest.main()
