# Command reference

Every command takes the global options `--root ROOT` (the repository root;
default: walk up from the current directory to `crossweft.json`), `--version`
and `-h`. `crossweft <command> -h` prints the same flags with an
example.

| Exit code | Meaning (for `check`, and wherever a command gives a verdict) |
|---|---|
| `0` | consistent / done |
| `1` | problems found; each is printed with its key |
| `2` | no verdict: the config or model could not be read or validated, a usage error, or nothing was scanned -- never a pass |

## `crossweft init`

Creates `crossweft.json`, an empty model in `seams/model/`, and the agent
harness (see `crossweft agents`).

| Flag | Meaning |
|---|---|
| `--example` | also write a tiny working seam: `crossweft-example/` (a Python and a TypeScript constant) and `seams/model/20-example.json`; refuses if `crossweft-example/` exists |
| `--language en\|ru` | language of the generated docs (default `en`) |
| `--no-agents` | do not install the agent harness |
| `--command LAUNCHER` | how the hooks start crossweft, recorded as `agent.command` (default `crossweft`) |

```bash
crossweft init --example && crossweft check
```

## `crossweft agents`

Writes or repairs the agent harness: the Claude Code hooks in
`.claude/settings.json`, the marked block in `AGENTS.md`, and the skill in
`.claude/skills/crossweft/`. Never overwrites text that is not its own. Details:
[agents.md](agents.md#the-repository-carries-the-harness).

| Flag | Meaning |
|---|---|
| `--command LAUNCHER` | how the hooks start crossweft (default: the recorded `agent.command`, else `crossweft`) |
| `--dry-run` | print what would change, write nothing |

```bash
crossweft agents --command 'python -m crossweft'   # a vendored copy
```

## `crossweft check`

Verifies every guard, anchor, route and finding in the model against the code.
The last lines are `RESULT: PASS|FAIL|ERROR` and `SCANNED: ...`.

| Flag | Meaning |
|---|---|
| `--format text\|json\|github\|sarif` | output: text (default), JSON, GitHub annotations, or SARIF 2.1.0 for code scanning (recorded findings arrive as suppressed results) |
| `--json` | same as `--format json` |
| `--changed REV` | run only the guards that read a file changed since REV; falls back to the full check when the map, lock, config or a joint plugin changed. For pre-commit, not for CI -- see [agents.md](agents.md#large-repositories-check---changed) |
| `--incremental` | `--changed` against the last commit at which the check passed in this work tree (kept in the git directory, with the paths that were uncommitted then, which rerun), or the full check when there is none or another crossweft version or build recorded it. A pass moves that commit forward. The stop hook runs this; CI and releases run plain `check` |

```bash
crossweft check --format github
crossweft check --changed HEAD
crossweft check --incremental
```

## `crossweft impact`

Lists the seams that run through the changed files, the file on the other side
of each to re-read, and the guard that compares them. Without paths it reads
the working tree against `HEAD` (staged, unstaged and untracked files).

| Flag | Meaning |
|---|---|
| `paths ...` | explicit files or directories instead of the git changes |
| `--base REF` | also count committed work since the merge-base with REF (not together with paths) |
| `--json` | machine-readable output |

```bash
crossweft impact server/main.go
crossweft impact --base origin/main
```

## `crossweft actions`

Prints, for each control of the model's `ui_actions`, every hop that takes it
(a dispatch table, a registry, a handler's route) and where. Exit `0` when
`check` would raise no `ui` or `anchor:ui:` problem for the selected entries, `1` when it would,
`2` when nothing could be traced (an invalid model, or no `ui_actions`). A click
smoke test can read `--json` to know what to press and which request to expect.

| Flag | Meaning |
|---|---|
| `id` | one `ui_actions` id (bare or `ui:<id>`) |
| `--json` | machine-readable output |

```bash
crossweft actions
crossweft actions --json ui:admin-buttons
```

## `crossweft fallbacks`

Lists every place in `fallbacks.include` where the code carries on after a
failure, with its status in the registry: `unregistered`, `proposed`,
`approved` or `rejected` (see [fallbacks.md](fallbacks.md)). Exit `0` when
`check` would raise no `fallback` problem, `1` when it would, `2` when nothing
could be scanned. `propose` registers sites for the owner's decision; `decide`
records the decision, signed when `fallbacks.signers` is set.

| Flag | Meaning |
|---|---|
| `propose <key or id>...` | register unregistered sites, or re-open entries whose code changed |
| `decide <id> approve\|reject` | the owner's decision on one entry |
| `--status` | list only `unregistered`, `proposed`, `approved` or `rejected` |
| `--json` | machine-readable output |
| `--md` | also write the map as Markdown to this repository-relative path |
| `--all` | `propose`: every unregistered site |
| `--behavior` | `propose`: what the code does when it fails here |
| `--reason` | why the code carries on (`propose`) / the reason for the decision (`decide`) |
| `--by` | who proposes / who decides |
| `--wrapper` | `propose`: the site's function is a fallback wrapper; its calls are listed with it |
| `--key` | `decide`: the owner's SSH key, when `fallbacks.signers` is set |

```bash
crossweft fallbacks --status proposed --md seams/fallbacks.md
crossweft fallbacks propose --all --reason "inventory before review" --by agent
crossweft fallbacks decide FB-0003 reject --reason "let the error surface" --by owner
```

## `crossweft show`

Prints one block, link, flow, data item, guard or finding and what it connects
to. Takes a bare id or the `kind:id` form that `check`, `impact` and the hooks
print, including a full problem key. Suggests close ids on a miss (exit 1).

```bash
crossweft show join:api-version
```

## `crossweft attest`

Records in the lock file that every region of a pair was re-read and agrees.

| Flag | Meaning |
|---|---|
| `ids ...` | pair ids |
| `--reason TEXT` | what you compared (required; lands in the lock file and in review) |
| `--all` | every changed or unattested pair |
| `--prune` | drop lock entries for pairs the model no longer declares |
| `--other-side-unchanged` | only some regions changed and you re-read the others: they are already equivalent. Without it, attest refuses when only one side changed |

```bash
crossweft attest price-rounding --reason "both round half-up to cents"
```

## `crossweft baseline`

Adoption only: records every current problem as open findings (one per
category) in a new model file, so the check passes while new drift still
fails. Refuses problems a finding cannot excuse (broken anchors, stale
exceptions) and never overwrites a file.

| Flag | Meaning |
|---|---|
| `--owner WHO` | who owns the recorded drift (required) |
| `--next-step TEXT` | what happens next (required) |
| `--file NAME` | model file to create (default `90-baseline.json`) |

```bash
crossweft baseline --owner platform --next-step "fix before 1.0"
```

## `crossweft discover`

Lists values typed into two languages that no guard reads yet (routes,
headers, API versions, URLs, event names, env vars), each with a ready-to-paste
guard whose regexes were tried on the files. With nothing to suggest it prints
a starter snippet.

| Flag | Meaning |
|---|---|
| `--limit N` | how many candidates to list (default 20) |
| `--json` | machine-readable output |

```bash
crossweft discover --limit 5
```

## `crossweft import`

When one side of a seam is a schema and the other is hand-written, prints
ready-to-paste `sets` and `joins` (fields, enum values, version, paths), each
tried on both files first. Exit 0 = guards printed, 1 = nothing matched,
2 = the schema could not be read. Details:
[model-reference.md](model-reference.md#importing-guards-from-a-schema).

| Argument / flag | Meaning |
|---|---|
| `openapi\|proto` | schema format (OpenAPI 3.x as JSON only) |
| `spec` | the schema file |
| `--against FILE` | the hand-written file on the other side (required) |
| `--link ID` | link id to put on every suggested guard |
| `--json` | machine-readable output |

```bash
crossweft import openapi api/openapi.json --against web/src/api.ts --link web-api
crossweft import proto proto/orders.proto --against worker/orders.py
```

## `crossweft render`

Writes the generated Markdown pages (Mermaid diagrams) and `viewer.html` into
`output_dir`. `check` fails while they are stale if
`check_rendered_docs` is set.

| Flag | Meaning |
|---|---|
| `--check` | only verify they are up to date (exit 1 if stale) |

```bash
crossweft render
```

## `crossweft validators`

Runs every `validate_*.py` and fails any validator that scanned nothing, lacks
a real self-test, or reports a finding. Details: [validators.md](validators.md).

| Flag | Meaning |
|---|---|
| `--dir DIR` | validators directory (default `validators.dir` in `crossweft.json`) |
| `--self-test` | prove the runner itself works |
| `--incremental` | reuse a self-test or normal run that passed last time when nothing it read, listed, checked or asked git has changed (kept in the git directory; see [validators.md](validators.md#incremental-runs-the-stop-hook)). For the stop hook; CI runs plain `validators` |

```bash
crossweft validators
crossweft validators --incremental
```

## `crossweft mcp`

A read-only MCP server on stdio with the tools `check`, `impact`, `show`,
`discover` and `other_side`. Details: [agents.md](agents.md#mcp-any-mcp-client).

```bash
claude mcp add crossweft -- crossweft mcp
```

## `crossweft hook`

The agent hook adapter: reads the agent's JSON on stdin and answers in that
agent's format. Never exits 2; an unknown agent or event is skipped with
exit 1. Configured for you by `crossweft agents`; see [agents.md](agents.md).

| Argument / flag | Meaning |
|---|---|
| `agent` | `claude-code`, `codex`, `gemini`, `copilot` or `cursor` |
| `event` | `session-start`, `post-edit` or `stop` |
| `--from-plugin` | called by the Claude Code plugin: stay quiet when the project's own hooks already run crossweft |

```bash
crossweft hook claude-code stop < payload.json
```

## `crossweft self-test`

Runs the engine and validator-runner self-tests on planted fixtures, plus the
self-tests of any joint plugins in the repository.

```bash
crossweft self-test
```
