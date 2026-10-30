"""`crossweft hook <agent> <event>` -- put the seam map into a coding agent's loop.

Three moments matter:

  session-start  tell the agent this repository declares its seams, and how
                 many, so it knows the rules before it edits anything;
  post-edit      after the agent edits a file on one side of a seam, name the
                 OTHER side it must re-read and the guard that compares them
                 (and, if the guard already disagrees, say so right away);
  stop           before the agent says "done", run `check`; if a seam is out of
                 agreement, send it back to work with the reasons.

The logic is agent-neutral; the adapters only translate stdin/stdout. Claude
Code's protocol is documented and tested here; the others follow each agent's
published hook reference and are marked experimental in the docs until they
are verified end to end.

Loop safety at stop: the agent is sent back while it makes progress (the set of
failing keys changes), at most MAX_BLOCKS times in a row. When it stops making
progress the hook lets it stop but tells the USER, loudly, that the check still
fails -- never a silent pass.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from .harness import project_hooks_run
from .engine import (CONFIG_FILE, Config, ModelError, _normalize_paths, build_impact, evaluate,
                     evaluate_incremental, feedback_line,
                     find_repo_root, load_config, load_model, validate_schema, ImpactError)

AGENTS = ("claude-code", "codex", "gemini", "copilot", "cursor")
EVENTS = ("session-start", "post-edit", "stop")
MAX_BLOCKS = 4


# --------------------------------------------------------------------------- #
# stdin -> edited paths
# --------------------------------------------------------------------------- #

def _patch_paths(patch: str) -> list[str]:
    """Paths named by an apply_patch envelope (Codex)."""
    paths = []
    for line in patch.splitlines():
        for marker in ("*** Update File:", "*** Add File:", "*** Delete File:", "*** Move to:"):
            if line.startswith(marker):
                paths.append(line[len(marker):].strip())
    return paths


def edited_paths(agent: str, payload: dict) -> list[str]:
    tool_input = payload.get("tool_input")
    if agent == "copilot":
        tool_input = payload.get("toolArgs")
        if isinstance(tool_input, str):
            try:
                tool_input = json.loads(tool_input)
            except ValueError:
                tool_input = {}
    if agent == "cursor":
        return [payload["file_path"]] if isinstance(payload.get("file_path"), str) else []
    if not isinstance(tool_input, dict):
        return []
    paths = [tool_input[key] for key in ("file_path", "notebook_path", "path", "filePath")
             if isinstance(tool_input.get(key), str)]
    if agent == "codex" and isinstance(tool_input.get("command"), str):
        paths += _patch_paths(tool_input["command"])
    if isinstance(tool_input.get("edits"), list):   # multi-file edit tools
        paths += [e["file_path"] for e in tool_input["edits"]
                  if isinstance(e, dict) and isinstance(e.get("file_path"), str)]
    return paths


def _cwd_of(payload: dict) -> Path:
    cwd = payload.get("cwd")
    roots = payload.get("workspace_roots")
    if not isinstance(cwd, str) and isinstance(roots, list) and roots:
        cwd = roots[0]
    return Path(cwd) if isinstance(cwd, str) and cwd else Path(os.getcwd())


def _continuing(payload: dict) -> bool:
    """Is the agent already continuing because of a stop hook?"""
    loops = payload.get("loop_count")
    return bool(payload.get("stop_hook_active")) or (isinstance(loops, int) and loops > 0)


# --------------------------------------------------------------------------- #
# agent-neutral messages
# --------------------------------------------------------------------------- #

def _with_feedback(cfg: Config, component: str) -> str:
    line = feedback_line(cfg, component)
    return "\n" + line if line else ""


def session_message(cfg: Config) -> str:
    try:
        model = load_model(cfg)
    except ModelError as exc:
        return f"crossweft: this repository adopted crossweft but its model does not load: {exc}"
    errors = validate_schema(model)
    if errors:
        return (f"crossweft: the seam model has {len(errors)} schema error(s), first: {errors[0]}. "
                "Tell the user; `crossweft check` shows all of them.")
    links = [l for l in model.entities["links"] if l["status"] == "current"]
    guards = sum(len(model.entities[k]) for k in ("joins", "sets", "pairs", "ui_actions"))
    return (f"crossweft: this repository declares its cross-component seams in "
            f"{cfg.model_dir.as_posix()} ({len(links)} current links, {guards} guards). When you "
            "edit a file on one side of a seam you will be told which other side to re-read; "
            "keep both sides in agreement. `crossweft impact` lists the seams your change "
            "touches; `crossweft check` must pass before you finish."
            + _with_feedback(cfg, "hook session-start"))


def post_edit_message(cfg: Config, raw_paths: list[str], cwd: Path | None = None
                      ) -> str | None:
    try:
        model = load_model(cfg)
    except ModelError as exc:
        return f"crossweft: the seam model does not load: {exc}"
    if validate_schema(model):
        return "crossweft: the seam model has schema errors -- run `crossweft check`."
    inside_paths = []
    for raw in raw_paths:
        # a relative path is relative to where the agent runs, not to the repo root
        if not Path(raw).is_absolute():
            raw = str(((cwd or Path.cwd()) / raw).resolve())
        try:
            inside_paths += _normalize_paths(cfg.root, [raw])
        except ImpactError:
            continue   # a file outside this repository is not on its map
    if not inside_paths:
        return None
    report = build_impact(model, inside_paths)
    lines, touched_ids, seams = [], set(), 0
    for row in report["links"]:
        if not row["both_ends_ours"] and not row["recheck"]:
            continue   # the other end is not our code: nothing for the agent to re-read
        seams += 1
        touched_ids.add(row["link"])
        sides = ", ".join(side for side in row["touched"])
        guards = ", ".join(row["guards"]) or (
            "the single owner in contract.defined_in" if row["enforcement"] in
            ("shared-code", "generated", "schema-tests") else "NOTHING -- consider adding a guard")
        contract = row["contract"] or "no contract declared"
        other = ", ".join(row["recheck"]) or "-"
        lines.append(f"- link {row['link']} ({row['from']} -> {row['to']}, {row['transport']}; "
                     f"{contract} [{row['enforcement'] or 'none'}]): you changed its {sides} side. "
                     f"Re-read: {other}. Guarded by: {guards}.")
    for row in report["guards"]:
        ident = row["ref"].split(":", 1)[1]
        touched_ids.add(ident)
        if row.get("link") in touched_ids:
            continue   # already listed under its link
        seams += 1
        other = f" Other side to re-read: {', '.join(row['other'])}." if row.get("other") else ""
        lines.append(f"- {row['ref']} compares the file you changed with another place.{other}")
    for row in report["findings"]:
        lines.append(f"- open finding {row['finding']} has evidence in this file -- if you fixed "
                     "it, close it; if it moved, re-anchor it.")
    if not lines:
        return None
    result = evaluate(cfg)
    now = [p for p in result.new if any(ident in p.key for ident in touched_ids)]
    if now:
        lines.append("Right now these checks disagree: " + " | ".join(p.message for p in now[:4]))
    names = ", ".join(inside_paths)
    return (f"crossweft: {names} is part of {seams} cross-component seam(s). Keep both sides in "
            "agreement:\n" + "\n".join(lines)
            + "\nRun `crossweft check` before you finish." + _with_feedback(cfg, "hook post-edit"))


def stop_message(cfg: Config) -> tuple[bool, str, list[str]]:
    """(ok, reason, failing keys). ok with a reason: nothing to send the agent
    back for, but the user must be told."""
    # the incremental check: only what changed since the last verified commit, so
    # a stop on a large repository costs seconds, not the whole map
    result = evaluate_incremental(cfg)[0]
    if result.nothing_scanned:
        # `crossweft check` exits 2 here ("scanned nothing"): not verified is
        # never shown as green
        if result.counts and not any(result.counts.values()):
            # an adopted but not yet mapped repository: tell the user on every
            # stop, without costing the agent an extra turn on every prompt
            return True, ("crossweft: `crossweft check` scanned nothing -- the seam map "
                          f"in {cfg.model_dir.as_posix()} declares nothing yet, so no seam "
                          "is checked. Ask the agent to map the repository (the `crossweft` "
                          "skill; `crossweft discover` suggests guards)."), []
        return False, (f"crossweft check scanned nothing (files={result.files} "
                       f"items={result.items}): it is misconfigured -- tell the user; "
                       "`crossweft check` shows the details. Then finish."), ["scanned-nothing"]
    if result.ok:
        return True, "", []
    keys = sorted([p.key for p in result.new] + [f"stale:{s}" for s in result.stale]
                  + ([f"error:{result.load_error}"] if result.load_error else [])
                  + [f"schema:{e}" for e in result.schema_errors]
                  + [f"render:{n}" for n in result.render_stale])
    reason = ("crossweft check fails:\n"
              + "\n".join(f"- {line}" for line in result.failure_lines()) + "\n"
              + _advice(cfg, result) + _with_feedback(cfg, "hook stop"))
    return False, reason, keys


# what to do, per kind of problem: "fix the other side" is wrong advice for a
# moved anchor or an unloadable model, and an agent follows advice literally
ADVICE = {
    "drift": "A seam is out of agreement: bring the other side in line with the one you "
             "changed (`crossweft impact <file>` names it).",
    "anchors": "The map points at a literal that moved or vanished: confirm the claim is "
               "still true and update the anchor in {model}, or restore the code.",
    "routes": "A route is served or called but not on the map: add it to the link's "
              "identifiers.route in {model}, or remove the dead route/call.",
    "map": "The map is incomplete: declare contract.enforcement, add the missing guard or "
           "anchors in {model} (see the crossweft skill).",
    "hazards": "A script line breaks what another program reads (see each rule's safe "
               "form): rewrite it that way. Only a deliberate use gets a finding with its reason.",
    "other": "Fix what the lines above name.",
}
DRIFT = {"joins", "sets", "pairs", "joints", "ui"}
MAP = {"seams", "evidence", "coverage", "provenance", "status", "agents"}


def _advice(cfg: Config, result) -> str:
    model = cfg.model_dir.as_posix()
    if result.load_error or result.schema_errors:
        return ("The model does not load, so nothing was checked: fix the errors above in "
                f"{model} (or crossweft.json). Then finish.")
    kinds = []
    for problem in result.new:
        kind = ("drift" if problem.category in DRIFT else "map" if problem.category in MAP
                else problem.category if problem.category in ADVICE else "other")
        if kind not in kinds:
            kinds.append(kind)
    lines = [ADVICE[kind].format(model=model) for kind in kinds]
    if result.stale:
        lines.append("A recorded finding no longer reproduces: set its status to closed with "
                     "closed_by.")
    if result.render_stale:
        lines.append("Run `crossweft render`.")
    excusable = [p for p in result.new if not p.fatal]
    if excusable:
        example = {"id": "SM-0NN", "title": "...", "severity": "minor", "status": "open",
                   "kind": "contract-mismatch", "summary": "...",
                   "detected_by": [excusable[0].key], "owner": "...", "next_step": "..."}
        lines.append("Only if a drift is intentional and cannot be fixed in this change: add a "
                     f"finding to \"findings\" in {model}, e.g. {json.dumps(example)} "
                     "(problems marked 'must be fixed' cannot be excused that way).")
    return "\n".join(lines) + "\nThen finish."


# --------------------------------------------------------------------------- #
# loop safety for stop
# --------------------------------------------------------------------------- #

def _state_path(cfg: Config, session: str) -> Path:
    """Per-user, per-repository, per-session loop state."""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 -- no user name: fall back to the uid or a constant
        user = str(getattr(os, "getuid", lambda: "user")())
    directory = Path(tempfile.gettempdir()) / f"crossweft-{hashlib.sha256(user.encode()).hexdigest()[:12]}"
    digest = hashlib.sha256(f"{cfg.root}|{session}".encode("utf-8")).hexdigest()[:24]
    return directory / f"stop-{digest}.json"


def should_block(cfg: Config, session: str, keys: list[str], already_continuing: bool) -> bool:
    """Block while the agent makes progress, at most MAX_BLOCKS in a row."""
    path = _state_path(cfg, session)
    state = {"keys": [], "count": 0}
    if path.is_file():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {"keys": [], "count": 0}
    if not already_continuing:
        state = {"keys": [], "count": 0}
    progress = keys != state.get("keys")
    block = (not already_continuing) or (progress and state.get("count", 0) < MAX_BLOCKS)
    state = {"keys": keys, "count": state.get("count", 0) + 1 if block else state.get("count", 0)}
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        # without saved state the loop cannot be bounded: block only the first
        # time; afterwards the user is warned instead (never an endless loop)
        return block and not already_continuing
    return block


def clear_state(cfg: Config, session: str) -> None:
    try:
        _state_path(cfg, session).unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# adapters
# --------------------------------------------------------------------------- #

def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")


def _context(agent: str, event: str, message: str) -> None:
    if agent == "claude-code" or agent == "codex":
        name = {"session-start": "SessionStart", "post-edit": "PostToolUse"}[event]
        _emit({"hookSpecificOutput": {"hookEventName": name, "additionalContext": message}})
    elif agent == "gemini":
        _emit({"hookSpecificOutput": {"additionalContext": message}})
    elif agent == "copilot":
        _emit({"additionalContext": message})
    elif agent == "cursor":
        # Cursor's afterFileEdit cannot reach the agent; the stop hook carries it.
        sys.stderr.write(message + "\n")


def _block(agent: str, reason: str) -> None:
    if agent == "gemini":
        _emit({"decision": "deny", "reason": reason})
    elif agent == "cursor":
        _emit({"followup_message": reason})
    else:
        _emit({"decision": "block", "reason": reason})


def _warn_user(agent: str, message: str) -> None:
    if agent in ("claude-code", "codex", "gemini"):
        _emit({"systemMessage": message})
    else:
        sys.stderr.write(message + "\n")


def run_hook(agent: str | None, event: str | None, stdin: bytes | str,
             from_plugin: bool = False) -> int:
    if agent not in AGENTS or event not in EVENTS:
        # Never exit 2: at Stop that blocks the agent, so a harness newer than
        # this crossweft (a new event, a new agent) would trap it with a usage
        # message. Exit 1: Claude Code shows the first stderr line as a
        # non-blocking "hook error" notice, while exit 0 hides stderr entirely
        # (code.claude.com/docs/en/hooks, exit codes) -- a skipped check is not a pass.
        sys.stderr.write(f"crossweft hook: this crossweft knows agents {list(AGENTS)} and "
                         f"events {list(EVENTS)}, not {agent!r} {event!r} -- skipped (is the "
                         "hook configuration newer than crossweft? upgrade crossweft)\n")
        return 1
    if isinstance(stdin, bytes):
        try:
            # agents send UTF-8 JSON whatever the console code page is
            stdin = stdin.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            sys.stderr.write(f"crossweft hook: stdin is not UTF-8 ({exc}) -- seams were NOT "
                             "checked\n")
            return 1
    try:
        payload = json.loads(stdin) if stdin.strip() else {}
    except ValueError as exc:
        sys.stderr.write(f"crossweft hook: stdin is not JSON ({exc}) -- seams were NOT checked\n")
        return 1
    if not isinstance(payload, dict):
        payload = {}
    start = _cwd_of(payload)
    if not start.exists():
        # not "not adopted": a path we cannot see says nothing about crossweft.json
        sys.stderr.write(f"crossweft hook: the working directory {start} does not exist -- "
                         "seams were NOT checked\n")
        return 1
    try:
        root = find_repo_root(start)
    except ModelError:
        return 0   # this repository has not adopted crossweft: nothing to do
    if from_plugin and project_hooks_run(root, start):
        return 0   # the project's own hooks run the same check: do not say it twice
    try:
        cfg = load_config(root)
    except ModelError as exc:
        message = f"crossweft: {CONFIG_FILE} is invalid -- seams are NOT being checked: {exc}"
        if event == "stop":
            _warn_user(agent, message)
        else:
            _context(agent, event if event != "stop" else "post-edit", message)
        return 0
    try:
        return _dispatch(agent, event, payload, cfg)
    except Exception as exc:  # noqa: BLE001 -- a crash must reach the agent/user, never pass
        message = (f"crossweft failed ({type(exc).__name__}: {exc}) -- the seams were NOT "
                   "checked. Tell the user; run `crossweft check` to see the error.")
        if event == "stop":
            session = str(payload.get("session_id") or payload.get("conversation_id") or "default")
            continuing = _continuing(payload)
            if should_block(cfg, session, ["crash:" + type(exc).__name__], continuing):
                _block(agent, message)
            else:
                _warn_user(agent, message)
        else:
            _context(agent, event, message)
        return 0


def _dispatch(agent: str, event: str, payload: dict, cfg: Config) -> int:
    if event == "session-start":
        if agent in ("copilot", "cursor"):
            return 0   # no session context channel verified for these agents
        _context(agent, event, session_message(cfg))
        return 0
    if event == "post-edit":
        message = post_edit_message(cfg, edited_paths(agent, payload), _cwd_of(payload))
        if message:
            _context(agent, event, message)
        return 0
    # stop
    session = str(payload.get("session_id") or payload.get("conversation_id") or "default")
    ok, reason, keys = stop_message(cfg)
    if ok:
        clear_state(cfg, session)
        if reason:
            _warn_user(agent, reason)
        return 0
    continuing = _continuing(payload)
    if cfg.agent_stop == "block" and should_block(cfg, session, keys, continuing):
        _block(agent, reason)
    else:
        _warn_user(agent, "crossweft: the agent is stopping while `crossweft check` still "
                          "fails:\n" + reason)
    return 0
