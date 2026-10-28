"""Click every button the map lists and see the request it sends.

`crossweft check` proves on the source that each button reaches a handler and
the handler a declared route. It cannot prove the handler runs: it may throw,
the button may be covered or disabled, the page may build a button the source
scan never saw. This smoke test closes that gap at runtime:

  1. `crossweft actions --json` names every control and the route its chain
     ends in;
  2. a local server serves web/public and records each /v1 request (the API
     itself is not started: the route checks keep the server side honest);
  3. Chromium opens the page, compares the page's [data-action] buttons with
     the map, presses each one and waits for its request.

Exit 0 when every button sent its request with no page error, 1 when one did
not (or the page shows a button the map does not know), 2 when the test could
not run (no Playwright or browser, or crossweft could not trace the map).

  python smoke/click_smoke.py              run against this example
  python smoke/click_smoke.py --self-test  also prove it turns red on a
                                           handler that throws
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE.parent
WAIT_SECONDS = 3.0   # a local request either arrives at once or not at all


class Recorder(SimpleHTTPRequestHandler):
    """Serves web/public; answers every /v1 request with an empty JSON object."""

    seen: list[tuple[str, str]] = []

    def _api(self) -> None:
        Recorder.seen.append((self.command, self.path))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self) -> None:  # noqa: N802 -- http.server's naming
        if self.path.startswith("/v1/"):
            self._api()
        else:
            super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        self._api()

    def log_message(self, *args) -> None:
        pass


def trace(root: Path) -> dict | None:
    override = os.environ.get("CROSSWEFT")
    command = shlex.split(override) if override else [sys.executable, "-m", "crossweft"]
    proc = subprocess.run([*command, "--root", str(root), "actions", "--json"],
                          capture_output=True, text=True, encoding="utf-8")
    try:
        return json.loads(proc.stdout)
    except ValueError:
        print(f"[ERR] crossweft actions did not answer (exit {proc.returncode}): "
              f"{(proc.stderr or proc.stdout).strip()[:400]}")
        return None


def expected(control: dict) -> str | None:
    last = control["steps"][-1] if control["steps"] else None
    return last["key"] if last and last["kind"] == "route" else None


def sent(path: str, route: str) -> bool:
    """The request path matches the traced route: `{id}` is one path segment,
    and a route ending in "/" stands for every path below it (crossweft's rule).
    simplification: the method is not compared -- the trace names no method."""
    path = path.split("?")[0]
    if route.endswith("/") and path.startswith(route):
        return True
    pattern = "".join("[^/]+" if part.startswith("{") else re.escape(part)
                      for part in re.split(r"(\{[^}]*\})", route))
    return re.fullmatch(pattern, path) is not None


def run(root: Path) -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[ERR] Playwright is not installed: pip install playwright && "
              "playwright install chromium -- nothing was clicked")
        return 2
    traced = trace(root)
    if traced is None:
        return 2
    untraced = [row["id"] for row in traced["ui_actions"] if not row["traced"]]
    if untraced:
        print(f"[ERR] crossweft could not trace {', '.join(untraced)} -- fix the map first")
        return 2
    controls = [c for row in traced["ui_actions"] for c in row["controls"]]
    if not controls:
        print("[ERR] the map lists no controls -- nothing to click")
        return 2
    handler = partial(Recorder, directory=str(root / "web" / "public"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/index.html"
    failures = 0
    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except Exception as exc:  # noqa: BLE001 -- no browser is "could not run"
                print(f"[ERR] Chromium did not start ({exc}) -- nothing was clicked")
                return 2
            page = browser.new_page()
            errors: list[str] = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(url)
            on_page = set(page.eval_on_selector_all("[data-action]",
                                                    "els => els.map(e => e.dataset.action)"))
            mapped = {c["control"] for c in controls if not c["dynamic"]}
            for extra in sorted(on_page - mapped):
                print(f"[FAIL] button {extra!r} is on the page but not on the map")
                failures += 1
            for control in controls:
                name, route = control["control"], expected(control)
                # an allowed exact control is still checked for presence; an allowed
                # dynamic one names a prefix, not a button that could be on the page
                allowed_prefix = control["dynamic"] and control["status"] == "allowed"
                if not allowed_prefix and name not in on_page:
                    print(f"[FAIL] {name}: on the map but not on the page")
                    failures += 1
                    continue
                if control["status"] == "allowed":
                    print(f"[SKIP] {name}: allowed on the map ({control['detail']})")
                    continue
                if control["status"] != "ok" or route is None:
                    print(f"[FAIL] {name}: the map does not trace it to a route "
                          f"({control['status']}: {control['detail'] or 'no route step'})")
                    failures += 1
                    continue
                Recorder.seen.clear()
                errors.clear()
                try:
                    page.click(f'[data-action="{name}"]', timeout=2000)
                except Exception as exc:  # noqa: BLE001 -- a covered or hidden button is this control's failure
                    print(f"[FAIL] {name}: could not be clicked ({str(exc).splitlines()[0]})")
                    failures += 1
                    continue
                deadline = time.monotonic() + WAIT_SECONDS
                while time.monotonic() < deadline and not any(sent(p, route)
                                                              for _, p in Recorder.seen):
                    page.wait_for_timeout(50)
                hit = [f"{m} {p}" for m, p in Recorder.seen if sent(p, route)]
                if hit and not errors:
                    print(f"[PASS] {name} -> {hit[0]}")
                    continue
                failures += 1
                why = f"no request to {route}" if not hit else "the page reported errors"
                print(f"[FAIL] {name}: {why}" + (f" ({'; '.join(errors)[:300]})" if errors else ""))
            browser.close()
    finally:
        server.shutdown()
    print(f"RESULT: {'PASS' if not failures else 'FAIL'} ({len(controls)} controls, "
          f"{failures} failed)")
    return 1 if failures else 0


def self_test() -> int:
    """The intact page must pass and a handler that throws must fail."""
    tmp = Path(tempfile.mkdtemp(prefix="cw-smoke-"))
    try:
        intact, broken = tmp / "intact", tmp / "broken"
        for copy in (intact, broken):
            shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("smoke"))
        app = broken / "web" / "public" / "app.js"
        text = app.read_text(encoding="utf-8")
        original = 'const res = await fetch("/v1/orders", {'
        if original not in text:
            print("[ERR] self-test fixture drifted: app.js no longer has the placeOrder fetch")
            return 2
        planted = 'throw new Error("planted"); ' + original
        app.write_text(text.replace(original, planted, 1), encoding="utf-8", newline="\n")
        print("== intact copy (must pass)")
        good = run(intact)
        print("== a handler that throws before its request (must fail)")
        bad = run(broken)
        if 2 in (good, bad):
            return 2
        if good == 0 and bad == 1:
            print("SELF-TEST: PASS (green on the intact page, red on the planted throw)")
            return 0
        print(f"SELF-TEST: FAIL (intact={good}, planted={bad})")
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=str(EXAMPLE), help="the example's root")
    parser.add_argument("--self-test", action="store_true",
                        help="prove the test is red on a planted runtime failure")
    args = parser.parse_args()
    return self_test() if args.self_test else run(Path(args.root).resolve())


if __name__ == "__main__":
    sys.exit(main())
