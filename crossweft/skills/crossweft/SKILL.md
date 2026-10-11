---
name: crossweft
description: Work safely in a repository that declares its cross-component seams with crossweft (it has a crossweft.json). Use when editing code that another component depends on -- routes, headers, protocol versions, DTO fields, env vars, pipe names, duplicated algorithms -- when `crossweft check` fails, when adding a new seam or guard to the map, or when attesting a changed pair.
---

# Working in a crossweft repository

## Codex plugin behavior

When this skill is installed through the Crossweft Codex plugin, it is a
skill-only integration. It does not register Codex hooks: before a
cross-component edit, run `crossweft impact <path>` yourself, and before
concluding the task, run `crossweft check`. Do not add a Codex hook
configuration from this skill; Crossweft publishes automatic hooks only where
their command-hook contract is verified.

A **seam** is a place where two components must agree: a route the client calls
and the server registers, a header, a protocol version, the fields of a DTO,
the env vars a service reads, the same rounding rule in two languages. The map
in `model_dir` (see `crossweft.json`) declares every seam and **how** its two
sides are kept in agreement (`contract.enforcement`). Hand-written seams
(`duplicated`, `convention`, `none`) each have a **guard** that reads both
sides: a `join` (a value must be equal), a `set` (members must match) or a
`pair` (two regions are fingerprinted and re-attested after a change).

## Before and after you edit

1. Before changing a file that may be one side of a seam, run
   `crossweft impact <path>`. It lists the links through that file, the file
   on the **other** side, and the guard that compares them.
2. Make the change on **both** sides. The hook (if installed) repeats this after
   every edit.
3. Run `crossweft check`. It must end with `RESULT: PASS` before you finish.
   Exit 0 = pass, 1 = problems (each with a key), 2 = no verdict (the config or
   model could not be read, or nothing was scanned) -- 2 is never a pass.

`crossweft check --changed HEAD` runs only the guards that read a changed file.
Use it as a fast pre-commit check, never as the final one: it cannot see drift
committed earlier, so finish with the full `crossweft check`.

If the repository configures the `crossweft mcp` server, its read-only tools
`impact`, `other_side`, `check`, `show` and `discover` answer the same
questions without a shell.

## When `crossweft check` fails

Every problem prints a `key`. Fix the cause, not the guard:

| Category | What it means | What to do |
|---|---|---|
| `joins` | a value differs between sides (or between two places on one side) | make every place carry the same value |
| `sets` | a member exists on one side only | add it to the other side; an intentional difference goes into the set's `allow` list **with a reason** |
| `pairs` | a fingerprinted region changed since its last attestation | re-read every region of the pair, make them agree, then `crossweft attest <id> --reason "<what you compared>"`. If only one region changed, attest refuses: change the twin too, or -- only if you re-read it and it is already equivalent -- add `--other-side-unchanged` (it records that claim; it is not an override) |
| `routes` | the server registers a route no link declares, a link declares a route nobody serves, or the client calls an unmapped route | map the route in the link's `identifiers.route`, or remove the dead call |
| `anchors` | a literal the map points at moved or vanished | confirm the claim is still true, then update the anchor |
| `seams` | a link has no contract, or a hand-written seam has no guard | declare `contract.enforcement`; add a join/set/pair (`crossweft discover` suggests ready-made ones) |
| `provenance` / `status` | data leaves a block that never received it; a current link depends on a legacy block | fix the map or the code, whichever is wrong |
| stale finding | a recorded finding no longer reproduces | the problem is fixed: set the finding's `status` to `closed` with `closed_by` |

If the drift is real, intentional, and cannot be fixed in this change, record a
**finding** (in the model's `findings`) with the problem key in `detected_by`,
an `owner` and a `next_step`. The check then reports it as known; when someone
fixes it, the check itself asks to close the record.

`crossweft baseline --owner <who> --next-step "<what>"` records every current
problem as findings at once. It is for adopting crossweft in a repository that
already has drift, once the map is written -- never for silencing drift you
just introduced.

## Never

- Delete or weaken a guard, or widen a regex, just to make the check pass.
- Attest a pair without reading every region, or pass `--other-side-unchanged`
  to get past a refusal; the reason lands in the lock file and in review.
- Add an `allow` entry or a finding without a concrete reason, or run
  `crossweft baseline` to get past a failing check.
- Edit the generated files in `output_dir` by hand -- change the model and run
  `crossweft render`.

## Mapping a repository from scratch

The map is written by agents, not by hand; the check is what keeps an
agent-written map honest. Work in this order and let `crossweft check` drive
you -- every step below turns into problems it lists until the step is done.

1. `crossweft init` (skip if `crossweft.json` exists). Then set
   `meta.coverage.roots` to the source roots (`["src/*", "server/*"]`, ...):
   from now on `check` lists every directory that is not the code of any
   block. That list is your to-do list; the map is complete when it is empty.
2. **Blocks.** One per thing that runs, ships or stores on its own: a
   program, a service, a library, a database, a config file other code reads,
   an external API. `code` = its directories; `lane` = where it runs. Split
   where a boundary is real (a process, a language, a network hop), not by
   folder size.
3. **Links.** For every pair of blocks that talk: `transport`, and anchors on
   **both** ends -- `from_anchors`/`to_anchors` are literals that exist in the
   code (the check fails on any anchor it cannot find, so you cannot invent
   one). Put routes, headers, env vars and pipe names in `identifiers`.
4. **Enforcement, honestly.** `shared-code` only if both sides import one
   symbol, `generated` only if a generator writes both, `schema-tests` only
   if both run shared vectors -- and name it in `defined_in`. Everything
   typed twice by hand is `duplicated` or `convention`.
5. **Guards.** `crossweft discover` lists values already typed into two
   languages with ready-to-paste joins and sets. When one side is an OpenAPI
   (JSON) or .proto schema, `crossweft import openapi|proto <schema> --against
   <file> --link <id>` prints guards for its fields, enums, version and paths.
   Every hand-written seam needs one that reads a file on each side; use a
   `pair` for duplicated logic that has no value to compare.
6. **Routes.** If a block serves HTTP, add `meta.route_scan` so registered
   routes and client literals are compared with the links.
7. `crossweft check` until `RESULT: PASS`. A problem that is real drift in
   the code is a finding for the owner, not something to hide: fix it, or
   record it with an `owner` and `next_step` (`crossweft baseline` records
   all of them at once in an adoption).
8. `crossweft agents`, so every later session -- yours or another agent's --
   gets the hooks, this skill and the AGENTS.md rules automatically.

What a person should review: the enforcement of each link (is it really one
owner?), and the open findings. Everything else the check verifies.

## Adding a seam to the map

1. `crossweft discover` lists values already typed into two languages (routes,
   headers, versions, env vars) with a ready-to-paste `join`/`set` whose regexes
   were tried against the files; `crossweft import` does the same when one side
   is a schema.
2. Add the two blocks (if missing) and a link between them with
   `contract.enforcement`, `from_anchors` and `to_anchors`.
3. Add the guard with `"link": "<link id>"`. A guard must read a file on **each**
   side, or the check reports the seam as unguarded.
4. `crossweft check`, then `crossweft render` if the repository commits the
   generated docs.

Field reference: `docs/model-reference.md`; every command and flag:
`docs/cli.md` -- both in the crossweft repository.

## When crossweft itself gets it wrong

Crossweft is young, and its authors want to know where it fails. When a check
reports a disagreement that is not one, misses one you found, prints a message
you could not act on, or meets a language, framework or seam kind it does not
handle, tell your user and offer a short report: the crossweft version
(`crossweft --version`), the command, the relevant output lines, what you
expected, and the agent you run in. Leave out private code, file contents,
secrets and internal names. The user decides whether to send it, through the
"Agent report" form at https://github.com/AnastasiyaW/crossweft/issues/new/choose
or by email (the address is on https://happyin.work/crossweft/#feedback). Never file it yourself, and do not
weaken a guard to get past a false alarm: record it as a finding and report it.
