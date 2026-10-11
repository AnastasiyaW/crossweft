# Using crossweft with coding agents

Three moments matter, whatever the agent:

| Moment | Command | What the agent gets |
|---|---|---|
| session start | `crossweft hook <agent> session-start` | one paragraph: this repository declares its seams, how many, and the rules |
| after an edit | `crossweft hook <agent> post-edit` | for each seam the edited file sits on: the file on the **other** side to re-read, the guard that compares them, and -- if a guard already disagrees -- the disagreement |
| before "done" | `crossweft hook <agent> stop` | if `crossweft check --incremental` fails, the agent is sent back with the reasons |

The stop hook checks only what changed since the last commit at which the map
passed in this work tree (`check --incremental`), so a stop on a large
repository costs the changed guards, not the whole map; the first stop, an
upgrade of the tool, or a change to the map, the config or a joint plugin runs
the full check. Measured on a 1,140-file map with 13 joint kinds: full check
385 s, a stop after editing one source file 38 s.

The stop hook can request a follow-up while the agent makes progress (the set
of failing keys changes), at most four times in a row. It is not an
unconditional block: when it stops making progress, the agent may stop and the
user is told that the check still fails. There is no endless loop and no silent
pass. Set `"agent": {"stop": "warn"}` in
`crossweft.json` to only warn. While the model declares nothing yet
(`crossweft check` exits 2, "scanned nothing") the user is told so at every
stop, but the agent is not sent back: an adopted, not yet mapped repository
would otherwise cost an extra agent turn on every prompt.

The hooks do nothing in a repository without `crossweft.json`, so a global
install is safe. `crossweft hook` never exits 2 (Claude Code reads that as
"block"): an agent or event this crossweft does not know -- a hook
configuration newer than crossweft -- is skipped with exit 1, which Claude
Code shows as a non-blocking hook error (exit 0 would hide the note), and
an unreadable payload or a working directory that does not exist is reported
as "seams were NOT checked".

## What an agent runs

| When | Command |
|---|---|
| before editing a file another component depends on | `crossweft impact <path>` -- or the `impact` / `other_side` tools of [`crossweft mcp`](#mcp-any-mcp-client) |
| before finishing | the full `crossweft check`: exit 0 = pass, 1 = problems (each with a key), 2 = no verdict (unreadable config or model, nothing scanned) -- never a pass |
| before a commit, in a large repository | `crossweft check --changed HEAD` -- fast, but blind to drift committed earlier, so never the final check ([details](#large-repositories-check---changed)) |
| after changing one region of a `pair` | re-read every region, then `crossweft attest <id> --reason "..."`. Attest refuses when only some regions changed; `--other-side-unchanged` records that the unchanged ones were re-read and already agree -- it is a claim that lands in review, not an override |
| adding a seam to the map | `crossweft discover` (values typed into two languages); `crossweft import openapi\|proto <schema> --against <file>` when one side is a schema |
| adopting crossweft in a repository with existing drift | `crossweft baseline --owner ... --next-step ...`, once, after the map is written -- never to silence new drift |

Every command and flag: [cli.md](cli.md).

## The repository carries the harness

The agent loop should not depend on each developer installing something. `crossweft init`
(or `crossweft agents` in a repository that already has a map) commits the
harness into the repository itself, so every agent that opens it picks the
seams up as part of the architecture:

| File | Who reads it | What it does |
|---|---|---|
| `.claude/settings.json` | Claude Code | the three hooks, merged into whatever the file already holds |
| `AGENTS.md` (a marked block) | Codex, Copilot, Cursor, Claude Code | the working rules; an existing `CLAUDE.md`, `.claude/CLAUDE.md` or `CLAUDE.local.md` gets an `@AGENTS.md` import (in the first of them), because Claude Code reads those instead of AGENTS.md when any of them exists ([memory docs](https://code.claude.com/docs/en/memory#agents-md)) |
| `.claude/skills/crossweft/SKILL.md` | Claude Code | the procedures: fix a failing check, attest a pair, map a repository from scratch |

`crossweft agents` never overwrites text that is not its own: the AGENTS.md
block sits between markers, the hooks are merged (a hook group that also holds
someone else's hook is never changed -- crossweft's hook gets a group of its
own), and a skill file it did not generate is left alone with an error. Files
keep their line endings and are replaced atomically; a file that is not UTF-8
is an error, and nothing is written.

How the hooks start crossweft is declared once, as `agent.command` in
`crossweft.json` (default `crossweft`, after the [tagged Git install](../README.md#install)).
`crossweft agents --command 'python -m crossweft'` (or `init --command ...`)
writes the hooks with that launcher and records it there; a plain rerun uses
the recorded one.

`agents` also sets `"agent": {"harness": true}`, unless you set it to `false`
yourself. From then on `crossweft check` fails when a hook is not exactly
`<agent.command> hook claude-code <event>` (removed, or rewritten to `echo`,
`true` or anything else), when `"disableAllHooks": true` turns them all off,
when the AGENTS.md block is edited, duplicated or out of date, when a CLAUDE
file hides AGENTS.md without importing it, or when the skill differs from the
installed crossweft's: the harness is guarded like any other seam, and
upgrading crossweft tells you to re-run `crossweft agents`.

## Claude Code (tested)

Install the plugin:

```
/plugin marketplace add AnastasiyaW/crossweft
/plugin install crossweft@crossweft
```

It registers the three hooks (`SessionStart`, `PostToolUse` on
`Edit|Write|MultiEdit|NotebookEdit`, `Stop`) and a `crossweft` skill. The hooks
run the package bundled with the plugin through `bin/crossweft`, which finds
Python 3.9+ on `PATH`. No `pip install` is needed.

The plugin is for your own machine; the repository's harness (above) is for
everyone who works in it. When both are present the plugin's hooks stay quiet
in that repository, so the agent hears each message once -- but only when the
repository's hooks actually run crossweft there: the session started in the
directory that holds `crossweft.json` (Claude Code reads
`.claude/settings.json` from where the session started), the hooks match
`agent.command`, and `<agent.command> --version`, run in the hook shell (Git
Bash on Windows, `sh` elsewhere), answers `crossweft <version>` within 10
seconds. Anything else -- no `crossweft` on `PATH`, a launcher that is `echo`,
no shell to ask -- and the plugin does the checking itself.

## OpenAI Codex (supported skill plugin)

Install the same public repository as a Codex marketplace, then install its
`crossweft` plugin:

```
codex plugin marketplace add AnastasiyaW/crossweft
codex plugin add crossweft@crossweft
```

The Codex plugin loads the same `crossweft` skill that Claude Code uses. It
does not declare automatic Codex hooks. After a cross-component edit, the skill
has the agent run `crossweft impact <path>` and, before concluding the task,
`crossweft check`. This is deliberate: the supported local Codex plugin hook
example invokes an MCP tool, and Crossweft does not publish an unverified
third-party command-hook configuration. Install the Crossweft CLI using the
[tagged Git install](../README.md#install) before using those commands.
`crossweft agents` still writes
`AGENTS.md`, so Codex receives the repository-level working rules.

## Other agents (experimental)

The adapters below follow each agent's published hook reference as of
2026-09. They are covered by unit tests of the JSON shapes, not yet by
end-to-end runs in each agent -- reports and fixes are welcome.

**Gemini CLI** -- `settings.json`:

```json
{"hooks": {
  "AfterTool": [{"matcher": "write_file|replace",
                 "hooks": [{"type": "command", "command": "crossweft hook gemini post-edit"}]}],
  "AfterAgent": [{"hooks": [{"type": "command", "command": "crossweft hook gemini stop"}]}]
}}
```

**GitHub Copilot (CLI and coding agent)** -- `.github/hooks/crossweft.json`:

```json
{"version": 1, "hooks": {
  "postToolUse": [{"type": "command", "matcher": "edit|create",
                   "bash": "crossweft hook copilot post-edit"}],
  "agentStop": [{"type": "command", "bash": "crossweft hook copilot stop"}]
}}
```

**Cursor** -- `.cursor/hooks.json`. Cursor's `afterFileEdit` cannot send text
to the agent, so only the stop hook is useful: it sends the agent back with a
`followup_message`.

```json
{"version": 1, "hooks": {"stop": [{"command": "crossweft hook cursor stop", "loop_limit": 3}]}}
```

**Aider** -- no hook protocol; use the test command, which Aider feeds back to
the model when it fails: `aider --test-cmd "crossweft check" --auto-test`.

## MCP (any MCP client)

`crossweft mcp` is a read-only [Model Context Protocol](https://modelcontextprotocol.io)
server on stdio (protocol 2025-06-18, stdlib only). It lets an agent ask the
same questions the hooks answer, whenever it wants:

| Tool | Arguments | Returns |
|---|---|---|
| `check` | `paths` (optional) | status `ok` / `problems` / `error` (exit 0 / 1 / 2 of `crossweft check`), counts, and the problems; with `paths`, only those located there -- the status and counts stay those of the whole check |
| `impact` | `paths` | the seams through those files, the other side of each to re-read, and the guards that compare them |
| `show` | `id` | one entity, bare or `kind:id` as `check` prints it |
| `discover` | `limit` (default 20) | the top unmapped cross-language values, each with a suggested join |
| `other_side` | `path` | just the files to re-read after editing that file |

Paths are relative to the repository root. Every result carries a text
summary and `structuredContent` JSON. A call that could not produce a verdict
(no `crossweft.json`, an unreadable model, a path outside the repository, an
id that does not exist) answers `isError: true` with the reason; a bad or
unknown argument is a JSON-RPC error. Nothing is written to disk.

Claude Code -- `.mcp.json` in the repository (or `claude mcp add crossweft -- crossweft mcp`):

```json
{"mcpServers": {"crossweft": {"command": "crossweft", "args": ["mcp"]}}}
```

Cursor -- `.cursor/mcp.json`, same shape:

```json
{"mcpServers": {"crossweft": {"command": "crossweft", "args": ["mcp"]}}}
```

The server finds `crossweft.json` by walking up from the directory the client
starts it in; if your client starts servers elsewhere, pass the root:
`"args": ["--root", "/path/to/repo", "mcp"]`. For a vendored copy use
`"command": "python", "args": ["-m", "crossweft", "mcp"]`.

## Any agent: AGENTS.md

`crossweft agents` writes this block (between markers, with your `model_dir`)
for you. For an agent that reads another file (`GEMINI.md`, `.cursorrules`),
paste it there:

```markdown
## Component seams (crossweft)

This repository keeps a map of its components and of every place where two of
them must agree -- routes, headers, protocol versions, DTO fields, env vars,
pipe names, duplicated algorithms -- in `<model_dir>`, and `crossweft check`
verifies the map against the code. Agents write and maintain the map, and
the check makes sure it cannot drift from the code.

- Before editing a file another component depends on, run
  `crossweft impact <path>` (or the `impact` tool of `crossweft mcp`): it names
  the file on the other side and the guard that compares them. Change both
  sides.
- Before you finish, `crossweft check` must print `RESULT: PASS` (exit 0;
  1 = problems, 2 = no verdict, which is never a pass). `crossweft check
  --changed HEAD` is a fast pre-commit check, not the final one. For Claude
  Code the hooks in `.claude/settings.json` do this for you: after each edit
  they name the other side, and they send you back while a seam disagrees.
- When you add a component, a connection, or a value two places must share,
  put it on the map in the same change: a block, a link with anchors and
  `contract.enforcement`, and a guard that reads both sides for every
  hand-written seam (`crossweft discover` suggests guards; `crossweft import`
  derives them when one side is an OpenAPI or .proto schema). The
  `crossweft` skill has the full procedure, including mapping a repository
  from scratch.
- Never weaken a guard, widen a regex, add an `allow` entry or a finding, run
  `crossweft baseline` (it is for adoption only), or attest a pair with
  `--other-side-unchanged` without re-reading every region, just to make the
  check pass; each needs a concrete reason.
```

## CI

GitHub Action (annotates the diff):

```yaml
- uses: actions/checkout@v4
- uses: AnastasiyaW/crossweft@<release commit>  # v0.2.5; a tag can be moved, a commit cannot
```

pre-commit:

```yaml
repos:
  - repo: https://github.com/AnastasiyaW/crossweft
    rev: v0.2.5
    hooks:
      - id: crossweft-check
```

Anything else: `crossweft check --format github`, `--format json`,
`--format sarif` (SARIF 2.1.0, for code-scanning uploads), or the plain text
output; exit 0 = consistent, 1 = problems, 2 = the model or config
could not be read. A machine format always prints exactly one document on
stdout: an unreadable `crossweft.json` gives the normal json shape with
`"ok": false` and `"error"` set, or a failed SARIF run, still exit 2. Every
SARIF result carries `partialFingerprints` (`crossweftKey/v1`: the finding key,
or the rule plus the normalised error text), so an upload through the API
updates an alert instead of duplicating it. A refused flag combination
(`--changed` with `--format sarif`) is a usage error: the message goes to
stderr and stdout stays empty, so the upload fails rather than reading as
"no alerts".

### Large repositories: `check --changed`

`crossweft check --changed REV` runs only the guards (anchors, joins, sets,
pairs, the route scan, joint kinds) that read a path in the change set: every
file that differs between REV and the working tree (committed, staged or
not), every untracked file, and every git-ignored file or directory (git cannot
say whether an ignored file changed, so its guards always run). The
model-wide checks -- seams, status, provenance, evidence, coverage,
requirements, harness, generated docs, the lock's stale entries -- run in full.
Exit codes are those of `check`.

It is never mistaken for a full check: the last line reads
`SCANNED: ... (changed-only: N of M guards)`, `--format json` carries a
`changed_only` object, and `--format sarif` is refused (code scanning would
close the alerts of every skipped guard). It falls back to the full check, and
says why, when the change set cannot be trusted: REV does not resolve, git
fails, or `crossweft.json`, a model file, the pair lock or a joint plugin is in
it (a changed map can move any guard onto any file). A recorded finding whose
guard was skipped is listed as "not re-checked" -- neither excused nor stale.

The route scan is two guards. When any router file (or a `router_search` /
`ignore_routers` path) changed, every router is rescanned: a route dropped from
one router is `unserved` only if no other router still registers it. Client
literals are judged against the links' declared routes alone, so only the
changed client files are read; a router that stops serving a route an
unchanged client still calls is reported as `route:unserved`. On a synthetic
40-service repository (922 files, 1,000 routes, 800 client files; one run each
on a loaded Windows machine, so read the ratios, not the seconds): full check
289 s -> 182 s; `--changed` on a worker file 14 s -> 24 s (noise: the route
scan does not run); on a client file 96 s -> 23 s; on a router file
316 s -> 28 s.

What it does not see: drift committed before REV in files that did not change
since. That is why the pre-commit hook may use `--changed HEAD` (every earlier
commit passed it) but CI and releases run the full check, and why the agent
**stop hook keeps the full check**: an agent may commit during the session and
must not finish with a seam out of agreement anywhere. For a branch in CI,
`crossweft check --changed "$(git merge-base origin/main HEAD)"` is the narrow
form; the full check stays the release gate.

```yaml
      - id: crossweft-check
        args: [--changed, HEAD]   # pre-commit appends args to `crossweft check`
```
