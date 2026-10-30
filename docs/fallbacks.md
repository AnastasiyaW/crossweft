# Fallbacks: every "carry on after a failure" is a decision

An agent that hits a failing step under pressure makes it "work": it catches
the error and goes on, reads a setting with a default, silences a command.
Each of these changes what the program does when something breaks, and nobody
decided it. `crossweft fallbacks` finds these places and keeps each one in a
registry with a status, so a fallback is agreed, refused or waiting for a
decision -- never unknown.

## Turn it on

```json
"fallbacks": {
  "include": ["server/**/*.go", "scripts/**/*.py", "web/**/*.ts"],
  "exclude": ["**/*_test.go", "**/testdata/**"],
  "registry": "seams/fallbacks.json",
  "signers": "~/.config/crossweft/allowed_signers",
  "stops": ["Fail"],
  "strict": false
}
```

`include` is required; `registry` defaults to `seams/fallbacks.json`;
`signers` is optional (see [Who decides](#who-decides)). `strict: true` makes
every fallback still waiting for a decision fail `check` (for CI or a release
gate). `stops` names the
repository's own functions that report a failure and stop (a PowerShell
`Fail` that throws, a Python `die`): a handler that calls one does not hide
the error.

## What is found

| kind | where |
|---|---|
| `swallow` | Python `except` that neither raises nor ends the process; Go `if err != nil { ... }` (on an error variable) after which the code goes on, a loop item is skipped, or values are returned without the error -- a bare `return` (the handler wrote its error) and a return that carries the error (`err`, `fmt.Errorf`, `errors.New`) are not fallbacks, nor is `panic` / `log.Fatal` / `os.Exit`; `catch` in JS/TS, C#, C/C++ and PowerShell that does not rethrow, exit, or hand back a failure (a status named `...Fail...` / `...Error...` / `..._failed`, a nonzero code) -- `return false` and a default object stay fallbacks; a JS `.catch(...)` callback likewise |
| `suppress` | Python `contextlib.suppress(...)` |
| `env-default` | `os.environ.get(name, default)`, `os.getenv(name, default)` |
| `silent-continue` | PowerShell `-ErrorAction SilentlyContinue` / `Ignore`, `$ErrorActionPreference = 'SilentlyContinue'` |

Python is read through its syntax tree; the other languages by pattern, with
strings and comments blanked first so a brace or keyword inside them is not
code. A file in `include` that cannot be read as its language is a problem,
not a silent pass.

## The registry

`seams/fallbacks.json` holds one entry per fallback:

```json
{"id": "FB-0003", "path": "server/config.go", "symbol": "Load", "kind": "swallow",
 "digest": "sha256:...", "behavior": "a missing config file means built-in defaults",
 "reason": "first run has no file yet", "status": "proposed", "proposed_by": "agent"}
```

A site's digest covers its own text and the code it guards -- the `try` body,
the Go statement that produced the error -- so the same handler around other
code is a different fallback. A site is tied to its entry by a marker comment
on its line, the line above or the line below (the first line of a handler):

```go
if err != nil { // fallback: FB-0003
```

Without a marker the entry is found by path, enclosing function, kind and
digest; a marker keeps the tie when the function is renamed. One entry covers
one site: a marker copied onto a second site is a problem, not a second
approval.

`check` fails (these problems cannot be waived as findings) on:

| problem | meaning |
|---|---|
| `fallback:unregistered:<key>` | a fallback the registry does not list |
| `fallback:rejected:<id>` | a refused fallback is still in the code |
| `fallback:changed:<id>` | an agreed fallback's code changed since the decision |
| `fallback:stale:<id>` | an entry whose site is gone |
| `fallback:unknown-marker:<path>:<line>` | a marker naming no entry, an entry of another file, kind or function, or one another site already uses |
| `fallback:undecided:<id>` | with `strict`: a fallback still waiting for a decision |
| `fallback:unsigned:<id>` | with `signers`: a decision without a valid signature |
| `fallback:rollback:<id>` | an entry's revision is older than in the last commit: an older decision was put back |
| `fallback-config:signing-off` | the registry holds signed decisions but `signers` was removed |
| `fallback-config:unreadable:<path>` | a file in `include` could not be read |

A `proposed` entry is not a problem: it is the queue of decisions, counted on
every `check` (`fallbacks: N of M wait for the owner's decision`).

### Wrappers

A helper that hides a fallback (`def read_or_default(...)`, `retryOrSkip()`)
is found where the fallback is -- inside the helper. Propose that site with
`--wrapper`: the map then lists every call of the helper with it, and a
refused wrapper fails `check` while any call remains.

## The workflow

1. The agent writes or finds a fallback; `check` fails with
   `fallback:unregistered`.
2. The agent registers it, saying what happens and why:
   `crossweft fallbacks propose <key> --behavior "..." --reason "..." --by <agent>`.
3. The owner reads the map (`crossweft fallbacks --status proposed`, or
   `--md seams/fallbacks.md` for a page) and decides:
   `crossweft fallbacks decide FB-0003 approve|reject --reason "..." --by <name>`.
4. A rejected fallback must leave the code; an approved one may not change
   without a new proposal.

## Who decides

Without `signers`, the registry is a plain file: anyone who can edit it --
including an agent -- can mark an entry approved, and `check` says so in its
info line. With `signers`, a decision counts only with an SSH signature
(namespace `crossweft-fallbacks`, identity `owner`) by a key in that file:
`decide --key <key>` signs it, `check` verifies it. The signature covers the
site, the decision, its time and its revision; each decision raises the
revision, and an entry older than the committed one fails `check`.

Two things decide whether this holds against an agent:

- **The key.** It must be one the agent cannot use -- a FIDO2 key that asks
  for a touch and a PIN (`ssh-keygen -t ed25519-sk -O verify-required`); an
  agent running as the owner can use any key file it can read.
- **The list of trusted keys.** Keep `signers` outside the repository (an
  absolute or `~` path on the owner's machine and in CI's secrets); a
  repository-relative file is accepted, but an agent can add its own key to
  it, and `check` says so. Removing `signers` while signed decisions exist
  fails `check`.

## Limits

- Patterns, not data flow: a fallback written in a shape not listed above (a
  `return default` after a failed check without any exception, a retry loop
  that switches provider) is not found. Wrappers and markers cover the ones
  you know about.
- Go: only `if <error variable> != nil` blocks are read; an error dropped with
  `_ = f()` or never checked is not found.
- A `catch` that returns a status whose name says failure is trusted to be one;
  a status named otherwise (`Status::Busy`) reads as a fallback.
- The scan always reads every file in `include`; it does not narrow to the
  changed files under `check --changed` / `--incremental`.
- Rollback is caught against the last commit only: an older decision committed
  on top of a newer one is a consistent history to `check`. Pin the newest
  decisions outside the agent's reach (a protected branch, a signed tag).
