# Enforcing seams across many repositories

The hooks in [agents.md](agents.md) live in one repository: `crossweft init` commits them,
and every agent that opens that repository picks them up. A team with many repositories
usually wants two more things:

1. the check to run in every repository that has a map, whoever set up the machine;
2. a few chosen repositories to **require** a map, so an agent cannot quietly work in
   one that never adopted it.

Both are a small layer on top of the existing hooks. This page describes the pattern we
run; adapt the list, the paths and the budget to your setup.

## 1. One user-level install covers every mapped repository

Register the three hooks once in the user-level agent settings (for Claude Code,
`~/.claude/settings.json`) instead of per repository:

```json
{"hooks": {
  "SessionStart": [{"hooks": [{"type": "command", "command": "crossweft hook claude-code session-start"}]}],
  "PostToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit",
                   "hooks": [{"type": "command", "command": "crossweft hook claude-code post-edit"}]}],
  "Stop": [{"hooks": [{"type": "command", "command": "crossweft hook claude-code stop"}]}]}}
```

This is safe: the hooks do nothing in a repository without `crossweft.json`. In a mapped
repository they behave exactly as the committed harness does, including the stop hook's
progress budget (at most four blocks in a row, never an endless loop).

## 2. Require adoption in chosen repositories

A user-level install still lets an agent edit code in a repository that has no map at
all: nothing fails, because there is nothing to check. For the repositories where seams
matter, add a thin gate in front of the stop hook. What it does:

| Step | Rule |
|---|---|
| Which repositories | an explicit list of repository names (opt-in); everything else is never gated |
| When | only when the session changed source code there (docs-only work is not gated) |
| Adopted? | `crossweft.json` present; otherwise block once with the onboarding steps (`crossweft init`, then let the agent map the repository) |
| Green? | `crossweft check` and, if the repository has validators, `crossweft validators` both exit 0; the exit code is the verdict, a timeout or a launch failure blocks too |
| Budget | at most three blocks in a row per session, then yield with a visible message that the check is still red |
| Cache | a pass is cached by HEAD plus a fingerprint of the working tree, for a limited time |
| Exception | a committed waiver file with a reason, who approved it and an expiry date (keep it short, e.g. at most 60 days) |
| Owner switch | one environment variable that turns the gate off for the person who owns the policy |

Two details that matter in practice:

- **Remember what was already uncommitted at session start.** On a shared checkout other
  sessions leave changes behind; blame only the code this session changed, or the gate
  fires on sessions that wrote nothing there.
- **A crash in the gate is not green.** Report it visibly as "not checked" and log it, but
  do not block on the gate's own failure; otherwise a bug in the gate wedges every session.

## 3. Pin the version you trust

If several repositories run Crossweft, pin one released version in all of them, and let
the gate treat an unknown version as "not checked". In CI install it by the release
commit, with the tag in a comment
(`pip install "git+https://github.com/AnastasiyaW/crossweft.git@<commit>"  # v0.2.9`):
a tag can be moved by anyone with push access and would silently change what every
repository runs; a commit cannot. A fix one repository needs goes to
Crossweft first and arrives everywhere with the next release, instead of drifting into
private copies of the engine.

## What this does not give you

- It does not make a map honest: whether a link is really `shared-code` or two copies is
  still a review question.
- It is only as strong as the agent harness: an agent without hooks runs no gate. For those,
  run `crossweft check` in CI as well (`uses: AnastasiyaW/crossweft@<tag>` or
  `crossweft check --format sarif`).
