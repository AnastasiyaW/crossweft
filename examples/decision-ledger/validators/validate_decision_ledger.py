"""validate_decision_ledger.py -- the agreed decisions are intact, signed and in force.

Fails when an entry of decisions/ledger.jsonl was edited, removed, inserted or reordered, when an
entry is not signed by a key in decisions/allowed_signers, when the signer file was swapped, when
decisions/current.json is not the fold of the ledger, or when a decision binds a guard id that the
crossweft model no longer has (deleting the guard would hide the drift).

    python validators/validate_decision_ledger.py            # normal run
    python validators/validate_decision_ledger.py --self-test
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "tools"))

import ledger as L  # noqa: E402


def problems_in(root: Path) -> tuple[list[str], int, int]:
    ledger, signers, current, _ = L._paths(root)
    for needed in (ledger, signers, current):
        if not needed.is_file():
            return [f"{needed.relative_to(root).as_posix()} is missing"], 0, 0
    try:
        entries = L.read_ledger(ledger)
    except (ValueError, json.JSONDecodeError) as exc:
        return [f"ledger.jsonl does not parse: {exc}"], 1, 0
    problems = L.check_chain(entries, signers)
    want = L.dump_current(L.fold(entries))
    if current.read_text(encoding="utf-8").replace("\r\n", "\n") != want:
        problems.append("decisions/current.json is not the fold of the ledger (it was edited by hand; "
                        "a change goes through propose -> approve)")
    guards = L.model_guards(root)
    pinned: set[str] = set()
    for subject, entry in sorted(L.in_force(entries).items()):
        pins = entry.get("guards")
        if not isinstance(pins, dict) or not pins or L.SETUP_PIN not in pins:
            problems.append(f"{entry.get('id')} ({subject}): no guards and {L.SETUP_PIN} signed with "
                            "the decision, so nothing ties it to the code")
            continue
        for ident, sha in sorted(pins.items()):
            if ident == L.SETUP_PIN:
                if L.setup_sha(root) != sha:
                    problems.append(f"{entry.get('id')}: {L.SETUP_PIN} changed after the decision was "
                                    "signed (check may now read another model); re-approve")
                continue
            pinned.add(ident)
            if ident not in guards:
                problems.append(f"{entry.get('id')}: guard {ident} was deleted from the crossweft model")
            elif L.guard_sha(guards[ident]) != sha:
                problems.append(f"{entry.get('id')}: guard {ident} changed after the decision was "
                                "signed (its paths or regex now read something else); re-approve")
    for finding in L.model_findings(root):
        keys = finding.get("detected_by") or []
        excused = sorted({g for g in pinned for key in (keys if isinstance(keys, list) else [keys])
                          for k in (str(key), str(key)[len("anchor:"):] if str(key).startswith("anchor:")
                                    else str(key))
                          if k == g or k.startswith(g + ":")})
        if excused:
            problems.append(f"finding {finding.get('id')} excuses {', '.join(excused)}, which a signed "
                            "decision binds: an agreed value cannot be waived by a finding")
    return problems, 3, len(entries)


def run(root: Path) -> int:
    problems, files, items = problems_in(root)
    for problem in problems:
        print(f"[FAIL] {problem}")
    print(f"SCANNED: files={files} items={items}")
    if not problems:
        print(f"[OK] {items} ledger entries chained and signed; current.json matches")
    return 1 if problems else 0


def _build(tmp: Path, key: Path, decisions: list[dict]) -> Path:
    root = tmp / "repo"
    shutil.copytree(HERE.parents[1], root, ignore=shutil.ignore_patterns("decisions", "__pycache__"))
    d = root / "decisions"
    d.mkdir()
    pub = key.with_suffix(".pub").read_text(encoding="ascii").split()
    (d / "allowed_signers").write_bytes(
        f"{L.IDENTITY} namespaces=\"{L.NAMESPACE}\" {pub[0]} {pub[1]}\n".encode("ascii"))
    genesis = L.make_entry(0, L.GENESIS_PREV, {"id": "D-0000", "op": "genesis",
                                               "signers": L.file_sha(d / "allowed_signers")}, key)
    entries = [genesis]
    for fields in decisions:
        fields = {**fields, "guards": L.guard_pins(root, fields["binds"])}
        entries.append(L.make_entry(len(entries), entries[-1]["hash"], fields, key))
    (d / "ledger.jsonl").write_bytes(
        "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in entries).encode("utf-8"))
    (d / "current.json").write_bytes(L.dump_current(L.fold(entries)).encode("utf-8"))
    return root


def _keygen(path: Path) -> None:
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "self-test", "-f", str(path)],
                   check=True, capture_output=True)


def self_test() -> int:
    decisions = [{"id": "D-0001", "op": "decide", "subject": "imagegen.sampler", "value": "dpmpp_2m_sde",
                  "binds": ["join:imagegen-sampler"], "text": "t"},
                 {"id": "D-0002", "op": "decide", "subject": "imagegen.steps", "value": 28,
                  "binds": ["join:imagegen-steps"], "text": "t"}]
    failures, checks = 0, 0
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        owner, other = tmp / "owner", tmp / "other"
        _keygen(owner)
        _keygen(other)

        def case(name: str, mutate, want_red: bool) -> None:
            nonlocal failures, checks
            work = tmp / name
            work.mkdir()
            root = _build(work, owner, decisions)
            mutate(root)
            problems, _, _ = problems_in(root)
            checks += 1
            if bool(problems) != want_red:
                failures += 1
                print(f"[SELF-TEST FAIL] {name}: expected {'red' if want_red else 'green'}, got {problems}")

        def lines(root: Path) -> list[dict]:
            return L.read_ledger(root / "decisions" / "ledger.jsonl")

        def write(root: Path, entries: list[dict]) -> None:
            (root / "decisions" / "ledger.jsonl").write_bytes(
                "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in entries).encode("utf-8"))

        def edit_old(root: Path) -> None:
            entries = lines(root)
            entries[1]["value"] = "euler"
            write(root, entries)

        def edit_and_rehash(root: Path) -> None:
            entries = lines(root)
            entries[1]["value"] = "euler"
            entries[1]["hash"] = L.entry_hash(entries[1])
            write(root, entries)

        def remove_middle(root: Path) -> None:
            write(root, [e for e in lines(root) if e["seq"] != 1])

        def append_by_agent(root: Path) -> None:
            entries = lines(root)
            entries.append(L.make_entry(len(entries), entries[-1]["hash"],
                                        {"id": "D-0003", "op": "supersede", "subject": "imagegen.steps",
                                         "value": 40, "binds": [], "text": "agent"}, other))
            write(root, entries)

        def append_by_owner(root: Path) -> None:
            entries = lines(root)
            entries.append(L.make_entry(len(entries), entries[-1]["hash"],
                                        {"id": "D-0003", "op": "supersede", "subject": "imagegen.steps",
                                         "value": 40, "binds": ["join:imagegen-steps"], "text": "owner",
                                         "guards": L.guard_pins(root, ["join:imagegen-steps"])},
                                        owner))
            write(root, entries)
            (root / "decisions" / "current.json").write_bytes(L.dump_current(L.fold(entries)).encode("utf-8"))

        def owner_signs_without_guards(root: Path) -> None:
            entries = lines(root)
            entries.append(L.make_entry(len(entries), entries[-1]["hash"],
                                        {"id": "D-0003", "op": "decide", "subject": "imagegen.seed",
                                         "value": 7, "binds": [], "text": "owner"}, owner))
            write(root, entries)
            (root / "decisions" / "current.json").write_bytes(L.dump_current(L.fold(entries)).encode("utf-8"))

        def excuse_with_finding(root: Path) -> None:
            path = root / "seams" / "model" / "10-system.json"
            model = json.loads(path.read_text(encoding="utf-8"))
            model["findings"] = [{"id": "SM-001", "title": "t", "summary": "s", "severity": "minor",
                                  "kind": "drift", "status": "open",
                                  "detected_by": ["join:imagegen-steps:00000000"]}]
            path.write_bytes(json.dumps(model, indent=2).encode("utf-8"))

        def swap_model_dir(root: Path) -> None:
            shutil.copytree(root / "seams" / "model", root / "seams" / "model2")
            config = root / "crossweft.json"
            data = json.loads(config.read_text(encoding="utf-8"))
            data["model_dir"] = "seams/model2"
            config.write_bytes(json.dumps(data, indent=2).encode("utf-8"))

        def repoint_guard(root: Path) -> None:
            path = root / "seams" / "model" / "10-system.json"
            text = path.read_text(encoding="utf-8")
            path.write_bytes(text.replace("STEPS = (", "OLD_STEPS = (", 1).encode("utf-8"))

        def swap_signers(root: Path) -> None:
            pub = other.with_suffix(".pub").read_text(encoding="ascii").split()
            (root / "decisions" / "allowed_signers").write_text(
                f"{L.IDENTITY} namespaces=\"{L.NAMESPACE}\" {pub[0]} {pub[1]}\n", encoding="ascii")

        def edit_current(root: Path) -> None:
            path = root / "decisions" / "current.json"
            path.write_text(path.read_text(encoding="utf-8").replace("28", "40"), encoding="utf-8")

        def drop_guard(root: Path) -> None:
            path = root / "seams" / "model" / "10-system.json"
            model = json.loads(path.read_text(encoding="utf-8"))
            model["joins"] = [j for j in model["joins"] if j["id"] != "imagegen-steps"]
            path.write_text(json.dumps(model, indent=2), encoding="utf-8")

        case("intact", lambda root: None, False)
        case("owner-appends-a-supersede", append_by_owner, False)
        case("old-entry-edited", edit_old, True)
        case("old-entry-edited-and-rehashed", edit_and_rehash, True)
        case("middle-entry-removed", remove_middle, True)
        case("agent-signs-with-its-own-key", append_by_agent, True)
        case("signer-file-swapped", swap_signers, True)
        case("current-json-edited-by-hand", edit_current, True)
        case("bound-guard-deleted", drop_guard, True)
        case("guard-regex-repointed", repoint_guard, True)
        case("drift-excused-by-a-finding", excuse_with_finding, True)
        case("model-dir-pointed-at-a-copy", swap_model_dir, True)
        case("decision-signed-without-guards", owner_signs_without_guards, True)
    print(f"SELF-TEST: checks={checks}")
    return 1 if failures else 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(self_test())
    sys.exit(run(HERE.parents[1]))
