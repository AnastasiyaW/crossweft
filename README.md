<p align="center"><img src="docs/assets/readme-header-1280x320.gif" width="100%" alt="Crossweft: guards the seams between languages. An AI agent can't say Done until both sides agree."></p>

<p align="center">
<a href="https://github.com/AnastasiyaW/crossweft/releases/latest"><img alt="latest release" src="https://img.shields.io/github/v/release/AnastasiyaW/crossweft?style=flat-square&labelColor=141210&color=EFE0B6&label=release"></a>
<a href="LICENSE"><img alt="license Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-D9A441?style=flat-square&labelColor=141210"></a>
<img alt="agents: Claude Code, Codex, MCP" src="https://img.shields.io/badge/agents-Claude%20Code%20%C2%B7%20Codex%20%C2%B7%20MCP-C4502F?style=flat-square&labelColor=141210">
<a href="https://happyin.work/crossweft/"><img alt="project page" src="https://img.shields.io/badge/site-happyin.work%2Fcrossweft-9BC27A?style=flat-square&labelColor=141210"></a>
</p>

# Crossweft

Crossweft fails your build when a value copied between languages changes on
one side only: an API version, a DTO field, an env var. It also tells your
coding agent which file on the other side to fix.

*Every hand-written seam needs a guard.*

A product written in more than one language is held together by seams: the
route the client calls and the server registers, a header, a protocol version,
the fields of a DTO, the env vars a service reads, a pipe name, the same
rounding rule in Go and in TypeScript. Local types, tests and linters can miss
these hand-maintained agreements. When no shared schema, generator or contract
test covers a seam, changing one side can leave the other side behind.

Coding agents also need this context: a passing local test does not tell them
which file in another component must change with it.

**crossweft** is a small, dependency-free (Python stdlib) checker that:

- keeps a **map** of your components and the links between them, verified
  against configured code anchors;
- makes every link declare **how its two sides are kept in agreement** -- and
  requires a mechanical **guard** that reads both sides for every link that is
  written by hand on both sides;
- tells a coding agent, right after it edits one side of a seam, **which file
  on the other side to re-read** -- and reports drift through hooks and a
  failing CLI/CI check. Hooks request repair; their retry limits and host
  boundaries are documented in [Agent integration](docs/agents.md).

## Words used below

| Word | Meaning |
|---|---|
| **seam** | a place where two components must agree on something written by hand on both sides: a route, a header, a version string, DTO fields, env vars, a duplicated algorithm |
| **block** | one component on the map (a service, a client, a worker, a config file) and the code directories it owns |
| **link** | a declared connection between two blocks; it carries one seam or a group of them |
| **enforcement** | a link's answer to "what keeps both sides equal?" -- a shared symbol, a generator, or nobody (`duplicated`, `convention`, `none`) |
| **guard** | a check that reads both sides of a link and fails when they disagree; required when nobody keeps them equal. Four kinds follow |
| **join** | guard: a value extracted by regex from each file must be identical |
| **set** | guard: the members extracted from each side (fields, enum values, env vars) must match |
| **pair** | guard: two marked regions of duplicated logic; changing one fails until someone re-reads the other and runs `crossweft attest` |
| **ui_actions** | guard: every UI control must reach a handler through the hand-written dispatch tables, and every handler must have a control |
| **anchor** | `{"path": ..., "find": ...}`: a literal that must exist in a file, so the map cannot claim code that is not there |
| **finding** | known drift recorded in the model with an owner and a next step; it excuses exactly the problems it names until they are fixed |

## What your agent sees

<p align="center"><img src="docs/assets/how-it-works-1280x420.png" width="100%" alt="What your agent sees: 01 session start, a short brief about the seam map; 02 after every edit, the other side and its guard; 03 before finishing, crossweft check runs and the hook won't let the agent finish while a seam disagrees."></p>

After Claude Code edits `server/main.go` in the [demo](examples/polyglot-shop):

```
crossweft: server/main.go is part of 3 cross-component seam(s). Keep both sides in agreement:
- link web-orders (web -> api, https; Orders API [duplicated]): you changed its to side.
  Re-read: web/src/api.ts. Guarded by: join:api-version, join:version-header,
  set:order-fields, set:web-statuses, pair:price-rounding.
- link worker-api-status (worker -> api, sql; order status values [convention]): you
  changed its to side. Re-read: worker/worker.py. Guarded by: set:worker-statuses.
- link env-api (env -> api, file; API environment [duplicated]): you changed its to side.
  Re-read: .env.example. Guarded by: set:env-api-vars.
Run `crossweft check` before you finish.
```

If it bumps the API version in Go and then tries to stop with the TypeScript
client still sending the old one, the stop hook sends it back:

```
crossweft check fails:
- join:api-version disagrees: web@web/src/api.ts:3='2026-09-01'; api@server/main.go:14='2026-10-01'
  [server/main.go:14] (key: join:api-version:b5dfb7e5)
A seam is out of agreement: bring the other side in line with the one you changed
(`crossweft impact <file>` names it).
Only if a drift is intentional and cannot be fixed in this change: add a finding to
"findings" in seams/model, e.g. {"id": "SM-0NN", ..., "detected_by":
["join:api-version:b5dfb7e5"], "owner": "...", "next_step": "..."} (problems marked
'must be fixed' cannot be excused that way).
Then finish.
```

The advice follows the kind of problem: a literal the map points at that moved
is "update the anchor or restore the code", not "fix the other side".

The key ends in a digest of which file holds which value, so a finding excuses
this disagreement only: if a value changes or moves to another file, the check
fails again.

## Install

```bash
python -m pip install "git+https://github.com/AnastasiyaW/crossweft.git@v0.3.0"
```

Requires Python 3.9+ and Git. This release is distributed from GitHub. The
install command pins the release tag on purpose. In CI, pin the release commit
instead (shown on the GitHub release; keep the tag in a comment), because a tag
can be moved and would change what CI runs. There are no Python
runtime dependencies.

In a repository, `crossweft init` installs the agent harness for everyone who
works there (see [Adopting it](#adopting-it)). For your own machine there is
also a Claude Code plugin (hooks + skill, no pip needed -- it runs the bundled
package with any Python 3.9+):

```
/plugin marketplace add AnastasiyaW/crossweft
/plugin install crossweft@crossweft
```

For Codex, install the CLI above and add the skill plugin:

```bash
codex plugin marketplace add AnastasiyaW/crossweft
codex plugin add crossweft@crossweft
```

The Codex skill runs `crossweft impact` and `crossweft check` explicitly;
automatic hooks are currently provided for Claude Code. See
[Agent integration](docs/agents.md) for behavior and limits.

Or vendor it: copy the `crossweft/` directory into your repository and run
`python -m crossweft`. No dependencies.

## Quick start

In an empty directory (a Git repository is not required for this step):

```bash
crossweft init --example
crossweft check         # RESULT: PASS
```

`--example` writes a Python constant and a TypeScript constant that must agree
(`crossweft-example/`), plus the two blocks, the link and the join that guard
them (`seams/model/20-example.json`), next to `crossweft.json`, an empty model
and the agent harness. It refuses to run if `crossweft-example/` already exists.

**Break it.** Change the date in `API_VERSION` in
`crossweft-example/api/version.py` only, and run `crossweft check` again:

```
[FAIL] joins: 1
   - join:example-api-version disagrees: example-web@crossweft-example/web/version.ts:2='2026-10-01'; example-api@crossweft-example/api/version.py:2='2026-10-02'
RESULT: FAIL (new=1 known=0 stale_findings=0)
```

It exits 1. `crossweft impact crossweft-example/api/version.py` names the file
on the other side (`crossweft-example/web/version.ts`) and the guard.

**Fix it.** Put the same value in both files; `crossweft check` passes again.

**Map your repository.** Run `crossweft init` in your repository (without
`--example`) and continue with [Adopting it](#adopting-it): your coding agent
writes the map, `crossweft check` keeps it honest. Then add the check to CI
(below). Every command and flag: [docs/cli.md](docs/cli.md).

## GitHub Action

Once the repository has a map (`crossweft.json` and `seams/model`), add the
check to CI. A seam that disagrees fails the job and is annotated on the line
of the diff:

```yaml
# .github/workflows/seams.yml
name: seams
on: [push, pull_request]
jobs:
  crossweft:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: AnastasiyaW/crossweft@<release commit>  # v0.3.0
```

Pin the release commit shown on the [release page](https://github.com/AnastasiyaW/crossweft/releases/latest):
release tags here are immutable, but a commit cannot move at all.

| Input | Default | What it does |
|---|---|---|
| `root` | `.` | Directory that holds `crossweft.json` |
| `validators` | `false` | Also run `crossweft validators` (needs `validators.dir` in `crossweft.json`) |
| `python-version` | `3.12` | Python used to run crossweft (3.9 or newer) |

## Five minutes on the demo

```bash
git clone --branch v0.3.0 --depth 1 https://github.com/AnastasiyaW/crossweft
cd crossweft
python -m pip install -e .
cd examples/polyglot-shop && crossweft check
```

The demo is a Go API, a TypeScript client, a Python worker and an
`.env.example`. Break any seam and watch the right guard catch it:

| Change | Caught by |
|---|---|
| bump `API_VERSION` in `web/src/api.ts` only | `join:api-version` |
| add a JSON field to the Go `Order` struct only | `set:order-fields` |
| add a status to the Python worker only | `set:worker-statuses` |
| read a new env var in the worker, not in `.env.example` | `set:env-worker-vars` |
| call `/v1/refunds` from the client | `route:client-unexplained` |
| rename a button's `data-action` in `web/public/index.html` only | `ui:admin-buttons:dead` (and `:orphan` for the old name) |
| drop a handler from the `ACTIONS` table in `web/public/app.js` | `ui:admin-buttons:dead` |
| change the rounding in `server/pricing.go` | `pair:price-rounding` -- until someone re-reads the TypeScript twin and runs `crossweft attest` |

Every one of these is a test in [`tests/test_crossweft.py`](tests/test_crossweft.py)
and [`tests/test_ui_actions.py`](tests/test_ui_actions.py). `crossweft actions`
prints the chain of every button, and
[`smoke/click_smoke.py`](examples/polyglot-shop/smoke/click_smoke.py) presses each
one in Chromium and waits for its request.
`crossweft render` turns the map into Markdown pages with Mermaid diagrams and
an interactive [`viewer.html`](examples/polyglot-shop/seams/viewer.html).

[`examples/decision-ledger`](examples/decision-ledger) keeps agreed settings in a
signed, append-only ledger: changing the code, an old entry, the signer file or
the bound guard fails `check`.

## The model in one screen

```json
{"blocks": [
  {"id": "web", "name": "Web client", "kind": "site", "lane": "browser", "status": "current",
   "summary": "TypeScript SPA", "code": ["web/"]},
  {"id": "api", "name": "Orders API", "kind": "service", "lane": "backend", "status": "current",
   "summary": "Go HTTP API", "code": ["server/"], "route_server": true}],
 "links": [
  {"id": "web-orders", "from": "web", "to": "api", "transport": "https", "status": "current",
   "summary": "Reads and places orders.",
   "contract": {"name": "Orders API", "enforcement": "duplicated"},
   "identifiers": {"route": ["GET /v1/orders/{id}", "POST /v1/orders"]},
   "from_anchors": [{"path": "web/src/api.ts", "find": "export async function getOrder"}],
   "to_anchors": [{"path": "server/main.go", "find": "r.Route(\"/v1\""}]}],
 "joins": [
  {"id": "api-version", "name": "API version", "link": "web-orders", "points": [
    {"path": "web/src/api.ts", "side": "web", "regex": "API_VERSION = \"([^\"]+)\""},
    {"path": "server/main.go", "side": "api", "regex": "APIVersion = \"([^\"]+)\""}]}]}
```

**`contract.enforcement`** is the heart of it:

| Enforcement | Who keeps both sides equal | What crossweft requires |
|---|---|---|
| `shared-code`, `generated`, `schema-tests` | one owner: a shared symbol, a generator, shared test vectors | `defined_in` must name the owner |
| `duplicated`, `convention`, `none` | nobody -- it is written by hand on both sides | a **guard** that reads a file on each side |

Guards:

- **join** -- a value extracted by regex from every point must be equal (every
  occurrence, so a second drifted definition on one side is caught too);
- **set** -- members extracted from each side must match (DTO fields, enum
  values, env vars, file lists);
- **pair** -- for duplicated logic that has no value to compare: the regions
  between `crossweft:begin <id>` / `crossweft:end <id>` are fingerprinted; a
  change to one fails until someone re-reads the other and runs
  `crossweft attest <id> --reason "..."`. The reason lands in the lock file and
  in review;
- **ui_actions** -- follows each button's `data-action` or key binding through
  the dispatch tables to its handler (and, with `"routes": true`, to a declared
  route); dead, orphan, unrouted and duplicate bindings fail, and
  `crossweft actions` prints the chains.

Around the guards, `crossweft check` also verifies that every claim about the
code is **anchored** to a literal that still exists, that server routes and
client route literals match the links (Go chi, gin, echo, Express/Fastify,
FastAPI and Flask natively; any other router through a regex),
that data leaves a block only if it created or received it, that current links
do not depend on legacy blocks, and that every source area is on the map. Known
drift is recorded as a **finding** with an owner and a next step; when the
problem disappears, the check demands the finding be closed. A retired route
the server still answers on purpose is mapped on a legacy link with `gone`
(`{"status": 410}`); a client that still calls it fails.

Full reference: [docs/model-reference.md](docs/model-reference.md).

## Adopting it

```bash
crossweft init          # crossweft.json, an empty model, and the agent harness
```

(If you tried `crossweft init --example` here, delete `crossweft-example/` and
`seams/model/20-example.json` before mapping your own code.) If
`crossweft discover` finds nothing to suggest in your repository, it prints the
example's shape with placeholders to fill in.

Then ask your coding agent to map the repository. The map is written by agents,
not by hand: the `crossweft` skill that `init` installs walks the agent through
it (blocks, links with anchors on both ends, honest enforcement, a guard for
every hand-written seam), and `crossweft check` drives the work:

- `meta.coverage.roots` makes every source directory that is on no block a
  problem, so the agent cannot leave half the system off the map;
- every claim is anchored to a literal in a file, so the agent cannot invent a
  connection -- an anchor that does not exist fails the check;
- `crossweft discover` hands the agent values already typed into two languages
  (routes, headers, API versions, URLs, event names, env vars), each with a
  guard whose regexes were already tried against the files. Review the suggested
  scope before adding a guard; discovery does not prove semantic equivalence.
- `crossweft import openapi|proto <schema> --against <file>` does the same when
  one side of a seam is an OpenAPI (JSON) or .proto schema and the other is
  hand-written: fields, enum values, version and paths, each guard already tried.

A person reviews what only a person can: whether each link's enforcement is
honest, and the open findings.

Adopting it in a repository that already has drift: once the map is written,
`crossweft baseline --owner <who> --next-step "<what next>"` records every
current problem as open findings (one per category), so the check goes green
while new drift still fails, and each finding fails again as stale once its
problems are fixed. Broken anchors and stale exceptions are refused: a finding
cannot excuse a map that describes the code wrongly.

`init` also writes the **agent harness** into the repository -- hooks in
`.claude/settings.json`, the rules in `AGENTS.md`, the skill in
`.claude/skills/` -- so every agent that opens the repository picks the seams up
as part of the architecture once you commit it. Each developer or CI runner
still needs the CLI available (`crossweft agents` adds or repairs the harness
later). `check` fails if the harness goes
missing. Details: [docs/agents.md](docs/agents.md).

Then put it where the rest of the work happens:

- **CI**: `uses: AnastasiyaW/crossweft@<release commit> # v0.3.0` (annotates the
  diff; pin the commit like any action, a tag can be moved), the
  `crossweft-check` pre-commit hook, or `crossweft check --format github|json|sarif`
  (SARIF 2.1.0 for GitHub code scanning; recorded findings arrive as suppressed
  results). In a large repository, `crossweft check --changed HEAD` (pre-commit)
  runs only the guards that read a changed file -- see [docs/agents.md](docs/agents.md#large-repositories-check---changed).
- **Agents over MCP**: `crossweft mcp` serves `check`, `impact`, `show`, `discover`
  and `other_side` as read-only tools -- see [docs/agents.md](docs/agents.md#mcp-any-mcp-client).
- **Exit codes** (`check`, and the action and pre-commit hook built on it):
  0 = consistent, 1 = problems, 2 = no verdict (the config or model could not
  be read or validated, or nothing was scanned) -- treat 2 as a failure, never
  as a pass.
- **Rules that are not A-equals-B** ("this constant has one source"): small
  validators run by `crossweft validators`, which fails any validator that
  scanned nothing, lacks a real self-test, or reports a finding -- see
  [docs/validators.md](docs/validators.md). `crossweft validators --incremental`
  reuses a passed run while nothing it read changed (for a stop hook; CI runs
  the plain suite), and `validators.exclude` names a `validate_*.py` that is
  not a suite validator, with a reason.
- **Fallbacks**: with `fallbacks` in `crossweft.json`, every place the code
  carries on after a failure is checked against `seams/fallbacks.json`, the
  owner's decisions; agents `crossweft fallbacks propose`, the owner
  `decide`s -- see [docs/fallbacks.md](docs/fallbacks.md).
- **Hazards** (opt-in, `"hazards": {"include": [...]}`): script lines that
  run without an error and still break what another program reads -- see
  [docs/model-reference.md](docs/model-reference.md).

This repository guards its own seams with crossweft -- the version in
`pyproject.toml`, the package and the plugin manifest; the hook events the
plugin calls; the commands this README mentions. See [`seams/`](seams/).

## How it compares

Each piece has prior art; the combination, and the rule that every
hand-written seam must declare itself and carry a guard, is what crossweft adds.

| Tool | What it does | Difference |
|---|---|---|
| Google `LINT.IfChange` / [ifttt-lint](https://github.com/ebrevdo/ifttt-lint), [if-changed](https://github.com/mathematic-inc/if-changed) | "if this block changes, that file must change too" | checks that the diff touched the other file; a join compares the values, on every run |
| [clevis](https://github.com/jesse-c/clevis) | value equality across files | no sets, no regions, no map |
| [Pact](https://docs.pact.io/) | executable consumer/provider contracts for HTTP and messages | Crossweft compares declared source facts; it does not replace behavioural contract tests |
| [buf breaking](https://github.com/bufbuild/buf), [oasdiff](https://www.oasdiff.com/) | compatibility checks for Protobuf and OpenAPI schemas | prefer schema checks where a schema exists; Crossweft also covers hand-maintained facts outside a schema |
| [ArchUnit](https://github.com/TNG/ArchUnit), [dependency-cruiser](https://github.com/sverweij/dependency-cruiser), [import-linter](https://github.com/seddonym/import-linter) | architecture rules inside one language | crossweft works across languages, processes and config files |
| [Structurizr](https://github.com/structurizr/structurizr), [LikeC4](https://github.com/likec4/likec4), [Backstage](https://backstage.io/docs/features/software-catalog/well-known-relations) | architecture models and software catalogs | Crossweft focuses on explicit source anchors and guards on declared connections |
| [fiberplane/drift](https://github.com/fiberplane/drift), Swimm | docs anchored to code | a hash fails on any edit; an anchor fails only when the claimed fact disappears |
| [codegraph](https://github.com/colbymchenry/codegraph) | code graph and change impact for agents | call graphs, not contracts; crossweft is complementary |
| [GitNexus](https://github.com/abhigyanpatwari/GitNexus) | code knowledge graph and change impact for agents (MCP); repository groups extract API contracts into a registry and match them across repositories, and `shape_check` compares response shapes with consumers' property accesses | contracts are inferred from the code graph; crossweft guards declared seams of any kind (env vars, headers, constants, duplicated logic) and fails the check when two sides disagree; the two combine |
| [archagent](https://github.com/BenedatLLC/archagent) | architecture invariants written in Markdown, compiled into import-linter, dependency-cruiser and ast-grep configs; an LLM only proposes | dependency and structure rules inside Python and JS/TS; crossweft compares values and members across languages |
| [Erode](https://github.com/erode-app/erode) | compares PR or local diffs with a LikeC4 or Structurizr model, using an LLM, to surface undeclared dependencies and structural changes | an LLM reviews dependencies against a model; crossweft's guards are deterministic and compare the values both sides hold |

## Limitations

- Guards use regular expressions, not a full language parser. Missing required
  matches fail, but an overly broad pattern or incomplete scope can still miss
  a real disagreement. Review the extracted values and use behavioural tests for
  runtime semantics.
- Agents write the model and the check keeps it honest, but whether a link's
  enforcement is really `shared-code` or just two copies is still a judgment a
  person should review.
- Route scanning understands Go chi, Express/Fastify, FastAPI, Flask, gin and echo
  natively, reading one file at a time: a prefix computed at runtime or a router
  handed in from another file is reported (or declared in `receivers`), not
  followed. Other routers need a regex.
- `crossweft import` reads OpenAPI 3.x as JSON only (the standard library has
  no YAML parser) and `.proto` files; it prints guards for you to review and
  never writes the model.
- `check --changed REV` cannot see drift committed before REV in files that
  did not change since. The agent's stop hook runs `check --incremental`
  (changed-only against the last passing commit, full when there is none); CI
  and releases run the full check.
- Claude Code has command hooks and a skill. Codex has a skill plugin that runs
  checks explicitly; it does not install automatic hooks. Other agent adapters
  are experimental and tested for output shape, not end to end.

## Status

`0.3.0`, alpha. The engine grew inside a commercial product that spans several
languages and processes, where one-sided changes kept reaching installs; this
is its extraction. Built by [HappyIn](https://happyin.ai).

## Tell us what broke

Crossweft has one user so far, so every report changes what we build next. A
false alarm, a seam it missed, a message your agent could not act on, a
language or framework you need: [open an issue](https://github.com/AnastasiyaW/crossweft/issues/new/choose)
or write to **crossweft@happyin.app**.

If you are a coding agent using Crossweft: when a check misfires, a message is
unclear or a seam kind is missing, draft a short report for your user (the
output of `crossweft --version`, the command, what you expected, what happened, no private
code) and let them decide whether to file it with the "Agent report" issue form.

Set `"feedback"` in `crossweft.json` to your own channel, and a failing check
or a seam hook message tells the agent where to report a problem with crossweft.

## License

[Apache License 2.0](LICENSE). Use it, change it and ship it, commercially or not.

Help adopting crossweft across a large codebase, custom scanners or training
for your team: **crossweft@happyin.app**.
