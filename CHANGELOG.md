# Changelog

## 0.2.4 (2026-10-11)

- The repository moved to `github.com/AnastasiyaW/crossweft`; GitHub redirects
  the old address. Install commands, plugin manifests, the marketplace entry
  and the docs name the new one.
- Feedback: issue forms for a problem, an idea and an agent report; the skill
  tells an agent, when crossweft itself gets something wrong, to draft a short
  report for its user (no private code) and let the user decide whether to
  send it; the README says where to write.
- README: header and how-it-works images, a release badge that follows the
  latest release, and a link to the project page.
- `action.yml`: a Marketplace-ready name (`Crossweft seam check`) and colour.

## 0.2.3 (2026-10-11)

- `validators.exclude` in `crossweft.json`: `{"validate_x.py": "<reason>"}` for a
  `validate_*.py` that is not a suite validator, such as a commit-time gate that
  judges one staged change. It is not run, and the report lists it with its
  reason. An entry that names no file, has no reason, or is also given a
  timeout is an error; excluding every file still fails with "nothing ran".

## 0.2.2 (2026-10-11)

- `check --incremental`: `--changed` against the last commit at which the check
  passed in this work tree, plus the paths that were uncommitted then (they
  rerun, so reverting an uncommitted file cannot bring back unchecked bytes);
  the full check when there is no such commit, another crossweft version or
  build recorded it (the record keeps a digest of the engine code), or the map,
  config or a joint plugin changed. HEAD and the uncommitted paths are read
  before the check runs, so a commit landing meanwhile is never recorded. A pass moves the
  commit forward. The record lives in the git directory (per worktree, never
  committed). Exclusive with `--changed`; refused with `--format sarif`.
- The stop hook runs `check --incremental`. Measured on a 1,140-file map with
  13 joint kinds: full check 385 s; a stop after editing one source file 38 s.
- `joints.sources` in `crossweft.json`: the plugin files and the helpers they
  import, for a `joints.dir` that also holds unrelated code. `--changed` then
  falls back to the full check only when one of those files changes (before:
  any change under `joints.dir`). A plugin that imports a helper from its
  directory, or reads a file from it while imported or checked, that neither
  `sources` nor the kind's inputs cover is an error
  (`joints:unlisted-read:<kind>:<path>`), so the list cannot go stale silently.
  Globs match the way `--changed` matches them, part by part.

## 0.2.1 (2026-10-10)

- New transform `go-http-status`: a Go `net/http` status constant name
  (`StatusTooManyRequests`) becomes its number (`429`), so the statuses a Go
  handler writes can be compared with the numeric statuses a client in another
  language accepts. The table is Go's `net/http/status.go`; an unknown name stays
  as it is and never matches a number, so a guard reading it fails instead of
  passing.
- A join point takes `within` (one capture group), like a set side: the point
  reads only those regions, e.g. the default block of a file that also documents
  an alternative value. Every match inside the regions still counts, so a drifted
  copy inside cannot hide. The regex runs on the whole file (not on a slice)
  and only matches lying wholly inside a region count, so `^`, `$`,
  lookbehind and lookahead see the real text; a
  `within` that finds no region fails as `anchor:join:<id>:within:<path>`.
- `engine.Checker.route_registrations(text, function_prefixes, unscannable=None)`:
  the go-chi route enumerator `check` uses, as a side-effect-free classmethod,
  so a joint-kind plugin that needs the server's routes reuses it instead of
  keeping a second scanner. Registrations it cannot name go to the optional
  callback.
- Docs: install by the release commit in CI (tag in a comment). A tag can be
  moved and would change what CI runs.

## 0.2.0 (2026-10-08)

- `crossweft impact` on files that are on no block says so and how to map them,
  instead of the generic "fix the other sides" advice; without paths outside a
  git work tree it exits 2 with one sentence instead of git's `--no-index` usage.
- `check --format sarif`: the `crossweft/model` and `crossweft/render` results
  now carry `partialFingerprints` too (rule plus normalised error text; one
  key for stale generated docs), so API uploads do not duplicate alerts.
  `check --format json` (or `--json`) with an unreadable `crossweft.json`
  prints the normal json document with `"ok": false` and exit 2 instead of
  plain text; `--changed` with `--format sarif` is still refused, now on
  stderr with an empty stdout.
- Every subcommand's `--help` now has a description and an example, and every
  flag a help line. New [docs/cli.md](docs/cli.md) documents every command and
  flag; this repository's own check fails when a command or flag is missing
  from the docs or documented without existing.
- The AGENTS.md block and the `crossweft` skill now cover the exit codes
  (2 is never a pass), `check --changed` as a pre-commit check only, the MCP
  tools, `import`, `baseline` as adoption-only, and what
  `attest --other-side-unchanged` claims. After upgrading, `crossweft check`
  reports the harness as out of date until you re-run `crossweft agents`.
- README: a quick start (init --example, break, fix, map, CI), the exit codes,
  and archagent, GitNexus contracts and Erode in the comparison.
- A join point's `side` must be non-empty, and a `side` that is a block id must
  name a block that holds the point's file (it, a part of it or a block
  containing it); otherwise a schema error names the owning block(s). Free
  labels stay valid -- `side` never decided which end a file is on.
- `check --changed` no longer rescans every route file when one changed: a
  changed client file reads only the changed client files, a changed router
  rescans the routers (every one, so `route:unserved` stays exact) and no
  client file. Path containment checks list each directory once instead of
  resolving every file. Synthetic 40-service repo: `--changed` on a client
  file 96 s -> 23 s, on a router 316 s -> 28 s, full check 289 s -> 182 s.
- `crossweft mcp`: a read-only MCP server on stdio (stdlib only) with the tools
  `check`, `impact`, `show`, `discover` and `other_side`; a call without a verdict
  is an `isError` result, a bad argument a JSON-RPC error.
- README opens with a plain one-sentence description and a glossary of the ten
  terms the rest of it uses.
- `crossweft init --example`: also writes a tiny working seam (a Python and a
  TypeScript constant, two blocks, a link, a join) so `crossweft check` passes
  at once, and fails naming the other file when one value changes. Refuses if
  `crossweft-example/` already exists; nothing is written then.
- `crossweft discover` with nothing to suggest prints a ready-to-edit
  blocks + link + join snippet instead of asking to confirm candidates.
- The model `init` writes points at the field reference by https URL instead of
  a `docs/` path that does not exist in your repository.
- A join point whose regex matches nothing is reported under `joins` (it was
  `anchors`) and shows the regex as written, not JSON-escaped. Its key is
  unchanged (`anchor:join:<id>:<path>`), so findings that name it still apply.
- A current link missing `from_anchors`/`to_anchors` now shows the expected
  shape, e.g. `"from_anchors": [{"path": "<file>", "find": "<text in that file>"}]`.
- `crossweft import openapi|proto <schema> --against <file> [--link <id>]`: prints
  ready-to-paste `sets`/`joins` for a seam whose one side is an OpenAPI 3.x (JSON
  only) or .proto schema; every regex is tried on both files first, and a side
  that matched nothing is marked and left out. Exit 0 / 1 (nothing matched) / 2
  (schema unreadable).
- Native route scanners `express` (with Fastify), `fastapi`, `flask`, `gin` and
  `echo`: groups, mounted/included routers and blueprints with literal prefixes
  are followed in the file; parameters are reported as `{name}`; a non-literal
  path, method or prefix, or a router handed in from elsewhere, is
  `route:unscannable:...` (or declared in the new `receivers`); a native router
  file with nothing scanned is `route:scan-empty:<file>`; `router_search` now
  reports unscanned routers of every supported kind, not only chi.
- `check --changed REV`: runs only the guards that read a file changed since
  REV (plus untracked and git-ignored files) and says so
  (`SCANNED ... (changed-only: N of M guards)`); a changed map, lock, config or
  joint plugin, or an unreadable change set, runs the full check instead. A
  finding whose guard was skipped is reported as not re-checked, never stale.

## 0.1.0 -- first public release

- License: Apache-2.0.
- Public name: Crossweft (`crossweft` package, command and plugin).
- `check --format sarif`: SARIF 2.1.0 for code scanning. Each problem carries
  its key as a partial fingerprint; recorded findings are suppressed results;
  an unreadable model is a failed invocation, not an empty run. Exit codes are
  those of `check`.
- `crossweft baseline --owner --next-step`: adopt in a repository with existing
  drift by recording every current problem as open findings (one per
  category). New drift still fails; each finding goes stale once fixed.
  Problems a finding cannot excuse are refused; an existing file is never
  overwritten.
- `attest` refuses when only some regions of a pair changed since the last
  attestation (the twin was not touched); `--other-side-unchanged` states that
  the twin was re-read and is already equivalent.
- A model that fails schema validation exits 2 with `RESULT: ERROR` (it was
  exit 1 with `RESULT: FAIL (new=0 ...)`), as documented: nothing was checked.
- The stop hook's advice follows the kind of problem (value drift, moved
  anchor, unmapped route, incomplete map, unloadable model) and carries a
  ready-to-fill finding with the problem key.
- `show` accepts the `kind:id` form that check, impact and hooks print
  (including a problem key) and suggests close ids on a miss; `check --help`
  lists the exit codes.
- Input integrity: reject duplicate JSON keys, missing set inputs, ambiguous
  paths and transforms that collapse distinct members.
- Findings identify the exact disagreement, so accepting known drift cannot
  silently accept a later different disagreement in the same guard.
- Go chi scanning covers `Method`/`MethodFunc`, raw strings and normalized
  paths; a two-sided guard must read two distinct component ends.
- Validator timeouts stop the subprocess tree. Hook input is UTF-8; plugin
  launching does not rely on the caller's `PYTHONPATH` layout.
- Agent integration preserves unrelated hooks and reports an unrunnable or
  incomplete harness instead of treating it as successful enforcement.

- Model: blocks, links, flows, data, findings; every link declares
  `contract.enforcement`; hand-written seams need a guard that reads both sides.
- Guards: `join` (every occurrence compared), `set`, and `pair` (fingerprinted
  regions re-attested with a written reason).
- Value transforms for joins and sets: `unescape-c`, `lower`, `strip`,
  `csv-words`, `basename`.
- Checks: anchors, routes (Go chi and regex scanners), data provenance, status,
  coverage, findings that go stale when fixed; paths may never leave the
  repository.
- `crossweft impact`, `crossweft discover`, `crossweft render` (Markdown +
  Mermaid + interactive viewer, English and Russian).
- Agent hooks: Claude Code plugin (tested); Codex, Gemini CLI, Copilot, Cursor
  adapters (experimental).
- `crossweft agents` (run by `init`): the repository carries its own agent
  harness -- Claude Code hooks, an AGENTS.md block, the skill -- and `check`
  fails when it goes missing or out of date. The skill includes mapping a
  repository from scratch: agents write the map, the check keeps it honest.
- `crossweft validators`: a meta-runner that fails any validator that scanned
  nothing or lacks a real self-test.
- GitHub Action, pre-commit hooks, `--format github|json`.

### Joint-kind plugins

- A repository can add its own kinds of joint as plugins (`joints.dir` in
  `crossweft.json`, one `joints_<kind>.py` per kind): `check` runs them,
  `render` writes their pages and generated files, `impact` names the kinds a
  change feeds. `meta.joint_kinds` pins the kinds, so deleting a model file or
  a plugin fails the check instead of turning a gate off. A plugin that fails
  to import, crashes, returns a malformed result or compares nothing is a
  fatal problem, never a pass.
- Hardened after independent review: `sys.exit()` or any non-Ctrl-C
  `BaseException` in a plugin (import, validate, check, render, generated,
  impact, self_test) is a loud error, never an exit 0; generated paths must be
  normalised and may not touch `crossweft.json`, `model_dir`, `output_dir`, the
  lock file, `joints.dir` or the plugin itself (case-folded); modules a plugin's
  import added from its own directory are dropped afterwards, so they cannot
  shadow a host module; `crossweft self-test` runs each plugin's `self_test()`
  and says `joint plugins: none configured` when there are none; wrong-typed
  `problems`/`files`/`items`/`other` and a crash in `render_markdown` or
  `generated` are named errors, and a plugin's own problem with 0 items is
  reported as itself.
- `check` prints every failing problem, including one outside the listed
  categories (before, it was counted in the result but not shown).

### Hardened before release (independent review, 2026-09-30)

Each item below was reproduced first and has a test that failed before the fix.

- Keys: join and pair problems end in a digest of the disagreement
  (`join:api-version:b5dfb7e5`), so a finding excuses only that disagreement
  and a new one fails again.
- Silent passes closed: a repeated JSON key in config, model or lock file is an
  error; every listed set file and glob must yield a file, and a literal path
  containing `[` is read as that file; two files a transform folds into one
  member fail; a guard counts as reading both sides only with files of two
  different ends; go-chi `Method`/`MethodFunc`, raw strings and non-literal
  paths (`route:unscannable`) are seen; directories starting with `_` are
  covered.
- Model paths have one spelling (no `\`, `//`, `./`; wrong case fails), so
  `impact` matches what `check` accepts.
- A regex group that does not take part is no match, not a crash; `render`
  touches only files it generated; a non-UTF-8 file there is an error.
- "Scanned nothing" is an error in every output (`RESULT: ERROR`, JSON
  `ok: false`, a GitHub annotation); at Stop an empty model warns the user
  instead of sending the agent back.
- Route checks index routes by normalised path (2,000 routes: 29 s to under 1 s).
- Hooks: stdin is UTF-8; `crossweft hook` never exits 2, and a hook this
  version does not know is skipped with exit 1 (visible, not blocking); the
  plugin stays quiet only where the project's own hooks actually run; the
  launcher ignores `PYTHONPATH` and a `crossweft/` in the working directory.
- `crossweft agents` is idempotent with any `--command`, leaves other tools'
  hooks and shared groups alone, keeps line endings, indentation and non-ASCII
  text, writes atomically, respects `agent.harness: false`, and adds the
  `@AGENTS.md` import to `CLAUDE.local.md` too. `check` also fails on
  `disableAllHooks`, on a duplicate AGENTS.md block and on a `CLAUDE.local.md`
  that hides AGENTS.md.
- Validators: a timeout or Ctrl-C kills the whole process tree; the report is
  ASCII-safe. `discover --limit` must be at least 1; the Action passes `root`
  through the environment.
