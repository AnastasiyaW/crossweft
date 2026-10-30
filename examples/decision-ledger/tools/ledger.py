"""Append-only, signed decision ledger: decisions/ledger.jsonl.

Each line is one entry. `prev` is the previous entry's `hash`, `hash` is the sha256 of the
entry without `hash` and `sig`, and `sig` is an SSH signature over `hash` (namespace
`decision-ledger`) by a key in decisions/allowed_signers. Entry 0 (`genesis`) pins the sha256
of allowed_signers, so swapping the signer file breaks the chain.

An agent can only propose: `propose` writes decisions/pending/<id>.json, unsigned. The owner
approves in her own terminal: `approve` shows the change, signs it with her key and appends it.
For a key an agent cannot use, make it a FIDO2 key that asks for a touch and a PIN:
`ssh-keygen -t ed25519-sk -O verify-required`.

    python tools/ledger.py propose --subject imagegen.steps --value 30 --bind join:imagegen-steps \
        --text "why" [--by <agent/session>]
    python tools/ledger.py approve D-0003 --key ~/.ssh/decisions_sk
    python tools/ledger.py fold        # rewrite decisions/current.json from the ledger
    python tools/ledger.py demo --key ~/.ssh/decisions_sk   # build the example ledger, signed
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

NAMESPACE = "decision-ledger"
IDENTITY = "owner"
GENESIS_PREV = "sha256:0"
OPS = ("genesis", "decide", "supersede", "revoke")


def canonical(entry: dict) -> bytes:
    body = {k: v for k, v in entry.items() if k not in ("hash", "sig")}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def entry_hash(entry: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical(entry)).hexdigest()


def file_sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def read_ledger(path: Path) -> list[dict]:
    entries = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise ValueError(f"{path.name}:{number}: empty line")
        entries.append(json.loads(line))
    return entries


def sign(data: str, key: Path) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        msg = Path(tmp) / "hash"
        msg.write_bytes(data.encode("ascii"))
        done = subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", NAMESPACE,
                               str(msg)], capture_output=True, text=True)
        if done.returncode != 0:
            raise RuntimeError(f"ssh-keygen sign failed: {done.stderr.strip()}")
        return (Path(tmp) / "hash.sig").read_text(encoding="ascii")


def verify(data: str, sig: str, signers: Path) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        sig_file = Path(tmp) / "entry.sig"
        sig_file.write_bytes(sig.encode("ascii"))
        done = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(signers), "-I", IDENTITY,
                               "-n", NAMESPACE, "-s", str(sig_file)],
                              input=data.encode("ascii"), capture_output=True)
        return done.returncode == 0


def check_chain(entries: list[dict], signers: Path) -> list[str]:
    """Problems with the chain: order, links, hashes, signatures, the pinned signer file."""
    problems = []
    if not entries:
        return ["the ledger has no entries (a genesis entry is required)"]
    prev = GENESIS_PREV
    for index, entry in enumerate(entries):
        where = f"entry {index} ({entry.get('id', '?')})"
        if entry.get("seq") != index:
            problems.append(f"{where}: seq is {entry.get('seq')!r}, expected {index} "
                            "(an entry was removed, inserted or reordered)")
        if entry.get("op") not in OPS or (index == 0) != (entry.get("op") == "genesis"):
            problems.append(f"{where}: op {entry.get('op')!r} -- entry 0 and only entry 0 is genesis")
        if entry.get("prev") != prev:
            problems.append(f"{where}: prev does not match the hash of the entry before it")
        if entry.get("hash") != entry_hash(entry):
            problems.append(f"{where}: hash does not match its content (the entry was edited)")
        if not isinstance(entry.get("sig"), str) or not verify(str(entry.get("hash")), entry["sig"],
                                                               signers):
            problems.append(f"{where}: signature missing or not made by a key in "
                            f"{signers.name} (an agent cannot approve a decision)")
        prev = entry.get("hash")
    if entries[0].get("signers") != file_sha(signers):
        problems.append(f"genesis pins {entries[0].get('signers')!r}, but {signers.name} is "
                        f"{file_sha(signers)} (the signer file was replaced)")
    return problems


def fold(entries: list[dict]) -> dict:
    """The decisions in force: the latest decide/supersede per subject, minus revoked ones."""
    state: dict = {}
    for entry in entries[1:]:
        if entry["op"] in ("decide", "supersede"):
            state[entry["subject"]] = entry["value"]
        elif entry["op"] == "revoke":
            state.pop(entry["subject"], None)
    return dict(sorted(state.items()))


def dump_current(state: dict) -> str:
    return json.dumps(state, indent=2, ensure_ascii=False) + "\n"


def make_entry(seq: int, prev: str, fields: dict, key: Path) -> dict:
    entry = {**fields, "seq": seq, "prev": prev}   # fields can never override the chain position
    entry["hash"] = entry_hash(entry)
    entry["sig"] = sign(entry["hash"], key)
    return entry


PROPOSAL_KEYS = {"id", "op", "subject", "value", "binds", "text", "proposed_by"}


SETUP_PIN = "crossweft.json"


def model_files(root: Path) -> list[Path]:
    """The model files `crossweft check` reads: model_dir as crossweft.json names it."""
    config = json.loads((root / "crossweft.json").read_text(encoding="utf-8"))
    return sorted((root / config.get("model_dir", "seams/model")).glob("*.json"))


def model_guards(root: Path) -> dict:
    """Every join, set and pair of the crossweft model, by `join:<id>` / `set:<id>` / `pair:<id>`."""
    guards = {}
    for path in model_files(root):
        data = json.loads(path.read_text(encoding="utf-8"))
        for kind in ("joins", "sets", "pairs"):
            for item in data.get(kind, []):
                guards[f"{kind[:-1]}:{item['id']}"] = item
    return guards


def model_findings(root: Path) -> list[dict]:
    found = []
    for path in model_files(root):
        found += json.loads(path.read_text(encoding="utf-8")).get("findings", [])
    return found


def setup_sha(root: Path) -> str:
    """crossweft.json decides which model `check` reads; a decision signs it, so pointing
    model_dir at an edited copy of the model breaks the decision."""
    return guard_sha(json.loads((root / SETUP_PIN).read_text(encoding="utf-8")))


def guard_sha(guard: dict) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(guard, sort_keys=True, ensure_ascii=False,
                                                 separators=(",", ":")).encode("utf-8")).hexdigest()


def guard_pins(root: Path, binds: list[str]) -> dict:
    """The sha256 of each bound guard's definition. Signed with the decision, so editing the
    guard's paths or regex afterwards (pointing it at another line) breaks the decision."""
    guards = model_guards(root)
    missing = [b for b in binds if b not in guards]
    if missing:
        raise ValueError(f"binds name guards the model does not have: {missing}")
    pins = {b: guard_sha(guards[b]) for b in binds}
    if pins:
        pins[SETUP_PIN] = setup_sha(root)
    return pins


def in_force(entries: list[dict]) -> dict:
    """The entry that decides each subject now."""
    latest: dict = {}
    for entry in entries[1:]:
        if entry["op"] in ("decide", "supersede"):
            latest[entry["subject"]] = entry
        elif entry["op"] == "revoke":
            latest.pop(entry["subject"], None)
    return latest


def append(ledger: Path, entry: dict) -> None:
    with ledger.open("a", encoding="utf-8", newline="\n") as fh:   # open(newline=) is 3.9-safe
        fh.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")


def _paths(root: Path) -> tuple[Path, Path, Path, Path]:
    d = root / "decisions"
    return d / "ledger.jsonl", d / "allowed_signers", d / "current.json", d / "pending"


def cmd_propose(root: Path, args: argparse.Namespace) -> int:
    ledger, _, _, pending = _paths(root)
    entries = read_ledger(ledger)
    ident = f"D-{len(entries):04d}"
    pending.mkdir(exist_ok=True)
    try:
        value = json.loads(args.value)   # 30, ["a", "b"], true, null
    except ValueError:
        value = args.value               # plain text: fast, nightly
    guard_pins(root, args.bind or [])    # fail now, not at approval, on a guard that does not exist
    proposal = {"id": ident, "op": args.op, "subject": args.subject, "value": value,
                "binds": args.bind or [], "text": args.text, "proposed_by": args.by}
    (pending / f"{ident}.json").write_bytes(
        (json.dumps(proposal, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    print(f"proposed {ident}: {args.subject} = {value!r}. The owner approves it with "
          f"`python tools/ledger.py approve {ident} --key <her key>`; until then it is not in force.")
    return 0


def cmd_approve(root: Path, args: argparse.Namespace) -> int:
    ledger, signers, current, pending = _paths(root)
    entries = read_ledger(ledger)
    problems = check_chain(entries, signers)
    if problems:
        print("refused: the ledger is already broken:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    proposal_file = pending / f"{args.id}.json"
    proposal = json.loads(proposal_file.read_text(encoding="utf-8"))
    extra = set(proposal) - PROPOSAL_KEYS
    if extra or proposal.get("op") not in ("decide", "supersede", "revoke"):
        print(f"refused: the proposal has unexpected keys {sorted(extra)} or op {proposal.get('op')!r}; "
              "the owner signs only what she is shown", file=sys.stderr)
        return 1
    if proposal["id"] != f"D-{len(entries):04d}":
        print(f"refused: {proposal['id']} was proposed for another position; propose it again",
              file=sys.stderr)
        return 1
    pins = guard_pins(root, proposal.get("binds", []))
    before = fold(entries).get(proposal["subject"], "<not decided>")
    after = "<revoked>" if proposal["op"] == "revoke" else repr(proposal.get("value"))
    print(f"{proposal['id']} [{proposal['op']}] {proposal['subject']}: {before!r} -> {after}\n"
          f"  why: {proposal['text']}\n  guards (signed as they are now): {', '.join(pins) or '-'}\n"
          f"  proposed by: {proposal.get('proposed_by') or '-'}")
    entry = make_entry(len(entries), entries[-1]["hash"], {**proposal, "guards": pins}, Path(args.key))
    append(ledger, entry)
    current.write_bytes(dump_current(fold(entries + [entry])).encode("utf-8"))
    proposal_file.unlink()
    print(f"appended {entry['id']} ({entry['hash'][:19]}...); decisions/current.json rewritten")
    return 0


def build_demo(root: Path, key: Path) -> None:
    """The example ledger, signed with `key`: decisions/allowed_signers from its public half,
    a genesis entry pinning it, then every decision in decisions/demo.json with its guards
    signed as they are now. The repository carries no signatures; each reader builds their own."""
    ledger, signers, current, _ = _paths(root)
    pub = key.with_suffix(".pub").read_text(encoding="ascii").split()
    signers.write_bytes(f"{IDENTITY} namespaces=\"{NAMESPACE}\" {pub[0]} {pub[1]}\n".encode("ascii"))
    entries = [make_entry(0, GENESIS_PREV, {"id": "D-0000", "op": "genesis",
                                            "signers": file_sha(signers)}, key)]
    for spec in json.loads((root / "decisions" / "demo.json").read_text(encoding="utf-8")):
        fields = {**spec, "guards": guard_pins(root, spec.get("binds", []))}
        entries.append(make_entry(len(entries), entries[-1]["hash"], fields, key))
    ledger.write_bytes("".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n"
                               for e in entries).encode("utf-8"))
    current.write_bytes(dump_current(fold(entries)).encode("utf-8"))


def cmd_demo(root: Path, args: argparse.Namespace) -> int:
    build_demo(root, Path(args.key))
    print("wrote decisions/allowed_signers, decisions/ledger.jsonl and decisions/current.json")
    return 0


def cmd_fold(root: Path, _: argparse.Namespace) -> int:
    ledger, _, current, _ = _paths(root)
    current.write_bytes(dump_current(fold(read_ledger(ledger))).encode("utf-8"))
    print(f"wrote {current.relative_to(root).as_posix()}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("propose")
    p.add_argument("--op", choices=("decide", "supersede", "revoke"), default="decide")
    p.add_argument("--subject", required=True)
    p.add_argument("--value", default="null")
    p.add_argument("--bind", action="append")
    p.add_argument("--text", required=True)
    p.add_argument("--by", default=os.environ.get("USER") or os.environ.get("USERNAME") or "")
    a = sub.add_parser("approve")
    a.add_argument("id")
    a.add_argument("--key", required=True)
    sub.add_parser("fold")
    m = sub.add_parser("demo")
    m.add_argument("--key", required=True)
    args = parser.parse_args(argv)
    root = Path(args.root)
    return {"propose": cmd_propose, "approve": cmd_approve, "fold": cmd_fold,
            "demo": cmd_demo}[args.cmd](root, args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
