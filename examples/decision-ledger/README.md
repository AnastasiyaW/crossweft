# Decision ledger: agreed settings a later chat cannot quietly change

In one session the owner and an agent agree on how a pipeline works: the sampler, the step
count, the LoRA set. A later chat, with none of that context, "improves" one of them. This
example makes the agreement checkable.

```
decisions/ledger.jsonl     append-only, one signed entry per decision
decisions/allowed_signers  the owner's public key (entry 0 pins its sha256)
decisions/current.json     the decisions in force: the fold of the ledger
pipeline/config.py         the code that must match them
seams/model/               joins and a set between current.json and config.py
validators/                validate_decision_ledger.py
tools/ledger.py            propose (agent) / approve (owner) / fold
```

## What stops what

| A later chat... | Caught by |
|---|---|
| changes `STEPS` in `config.py` only | `join:imagegen-steps` in `crossweft check` |
| changes `config.py` and `current.json` | the validator: `current.json` is not the fold of the ledger |
| appends a ledger entry signed with its own key | the validator: not signed by a key in `allowed_signers` |
| edits an old entry, even re-hashed and re-signed | the validator: the chain or the signature breaks |
| removes or reorders an entry (not the newest ones, see Limits) | the validator: `seq` and `prev` no longer line up |
| swaps `allowed_signers` for its own key | the validator: genesis pins the file's sha256 |
| deletes the guard a decision binds | the validator: the guard is gone from the model |
| points the guard's regex at another line (`OLD_STEPS = 28`, then `STEPS = 50`) | the validator: each decision signs the sha256 of its guards as they were |
| writes an expression instead of the value (`STEPS = 28 * 2`) | the join: its regex is anchored to the whole line |
| excuses the drift with a `findings` entry | the validator: a finding may not waive a guard a signed decision binds |
| points `model_dir` in `crossweft.json` at an edited copy of the model | the validator: each decision also signs `crossweft.json`, and reads the model `check` reads |

The repository carries no signatures. Build the example ledger with your own key
first: `python tools/ledger.py demo --key <your key>` writes `decisions/allowed_signers`
from its public half and signs the decisions in `decisions/demo.json` into
`decisions/ledger.jsonl` (both are git-ignored).

The legitimate path: the agent runs `tools/ledger.py propose ...`, which writes
`decisions/pending/<id>.json` and changes nothing in force. The owner runs `tools/ledger.py
approve <id> --key <her key>` in her own terminal; it prints the operation, the old and new
value, the reason and the guards, refuses a proposal with any other field, and signs the entry
together with the sha256 of each bound guard as it is now.
Then the code is changed to match, and `crossweft check` and `crossweft validators` are green.
Every case above is a test in `tests/test_decision_ledger.py` and in the validator's self-test.

## Limits

- **The owner's key must be one the agent cannot use.** An agent runs with the owner's account
  and can read every file, so a key file with or without a passphrase in an agent can be used by
  it (agents cache unlocked keys). Use a FIDO2 key that asks for a touch and a PIN:
  `ssh-keygen -t ed25519-sk -O verify-required`. The example and its tests use ordinary keys.
- **Rolling back to an older, valid state is not caught here.** Cutting the newest entries, or restoring an older ledger,
  `current.json` and code from git gives a consistent, correctly signed history. Pin the newest
  head outside the agent's reach, for example a signed tag the owner pushes to a repository whose
  tags cannot be moved.
- **Hooks only stop an honest agent.** A hook that refuses edits to `decisions/` keeps a drifting
  agent on the path above. It is not a security boundary: the signature is.
- **Only the literal assignment is read.** The guards check the line `STEPS = 28`; a later
  `STEPS += 22` or an indented `STEPS = 50` under an `if` in the same file is not seen. Keep the
  agreed values in a file that holds nothing else, review changes to it, or add a validator that
  parses it.
- **Any change to `crossweft.json` needs a new approval**, because the decisions sign it.
- **The check proves the code says what was agreed**, not that the agreed setting is good.
