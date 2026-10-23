# Model reference

The map lives in `model_dir` (default `seams/model/`) as any number of `*.json`
files, merged in file-name order. Exactly one file carries `meta`. Each file may
hold any of the entity lists below. Unknown keys and unknown fields are errors:
a typo never loads silently.

All paths are relative to the repository root. An absolute path, a path with
`..`, or a symlink that resolves outside the repository is refused. A path is
written exactly as git names the file -- `/` separators, no `./` or `//`, and
the file's own letter case (checked on case-insensitive file systems too,
`path-case:<path>`) -- because `impact` and the agent hooks compare paths as
written.

## `crossweft.json`

At the repository root; it also marks the root. Every key is optional.

| Key | Default | Meaning |
|---|---|---|
| `model_dir` | `seams/model` | where the `*.json` model files live |
| `output_dir` | `seams` | where `crossweft render` writes the generated docs and `viewer.html`; it never overwrites a file there that it did not generate |
| `lock_file` | `<output_dir>/pairs.lock.json` | pair attestations; may not live in `model_dir` |
| `language` | `en` | language of the generated docs and viewer (`en`, `ru`) |
| `viewer_template` | packaged | your own HTML template for `viewer.html` |
| `check_rendered_docs` | `false` | `check` also fails when the generated docs are stale |
| `requirements` | -- | `{"doc": "docs/REQUIREMENTS.md", "id_pattern": "^[A-Z]+-\\d+$"}`: entities may reference requirement ids; each must have a table row `\| ID \|` in the doc |
| `agent` | `{"stop": "block"}` | `stop`: `block` sends the agent back while the check fails, `warn` only tells the user. `harness`: `true` makes `check` fail when the agent harness written by `crossweft agents` (hooks, AGENTS.md block, skill) is missing or out of date; `init` sets it |
| `validators` | -- | `{"dir": "scripts/validators", "timeout": 120, "timeouts": {"validate_x.py": 300}, "exclude": {"validate_gate.py": "commit gate"}}` for `crossweft validators` ([validators.md](validators.md)) |
| `discover` | -- | `{"exclude": ["vendor/**", "**/*_test.go"]}` for `crossweft discover` |
| `joints` | -- | `{"dir": "tools/joints"}`: where your [joint-kind plugins](#joint-kind-plugins) live. Optional `"sources"`: globs of the plugin files and the helpers they import, when `dir` also holds unrelated code -- `check --changed` then reruns the whole map only when one of those changes (default: any change under `dir`). A plugin that imports a helper from `dir`, or reads a file from `dir` while it is imported or checked, that neither `sources` nor the kind's inputs (entry paths, `IMPACT_GLOBS`) cover is an error (`joints:unlisted-read:<kind>:<path>`); globs match the way `--changed` does, part by part. `sources` may list any file, not only `.py`. A read is seen when the plugin runs, so a full check fails on it and an incremental check never records a pass over it; plain `--changed REV` assumes REV itself passed |
| `hazards` | -- | `{"include": ["scripts/**/*"], "exclude": [], "rules": [...]}`: run the [built-in hazard rules](#built-in-hazard-rules) on these files (`rules` defaults to all). An `include` glob that names no file is a fatal `hazard-config:empty:<glob>` problem |

## `meta`

| Field | Required | Meaning |
|---|---|---|
| `schema` | yes | `"crossweft.map.v1"` |
| `title` | yes | shown on the generated pages |
| `lanes` | yes | `[{"id", "name", "plane"?}]` -- columns of the map (where a block runs) |
| `planes` | no | `[{"id", "name"}]` -- optional groups of lanes (e.g. runtime vs delivery); when present every lane names its plane |
| `verified_on`, `verified_against` | no | date and commit the map was last reviewed at; `check` then reports which blocks' code changed since |
| `route_scan` | no | see [Routes](#routes) |
| `joint_kinds` | no | the [joint kinds](#joint-kind-plugins) this model must carry, e.g. `["handles", "pipes"]` |
| `coverage` | no | `{"roots": ["src/*"], "ignore": [{"path", "reason"}]}` -- every directory matched by `roots` must be the code of some block (hidden directories, `.name`, and `__pycache__` are skipped; anything else not on the map goes in `ignore` with a reason) |

## `blocks`

A program, service, component, library, store, external service, artifact or tool.

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | kebab-case |
| `name`, `summary` | yes | |
| `kind` | yes | `actor executable service component library store external artifact tool contract site host ci group` |
| `lane` | yes | a lane id |
| `status` | yes | `current planned out-of-scope test-only legacy` |
| `code` | no | files/directories that are this block's code (used by coverage, impact, and to decide which side a file is on) |
| `anchors` | no | claims about the code, see [Anchors](#anchors) |
| `parent` | no | a containing block |
| `route_server` | no | `true` for the block whose routes `route_scan` reads |
| `short`, `owner`, `runs`, `docs`, `evidence`, `waits_for`, `notes`, `requirements` | no | documentation fields |

## `links`

Who passes what to whom.

| Field | Required | Meaning |
|---|---|---|
| `id`, `from`, `to`, `summary` | yes | `from`/`to` are block ids |
| `transport` | yes | `named-pipe https http-loopback process-launch file registry scm in-process generated-code build-input embedded git tunnel smtp sql object-store cache webhook oauth manual ui mtls dpapi docker os-api ssh` |
| `status` | yes | as for blocks; a current link may not touch a legacy/planned block |
| `contract` | when both ends are our code | `{"name", "enforcement", "defined_in"?, "note"?}` -- see below |
| `from_anchors`, `to_anchors` | when that end has code | where each side implements the link |
| `identifiers` | no | `{"route": ["GET /v1/x"], "header": [...], "env": [...], "pipe": [...], ...}` |
| `carries`, `returns` | no | data ids this link carries (`returns`: back to the caller) |
| `gone` | no | legacy links only: `{"status": 410, "replaced_by"?: "<current link id>", "note"?}` -- the retired routes in `identifiers.route` are still registered on purpose and answer `status` (see [Routes](#routes)) |
| `label`, `sync`, `waits`, `on_error`, `evidence`, `envs`, `notes`, `requirements` | no | documentation fields |

### `contract.enforcement`

| Value | Who keeps the two sides equal | Rule |
|---|---|---|
| `shared-code` | one symbol both sides import | `defined_in` must name it |
| `generated` | a generator from one source | `defined_in` must name the source |
| `schema-tests` | shared test vectors both sides run | `defined_in` must name them |
| `duplicated` | nobody -- hand-copied on each side | a join/set/pair must read a file of **each** side |
| `convention` | nobody -- agreed by discipline | same |
| `none` | nobody | same, when both ends are our code |

A link of transport `in-process` needs no contract: the compiler checks the call.

## Guards

A guard with `"link": "<id>"` counts for that link's seam only if it reads a
file on the `from` side **and a different** file on the `to` side. Each file is
on one side at most:

- an anchor file of exactly one side (`from_anchors` / `to_anchors`) is on that
  side;
- any other file belongs to the block with the most specific `code` path that
  holds it, and so to the end that is that block or contains it (the deeper
  end, when one end contains the other);
- a file under the code of a block that only *encloses* an end -- an end
  without `code` of its own -- is on that end's side, unless the other end
  inherits the same code. Two parts of one block cannot be told apart by code:
  give them their own `code`, or read their anchor files.

### `joins` -- a value must be equal

```json
{"id": "api-version", "name": "API version", "link": "web-orders", "points": [
  {"path": "web/src/api.ts", "side": "web", "regex": "API_VERSION = \"([^\"]+)\""},
  {"path": "server/main.go", "side": "api", "regex": "APIVersion = \"([^\"]+)\""}]}
```

`side` (required, non-empty) is the label a point's value is reported under.
It does **not** decide which end of the link a file is on -- the file's path
does (see Guards above). Any label is allowed (`"server (Go)"`, `"spec"`); a
label that is a **block id** is a claim and is checked: that block, a part of
it, or a block containing it must hold the file's `code`, otherwise the model
is refused with the blocks that do hold it. A block that is not an end of the
link is fine when it holds the file (a join may read more than two files).
Measured before this rule (2026-10-07): in a real map, 638 of 644 join points
used free labels and all 6 block-id sides named the file's own block.

Each point's regex is applied to its file; **every** match counts, so a second,
drifted definition on one side is caught too. Capture groups are joined with
`,`. Optional per point: `transform` (`unescape-c`, `lower`, `strip`,
`csv-words`, `basename` -- the last path segment, `go-http-status` -- a Go
`net/http` constant name such as `StatusTooManyRequests` becomes `429`; an
unknown name stays as it is and so never matches a number), `prefix`,
`suffix`, `note`, and `within` -- a regex with one capture group that narrows
the file to regions first (the default block when the file also documents an
alternative value). Every match lying wholly inside a region counts, and the
regex runs on the whole file, so `^`, `$`, `\b`, lookbehind and lookahead see
the real text around a region; a `within` that finds no region is a problem
`anchor:join:<id>:within:<path>`.

### `sets` -- members must match

```json
{"id": "order-fields", "name": "Order JSON fields", "link": "web-orders", "mode": "equal",
 "left":  {"label": "TypeScript", "paths": ["web/src/api.ts"],
           "within": "interface Order \\{([^}]*)\\}", "regex": "^\\s*([a-z_]+)\\??:"},
 "right": {"label": "Go", "paths": ["server/main.go"],
           "within": "type Order struct \\{([^}]*)\\}", "regex": "json:\"([a-z_]+)"}}
```

`mode`: `equal`, `left-subset` (every left member is on the right),
`right-subset`. A side is either `paths` + `regex` (one capture group; optional
`within` narrows each file to regions first; optional `exclude`, `transform`) or
`files` (glob -- the members are the matching file paths; add
`"transform": ["basename"]` to compare them with bare file names; two files
that a transform maps to one member are a problem `set:<id>:collision:<member>`,
because the set could not notice either one disappearing). `allow`:
`[{"item", "side", "reason"}]` for intentional one-sided members; an allow entry
that is no longer needed fails the check.

Every entry of `paths` / `files` must yield a file: a listed file that is
missing, or a glob that matches nothing, fails the check (key
`anchor:set:<id>:<entry>`) -- it cannot silently drop out while the other
entries keep the side non-empty. An entry that names an existing file is read
as that file even when it contains glob characters (`web/orders/[id]/page.ts`).

### Importing guards from a schema

When one side of a seam is a schema and the other is hand-written,
`crossweft import openapi <spec.json> --against <file> [--link <id>]` or
`crossweft import proto <file.proto> --against <file>` prints ready-to-paste
`sets` and `joins`: component-schema properties and message fields become a
set of field names, enums a set of values, `info.version` a join, OpenAPI
`paths` a `right-subset` set (plus the `identifiers.route` list for the link,
prefixed with the path of the first server URL). Every regex is tried before it
is printed: the schema side must reproduce exactly what the schema declares,
and the hand-written side is the region of `--against` that agrees best with
it. A side that matched nothing is marked `NOT READY` and left out of the paste
block; a guard that already disagrees says so. It never writes the model.

OpenAPI is read as JSON only (3.x, pretty-printed, one key per line): the
standard library has no YAML parser, so convert YAML first. A .proto file is
tokenized with its comments stripped (messages, nested messages, `oneof`, `map`
fields, enums). Exit codes: `0` suggestions printed, `1` the schema was read
but nothing usable matched, `2` the schema (or `--against`) cannot be read.
Names are compared as written: a `snake_case` schema against a `camelCase`
client, or `STATUS_PAID` against `"paid"`, shows up as drift to review.

### `pairs` -- duplicated regions that cannot be compared by value

```json
{"id": "price-rounding", "name": "Price rounding", "link": "web-orders",
 "regions": [{"path": "web/src/pricing.ts", "side": "web"},
             {"path": "server/pricing.go", "side": "api"}]}
```

Each region is the lines between `crossweft:begin <id>` and `crossweft:end <id>`
(in any comment syntax; exactly one of each per file, on separate lines, with a
non-empty region between them), or the whole file with `"whole_file": true`. `crossweft attest <id> --reason "..."` records a
fingerprint of every region (trailing whitespace and line endings ignored) in
the lock file. Any later change to any region fails the check until someone
re-reads the others and attests again. `crossweft attest --all` attests every
changed pair; `--prune` drops lock entries for removed pairs.

## Anchors

`{"path": "server/main.go", "find": "type Order struct"}` or
`{"path": "...", "regex": "..."}`; a path ending in `/` must be a directory. A
missing file or literal always fails -- the map must be corrected -- and cannot
be excused by a finding.

## Routes

```json
"route_scan": {
  "routers": [{"path": "server/main.go", "scanner": "go-chi"},
              {"path": "web/server.js", "scanner": "express",
               "receivers": {"app": "", "axios": null}},
              {"path": "api/app.py", "scanner": "regex", "prefix": "/v1",
               "pattern": "@app\\.(?P<method>get|post|put|delete)\\(\"(?P<path>/[^\"]*)\""}],
  "router_search": ["server/**/*.go"],
  "ignore_routers": [{"path": "server/debug.go", "reason": "dev only"}],
  "client_globs": ["web/src/**/*.ts"],
  "client_exclude": ["**/*.test.ts"],
  "client_literal_regexes": ["[\"'`](/v1/[^\"'`$]*)"]
}
```

- `go-chi` understands `Get`/`Post`/... , `Handle`/`HandleFunc`, `Method` /
  `MethodFunc` (with an `http.MethodX` constant or a string literal), paths
  written as `"..."` or `` `...` ``, `r.Route("/v1", func...)` nesting, `.Mount`,
  `.Group`, `.With(...)`, and `function_prefixes` (`{"registerV1": "/v1"}` for
  routes registered in a helper). A registration whose path or method is not a
  literal -- a constant, a concatenation -- is a problem
  `route:unscannable:<file>:<Verb> <expression>` when it is called on a router
  (a variable or struct field set from `chi.NewRouter()`, or declared
  `chi.Router` / `*chi.Mux`), or on any other receiver when a handler follows
  a path-like argument (one containing a `"/..."` literal, or a name the file
  sets to one). Write it as a literal, or record a finding with that key.
- `express` (Express and Fastify, `.js`/`.ts`), `fastapi`, `flask`, `gin` and
  `echo` follow the routers the file itself creates and how they are joined:
  - `express`: `app`/`router` `.get`/`.post`/... (`.all` = `ANY`), path arrays,
    `router.route('/x').get(h).put(h)`, `app.use('/prefix', [mw, ...] router)`
    for a router created in the file (`express()`, `express.Router()`,
    `Router()`; mounted twice = served twice), Fastify `fastify.get(...)`,
    `fastify.route({method, url})`. A template literal without `${...}` is a
    literal; with one it is unscannable.
  - `fastapi`: `@app.get("/x")`/`@router.post(...)`, `api_route(...,
    methods=[...])`, `add_api_route`, `websocket` (= `GET`),
    `APIRouter(prefix=...)` and `include_router(router, prefix=...)` (nested
    routers compose), `app.mount("/x", ...)`.
  - `flask`: `@app.route("/x", methods=[...])` (default `GET`), `@bp.get(...)`,
    `add_url_rule`, `Blueprint(..., url_prefix=...)`, and
    `register_blueprint(bp, url_prefix=...)`, whose prefix replaces the
    blueprint's own; nested blueprints compose.
  - `gin`: `r.GET(...)`/`Any`/`Handle(method, ...)`/`Match([]string{...}, ...)`,
    `StaticFile` (= `GET`), `Static`/`StaticFS` (= `MOUNT`),
    `g := r.Group("/v1")` and nested groups, inline `r.Group("/x").POST(...)`.
  - `echo`: `e.GET(...)`/`Any`/`Add(method, ...)`/`Match(...)`, `File` (= `GET`),
    `Static` (= `MOUNT`), `g := e.Group("/admin")` and nested groups.

  Parameters are reported as `{name}` whatever the spelling (`:id`, `:id?`,
  `:id(\d+)`, `<int:id>`, `{id:path}`). Comments and strings are skipped
  (JavaScript regex literals are not recognised). Anything mounted below a
  literal prefix that the file does not create (a router imported from another
  file, a sub-app, a static directory, a Fastify plugin) is a `MOUNT`
  registration; without a prefix it is an `[INFO]` line naming the call, so
  that file is scanned as its own router entry. What the scanner cannot read
  statically is a problem `route:unscannable:<file>:...`, never a skipped
  route: a path, method or prefix that is not a literal (a constant, an
  f-string, a template with `${...}`, a concatenation) -- a group or mount
  prefix also covers the routes below it; a route on a receiver the file
  neither creates nor names in `receivers` (`module.exports = (app) => ...`,
  or a client call such as `axios.get('/x', ...)`), keyed
  `<receiver>.<verb> <path>`; and the routes on a router parameter or field
  typed as a group (`rg *gin.RouterGroup`, `g *echo.Group`,
  `router: APIRouter`), keyed `prefix <name>`. `receivers` maps such a name to
  its prefix (`""` for the root) or to `null` (not a router: its calls are
  ignored). A file in which a native scanner finds no registration and
  nothing unscannable is the fatal `route:scan-empty:<file>`: zero scanned is
  never a pass.
- `regex` takes a pattern with a named `path` group and an optional `method`
  group (none = `ANY`).
- `prefix` is prepended to every route of its router entry (any scanner);
  `function_prefixes` is go-chi only, `receivers` native scanners only.
- `router_search` reports every file that builds a router of a supported kind
  (`chi.NewRouter()`, `express()`, `express.Router(`, `fastify(`, `FastAPI(`,
  `APIRouter(`, `Flask(`, `Blueprint(`, `gin.Default()`, `gin.New()`,
  `echo.New()`) and is neither scanned nor ignored.
- Parameters compare equal in any spelling: `{id}`, `:id`, `<int:id>`.

Checks, over **current** links only (a planned link may name a route nobody
serves yet; a legacy link does not map a live one): every registered route
belongs to a link's `identifiers.route`; every declared route of a link into the
`route_server` block is registered; every client literal is explained by a
declared route (a literal ending in `/` is a prefix, `*` globs are allowed).

**Retired routes kept as tombstones.** A route you retired but still register,
so that old clients get a clear `410 Gone` instead of a `404`, goes on a
`legacy` link with `gone`:

```json
{"id": "old-checkout", "from": "web", "to": "api", "transport": "https",
 "status": "legacy", "summary": "retired; answers 410 checkout_retired",
 "identifiers": {"route": ["POST /v1/old-checkout"]},
 "to_anchors": [{"path": "server/main.go", "find": "retiredCheckout"}],
 "gone": {"status": 410, "replaced_by": "web-api"}}
```

Such a route is mapped (no `route:unmapped`, no finding needed), and two
checks watch it: the tombstone must stay registered
(`route:gone-unserved:<METHOD> <path>` -- drop the route from the map or keep
serving it), and a client literal that names it exactly is
`route:gone-called:<literal>` (live code calling a route that only answers that
it is gone). `gone` needs `identifiers.route` and `to_anchors` (where the
server answers it); `replaced_by` must name a current link.

## `data`

`{"id", "name", "kind", "origin", "summary"}` plus optional `defined_in`,
`stored_in`, `consumers`, `also_from`, `anchors`, `sensitivity`. Provenance
rule: data may leave a block over a link only if the block created it (`origin`,
`also_from`) or received it over another mapped link.

## `flows`

`{"id", "name", "status", "summary", "steps": [{"link", "action", "reply"?, "waits"?, "then"?}]}`
-- rendered as sequence diagrams; a current flow may not use a non-current link.

## `findings`

A known mismatch you cannot fix in this change.

| Field | Meaning |
|---|---|
| `id` | like `SM-001` |
| `title`, `summary`, `severity` (`blocker major minor info`), `kind`, `status` (`open accepted closed`) | |
| `detected_by` | problem keys this finding explains (or `manual`) |
| `owner`, `next_step` | required while open or accepted |
| `defer_reason` | required when `accepted`: `missing-data missing-dep arch-decision scope-explosion inaccessible-repo` |
| `closed_by` | required when closed (a commit or evidence) |
| `where`, `evidence`, `impact`, `since`, `notes`, `requirements` | optional |

A problem whose key is in an open finding's `detected_by` is reported as KNOWN
and does not fail the check -- unless it is fatal (a broken anchor, a stale
allow entry, a stale lock entry). A finding whose key no longer fires is
**stale** and fails the check, so the register cannot rot.

`crossweft baseline --owner <who> --next-step "<what next>"` writes one open
finding per category for every problem `check` reports now (into
`90-baseline.json`, never over an existing file) -- the adoption path for a
repository that already has drift.

Copy keys from the `check` output. The key of a join or a pair problem ends in
a short digest of that exact disagreement -- `join:<id>:<digest>` of which value
each point (its file and regex) reads, `pair:changed:<id>:<digest>` of the
current content of each region's file. A finding therefore excuses one
disagreement: when a value changes, or the same values move to other files, or
a region changes again, the new problem fails and the old finding goes stale.
Line numbers and line endings do not change the key, so moving code does not.


## Built-in hazard rules

Some seams are a line of script that runs without an error and still breaks
what another program reads: a status file, an environment, an argument. With
`"hazards": {"include": [...]}` in `crossweft.json`, `check` scans those files
for four such lines. Each hit is an ordinary problem in the `hazards` category,
keyed `hazard:<path>:<rule>:<digest>` (the digest is of the line's text, so
moving the line keeps the key and changing it makes a new one); a deliberate
use is recorded as a finding with its reason, like any other drift.

| Rule | Flags | Safe form |
|---|---|---|
| `cmd-digit-redirect` | a variable expanded right before `>`/`>>` in batch text (`echo step=%RC%>>"log"`, also inside another language's string that generates a `.cmd`). When the value is one digit, cmd reads `0>>`/`1>>` as a stream handle: `step=0` is never written, `step=1` writes `step=`. | `>>"log" echo step=%RC%` |
| `empty-to-zero` | numeric coercion of a parsed field that may be empty, with no digits-only check on the same or the three lines before: PowerShell `[int]$parts[1]`, `$x -as [int]`; Python `int(x or 0)`, `int(x) if x else 0`. An empty or missing status becomes 0 -- usually "success". It also fires on a deliberate count default (`total = int(n or 0)`); give that line a finding, or check the text first. | `if ($v -notmatch '^[0-9]+$') { <no verdict> }` / `if not v.isdigit(): raise ...`, then convert |
| `toolchain-env-name` | `RC`, `CC`, `CXX`, `LIB`, `INCLUDE`, `LINK`, `CL`, `LIBPATH`, `_CL_` or `_LINK_` set (`set`, `$env:`, `SetEnvironmentVariable`, `export`) to a status -- a number, a verdict word (`ok`, `failed`, ...), an exit code (`%ERRORLEVEL%`, `$LASTEXITCODE`, `$?`) or a status-named variable (`%BUILD_RC%`) -- in a file that later runs cmake, msbuild, cl, make, ninja, meson, gcc/clang or cargo, or as a prefix of that run in a shell (`CC=1 make`). `set RC=%ERRORLEVEL%` before CMake: "Could not find compiler set in environment variable RC". A tool, a path, an option list, another variable, extending the variable (`%INCLUDE%;...`, `+=`) or clearing it is not flagged; neither is a run named only in an `echo`/`rem` line. | rename it (`BUILD_RC`) |
| `glob-length-guard` | a `case` pattern of N `?` and one `*` (N >= 2) whose branch rejects (`exit`/`return` non-zero, `false`, `>&2`). It matches every value of N characters or more, so as "not exactly N" it refuses valid input. | `[ "${#REV}" -eq 40 ]`; for "longer than N", N+1 `?` |

Rules match text, not a parse: a batch line is read with cmd's quoting
(`"` and `^`) and its expansions left to right, shell comments and `case`
nesting are honoured, and a comment line (`#`, `rem`, `::`) is skipped. A
coercion split over two lines (`int(` then `x or 0)`), an `if` condition longer
than 200 characters in `int(x) if ... else 0`, and a digits check on another
element of the same variable (`$parts[0]` for `[int]$parts[1]`) are not told
apart. A status-named variable is judged by the last `_` part of its name. Every
pattern is linear in the input; the self-test times hostile lines. The planted unsafe and safe cases live in
`crossweft/hazards.py` (`PLANTED`) and run in `crossweft self-test`.


## Joint-kind plugins

Some joints are specific to one codebase: which port each service listens
on and each client dials, which feature flags both sides read, which queue
names a producer and a consumer agree on. A plugin teaches `check` one such kind without
changing crossweft. Each `<joints.dir>/joints_<kind>.py` is one kind; the model
carries its entries under the top-level key `<kind>` (in any model file).

```python
SECTION = "ports"                       # must equal the file name's <kind>

def validate(entries): ...              # -> list of schema errors
def check(root, entries, model): ...    # -> {"problems": [(key, message, fatal)],
                                        #     "items": int, "files": [...], "info": str}
def self_test(): ...                    # -> 0 when every planted fault is red

PAGE = "ports.md"                       # optional: a generated page in output_dir
TITLE = {"en": "Ports", "ru": "Порты"}  # its title and README link
def render_markdown(entries): ...       # its body (crossweft adds the header)
def generated(root, entries): ...       # optional: {repo-relative path: text} that
                                        # `render` writes and `check` compares
IMPACT_GLOBS = ["src/**/*.cmake"]       # optional: files that feed the kind
def impact(entries, changed): ...       # optional: [{"ref", "message", "other": [paths]}]
```

A plugin that needs the routes a Go server registers calls
`crossweft.engine.Checker.route_registrations(text, function_prefixes)` -- the
same go-chi enumerator `check` uses, returning `(method, path, line, offset)`.

`meta.joint_kinds` pins the kinds a model must carry. A pinned kind with no
entries (its model file was deleted) or no plugin (the plugin was deleted or
renamed), and a kind with entries that is not pinned, all fail the check --
so no gate can be turned off by deleting a file. A plugin's problem keys are
prefixed with its kind (`ports:port:api`) and can be excused by findings like
any other key.

A plugin is held to the same rules as crossweft itself: an import error, a
missing name, a crash or `sys.exit()` in any hook (anything but Ctrl-C is caught
and named), a malformed result (`problems`, `files` or `items` of the wrong
type, an `impact` row whose `other` is not a list of path strings), or entries it
never compared (`items` is 0 with no problem of its own) are fatal
`joints:broken:<kind>` problems, a schema error or an `[ERR]`, never a silent
pass and never a raw traceback. A plugin that reports its own problem with
`items` 0 is reported as that problem. A crash in `render_markdown` or
`generated` fails `render` and, with `check_rendered_docs`, `check` (exit 2).

A plugin may not take a crossweft model key as its kind. The files its
`generated` returns must be written normalised (`/`-separated, no empty, `.` or
`..` segment, no backslash, colon or leading `/`) and may not be, or lie under,
`crossweft.json`, `model_dir`, `output_dir`, the lock file, `joints.dir` or the
plugin's own file (compared case-folded). Its directory is on `sys.path` only
while it is imported, and afterwards every module that import added from the
plugin's directory is dropped again, so a helper there (a `csv.py`) cannot stand
in for a host module that was not imported yet. Host modules already imported
are never replaced.

`crossweft self-test` also runs every plugin's `self_test()` (it must return
`0`); with no `crossweft.json`, no `joints.dir` or no plugin it prints
`joint plugins: none configured` so the absence is visible.

`impact` names a kind whenever a changed file is a path written in its entries
(a string containing `/`) or matches `IMPACT_GLOBS`, and says so when a kind has
neither, so a plugin gate is never silently left out of the pre-commit report.

`check --changed` and `check --incremental` rerun a kind only when one of these
files (or a file under `joints.dir`) changed. A plugin that reads any other file
must list it in `IMPACT_GLOBS`: reads under `joints.dir` are caught by
`joints.sources`, reads elsewhere are not, so an undeclared one is rechecked only
by a full `check` (which CI runs).
