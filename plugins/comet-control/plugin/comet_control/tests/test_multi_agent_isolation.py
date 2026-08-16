#!/usr/bin/env python3
"""Live multi-agent isolation, tiling, and lifecycle regression suite.

This test drives the raw Unix-socket protocol so it represents two independent
agent clients, not calls serialized by one harness. It creates a temporary
local fixture, exercises two/three/four isolated Chromium windows, and verifies
secondary-display tiling, navigation, cursor identity, and session-scoped closeout.

Exit codes:
  0  all assertions passed and cleanup completed
  1  product regression or cleanup failure
  2  bridge environment unavailable (suite skipped)
"""

from __future__ import annotations

import html
import json
import math
import os
import socket
import stat
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from PIL import Image


PLUGIN_ROOT = Path(__file__).resolve().parents[3]
SOCK = Path(os.environ.get(
    "COMET_CONTROL_BRIDGE_SOCKET",
    str(PLUGIN_ROOT / "run" / "comet-control.sock"),
))
TTL_SECONDS = int(os.environ.get("COMET_CONTROL_MULTI_AGENT_TEST_TTL_SECONDS", "90"))
BRIDGE_TIMEOUT_SECONDS = 35
PARALLEL_WAIT_MS = 2_000
WINDOW_BOUNDS_TOLERANCE = int(os.environ.get("COMET_CONTROL_WINDOW_BOUNDS_TOLERANCE", "20"))
LAYOUT_PROOF_HOLD_SECONDS = float(os.environ.get("COMET_CONTROL_LAYOUT_PROOF_HOLD_SECONDS", "0"))


class TestFailure(RuntimeError):
    """An acceptance assertion failed."""


@dataclass(frozen=True)
class Session:
    session_id: str
    label: str
    lease_token: str
    window_id: Any
    tab_id: Any


class FixtureState:
    def __init__(self, nonce: str) -> None:
        self.nonce = nonce
        self.cookie_name = f"comet_control_multi_agent_{nonce}"


class FixtureHandler(BaseHTTPRequestHandler):
    server_version = "Comet ControlMultiAgentFixture/1.0"

    @property
    def fixture(self) -> FixtureState:
        return self.server.fixture  # type: ignore[attr-defined]

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _authenticated(self) -> bool:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        value = cookie.get(self.fixture.cookie_name)
        return bool(value and value.value == self.fixture.nonce)

    def _send_html(self, body: str, *, set_cookie: str | None = None) -> None:
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _page(
        self,
        owner: str,
        phase: str,
        *,
        done: bool,
        clicked: str = "",
        clicked_at: str = "0",
    ) -> str:
        owner_json = json.dumps(owner)
        auth = "true" if self._authenticated() else "false"
        marker = '<div id="done">Done</div>' if done else ""
        controls = "" if done else f"""
          <button id="target" type="button">Operate</button>
          <a id="next" href="/done/{html.escape(owner.lower())}?phase={html.escape(phase)}">Next</a>
          <output id="click-count">0</output>
        """
        script = "" if done else f"""
          <script>
            document.querySelector('#target').addEventListener('click', () => {{
              const count = document.querySelector('#click-count');
              count.value = String(Number(count.value || '0') + 1);
              document.body.dataset.clicked = {owner_json};
              document.body.dataset.clickedAt = String(Date.now());
              const next = document.querySelector('#next');
              const url = new URL(next.href);
              url.searchParams.set('clicked', {owner_json});
              url.searchParams.set('clickedAt', document.body.dataset.clickedAt);
              next.href = url.href;
            }});
          </script>
        """
        proof_colors = {
            "A": "#d10a32",
            "B": "#0a62d1",
            "C": "#12a150",
            "D": "#c06a00",
        }
        proof_color = proof_colors[owner]
        return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <title>Comet Control Agent {html.escape(owner)}</title>
    <style>
      html, body {{ min-height: 100%; background: {proof_color}; }}
      body {{ font: 16px system-ui; margin: 40px; }}
      main {{ display: grid; gap: 18px; max-width: 420px; }}
      button, a {{ width: max-content; padding: 10px 16px; }}
    </style>
  </head>
  <body data-agent="{html.escape(owner)}" data-auth="{auth}" data-phase="{html.escape(phase)}"
        data-clicked="{html.escape(clicked)}" data-clicked-at="{html.escape(clicked_at)}">
    <main>
      <h1>Agent {html.escape(owner)}</h1>
      {marker}
      {controls}
    </main>
    {script}
  </body>
</html>"""

    def _moving_target_page(self) -> str:
        return """<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <title>Comet Control Moving Target</title>
    <style>
      html, body { min-height: 100%; background: #f4efe4; }
      body { font: 16px system-ui; margin: 0; }
      button { position: fixed; width: 150px; height: 52px; }
      #arm-moving-target { left: 80px; top: 40px; }
      #moving-target { left: 80px; top: 120px; }
      #decoy { display: none; left: 80px; top: 120px; }
    </style>
  </head>
  <body data-target-clicks="0" data-decoy-clicks="0" data-moved="false">
    <button id="arm-moving-target" type="button">Arm movement</button>
    <button id="moving-target" class="action" type="button"><span>Open search</span></button>
    <button id="decoy" class="action" type="button"><span>Open search</span></button>
    <script>
      const target = document.querySelector('#moving-target');
      const decoy = document.querySelector('#decoy');
      target.addEventListener('click', () => {
        document.body.dataset.targetClicks = String(
          Number(document.body.dataset.targetClicks) + 1
        );
      });
      decoy.addEventListener('click', () => {
        document.body.dataset.decoyClicks = String(
          Number(document.body.dataset.decoyClicks) + 1
        );
      });
      window.armMovingTarget = () => {
        const cursor = document.querySelector('#comet-control-agent-cursor-overlay');
        if (!cursor) return false;
        const observer = new MutationObserver(() => {
          observer.disconnect();
          target.style.left = '520px';
          target.style.top = '300px';
          decoy.style.display = 'block';
          document.body.dataset.moved = 'true';
        });
        observer.observe(cursor, { attributes: true, attributeFilter: ['style'] });
        return true;
      };
      document.querySelector('#arm-moving-target').addEventListener('click', window.armMovingTarget);
    </script>
  </body>
</html>"""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        parsed = urlparse(self.path)
        phase = parse_qs(parsed.query).get("phase", ["unknown"])[0]
        if parsed.path == "/bootstrap":
            cookie = (
                f"{self.fixture.cookie_name}={self.fixture.nonce}; "
                "Path=/; SameSite=Lax"
            )
            self._send_html(
                "<!doctype html><html><body>"
                '<main id="bootstrap">Profile ready</main>'
                "</body></html>",
                set_cookie=cookie,
            )
            return
        if parsed.path == "/logout":
            cookie = (
                f"{self.fixture.cookie_name}=; Path=/; Max-Age=0; SameSite=Lax"
            )
            self._send_html(
                '<!doctype html><html><body><main id="logged-out">Logged out</main></body></html>',
                set_cookie=cookie,
            )
            return
        if parsed.path == "/slow":
            time.sleep(3)
            self._send_html('<!doctype html><html><body><main id="slow">Slow</main></body></html>')
            return
        if parsed.path == "/moving-target":
            self._send_html(self._moving_target_page())
            return
        if parsed.path in {"/a", "/b", "/c", "/d"}:
            self._send_html(self._page(parsed.path[1:].upper(), phase, done=False))
            return
        if parsed.path in {"/done/a", "/done/b", "/done/c", "/done/d"}:
            query = parse_qs(parsed.query)
            self._send_html(
                self._page(
                    parsed.path[-1].upper(),
                    phase,
                    done=True,
                    clicked=query.get("clicked", [""])[0],
                    clicked_at=query.get("clickedAt", ["0"])[0],
                )
            )
            return
        self.send_error(404)

def bridge(payload: dict[str, Any], *, timeout: float = BRIDGE_TIMEOUT_SECONDS) -> dict[str, Any]:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    with client:
        client.connect(str(SOCK))
        client.sendall(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    if not chunks:
        raise TestFailure("bridge returned an empty response")
    try:
        result = json.loads(b"".join(chunks))
    except json.JSONDecodeError as exc:
        raise TestFailure(f"bridge returned invalid JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise TestFailure("bridge response was not an object")
    return result


def require(condition: bool, name: str, detail: str = "") -> None:
    if not condition:
        suffix = f" ({detail})" if detail else ""
        raise TestFailure(f"{name}{suffix}")
    suffix = f" — {detail}" if detail else ""
    print(f"  [PASS] {name}{suffix}")


def require_success(response: dict[str, Any], operation: str) -> dict[str, Any]:
    require(
        response.get("success") is True,
        f"{operation} succeeds",
        str(response.get("error") or response.get("error_code") or ""),
    )
    return response


def require_failure(
    response: dict[str, Any], operation: str, *, contains: str | None = None
) -> dict[str, Any]:
    detail = str(response.get("error") or response.get("error_code") or "")
    require(response.get("success") is not True, f"{operation} is rejected", detail)
    if contains:
        require(contains.lower() in detail.lower(), f"{operation} explains the rejection")
    return response


def preflight(session_id: str, label: str, url: str) -> Session:
    response = require_success(
        bridge(
            {
                "type": "session_preflight",
                "sessionId": session_id,
                "agentLabel": label,
                "url": url,
                "isolation": "window",
                "ttlSeconds": TTL_SECONDS,
                "timeoutSeconds": BRIDGE_TIMEOUT_SECONDS,
            }
        ),
        f"preflight {label}",
    )
    for field in ("session_id", "lease_token", "window_id", "tab_id"):
        require(response.get(field) is not None, f"preflight returns {field}")
    require(response["session_id"] == session_id, "preflight preserves session identity")
    return Session(
        session_id=session_id,
        label=label,
        lease_token=str(response["lease_token"]),
        window_id=response["window_id"],
        tab_id=response["tab_id"],
    )


def run_session(
    session: Session,
    actions: list[dict[str, Any]],
    *,
    timeout: int = BRIDGE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    return require_success(
        bridge(
            {
                "type": "run",
                "sessionId": session.session_id,
                "leaseToken": session.lease_token,
                "sessionName": f"Comet Control E2E {session.label}",
                "agentLabel": session.label,
                "timeoutSeconds": timeout,
                "actions": actions,
            },
            timeout=timeout + 5,
        ),
        f"operate {session.label}",
    )


def close_session(session: Session) -> dict[str, Any]:
    return require_success(
        bridge(
            {
                "type": "session_closeout",
                "sessionId": session.session_id,
                "leaseToken": session.lease_token,
                "timeoutSeconds": 10,
            },
            timeout=12,
        ),
        f"closeout {session.label}",
    )


def session_inventory() -> dict[str, dict[str, Any]]:
    response = require_success(
        bridge({"type": "sessions", "timeoutSeconds": 5}, timeout=7),
        "session inventory",
    )
    raw = response.get("sessions", [])
    if isinstance(raw, dict):
        entries = []
        for key, value in raw.items():
            item = dict(value) if isinstance(value, dict) else {}
            item.setdefault("session_id", key)
            entries.append(item)
    elif isinstance(raw, list):
        entries = [item for item in raw if isinstance(item, dict)]
    else:
        raise TestFailure("sessions response did not contain a list or object")
    inventory: dict[str, dict[str, Any]] = {}
    for item in entries:
        session_id = item.get("session_id") or item.get("sessionId")
        if session_id:
            inventory[str(session_id)] = item
    return inventory


def _rect(entry: dict[str, Any], key: str) -> dict[str, int]:
    value = entry.get(key)
    require(isinstance(value, dict), f"lease inventory returns {key}")
    rect: dict[str, int] = {}
    for field in ("left", "top", "width", "height"):
        raw = value.get(field)
        require(isinstance(raw, (int, float)), f"{key}.{field} is numeric")
        rect[field] = round(raw)
    require(rect["width"] > 0 and rect["height"] > 0, f"{key} has positive area")
    return rect


def _grid(count: int) -> tuple[int, int]:
    if count <= 1:
        return 1, 1
    if count == 2:
        return 2, 1
    columns = math.ceil(math.sqrt(count))
    return columns, math.ceil(count / columns)


def _cell(area: dict[str, int], columns: int, rows: int, slot: int) -> dict[str, int]:
    column = slot % columns
    row = slot // columns
    left_offset = area["width"] * column // columns
    right_offset = area["width"] * (column + 1) // columns
    top_offset = area["height"] * row // rows
    bottom_offset = area["height"] * (row + 1) // rows
    return {
        "left": area["left"] + left_offset,
        "top": area["top"] + top_offset,
        "width": right_offset - left_offset,
        "height": bottom_offset - top_offset,
    }


def require_tiled_layout(
    inventory: dict[str, dict[str, Any]], sessions: list[Session], expected_count: int
) -> None:
    entries = [inventory[session.session_id] for session in sessions]
    require(len(entries) == expected_count, f"{expected_count}-window layout has every lease")
    columns, rows = _grid(expected_count)
    require(
        all(entry.get("layout_count") == expected_count for entry in entries),
        f"{expected_count}-window layout count is synchronized",
    )
    require(
        all(entry.get("layout_columns") == columns and entry.get("layout_rows") == rows for entry in entries),
        f"{expected_count}-window layout uses {columns}×{rows} grid",
    )
    display_ids = {str(entry.get("display_id")) for entry in entries}
    require(
        len(display_ids) == 1 and "None" not in display_ids,
        "agent windows share one display",
        next(iter(display_ids), "missing"),
    )
    display_counts = {int(entry.get("display_count") or 0) for entry in entries}
    require(len(display_counts) == 1 and min(display_counts) >= 1, "display inventory is synchronized")
    if next(iter(display_counts)) > 1:
        require(
            all(entry.get("display_role") == "secondary" for entry in entries),
            "agent windows use a secondary display when one exists",
        )
    areas = [_rect(entry, "display_work_area") for entry in entries]
    require(all(area == areas[0] for area in areas), "agent windows share one display work area")
    slots = {int(entry.get("layout_slot")) for entry in entries}
    require(slots == set(range(expected_count)), f"{expected_count}-window slots are unique")

    actual_bounds: list[dict[str, int]] = []
    for entry in entries:
        slot = int(entry["layout_slot"])
        requested = _rect(entry, "requested_window_bounds")
        expected = _cell(areas[0], columns, rows, slot)
        require(requested == expected, f"layout slot {slot} requests its exact grid cell")
        actual = _rect(entry, "window_bounds")
        actual_bounds.append(actual)
        deltas = {key: abs(actual[key] - requested[key]) for key in requested}
        require(
            max(deltas.values()) <= WINDOW_BOUNDS_TOLERANCE,
            f"layout slot {slot} reaches requested macOS bounds",
            str(deltas),
        )

    for first_index, first in enumerate(actual_bounds):
        for second in actual_bounds[first_index + 1 :]:
            overlap_width = max(
                0,
                min(first["left"] + first["width"], second["left"] + second["width"])
                - max(first["left"], second["left"]),
            )
            overlap_height = max(
                0,
                min(first["top"] + first["height"], second["top"] + second["height"])
                - max(first["top"], second["top"]),
            )
            require(
                overlap_width * overlap_height == 0,
                f"{expected_count}-window layout has no overlapping windows",
            )


def result_of(response: dict[str, Any], action_type: str) -> dict[str, Any]:
    for item in response.get("results", []):
        if isinstance(item, dict) and item.get("type") == action_type:
            return item
    raise TestFailure(f"run response did not contain {action_type!r}")


def parallel_pair(
    first: Callable[[], Any], second: Callable[[], Any]
) -> tuple[Any, Any]:
    barrier = threading.Barrier(3)

    def synchronized(call: Callable[[], Any]) -> Any:
        barrier.wait(timeout=5)
        return call()

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="comet-control-agent") as pool:
        first_future = pool.submit(synchronized, first)
        second_future = pool.submit(synchronized, second)
        barrier.wait(timeout=5)
        return first_future.result(), second_future.result()


def start_fixture() -> tuple[ThreadingHTTPServer, threading.Thread, FixtureState, str, int]:
    state = FixtureState(uuid.uuid4().hex[:12])
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    server.daemon_threads = True
    server.fixture = state  # type: ignore[attr-defined]
    port = int(server.server_address[1])
    thread = threading.Thread(
        target=server.serve_forever,
        name="comet-control-multi-agent-fixture",
        daemon=True,
    )
    thread.start()
    return server, thread, state, f"http://127.0.0.1:{port}", port


def stop_fixture(
    server: ThreadingHTTPServer, thread: threading.Thread, port: int
) -> tuple[bool, bool]:
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
    deadline = time.monotonic() + 2
    port_closed = False
    while time.monotonic() < deadline:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.2)
        with probe:
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                port_closed = True
                break
        time.sleep(0.05)
    return not thread.is_alive(), port_closed


def bridge_socket_paths() -> set[str]:
    paths: set[str] = set()
    for candidate in SOCK.parent.glob(f"{SOCK.name}*"):
        try:
            if stat.S_ISSOCK(candidate.stat().st_mode):
                paths.add(str(candidate))
        except FileNotFoundError:
            continue
    return paths


def parallel_actions(base_url: str, owner: str) -> list[dict[str, Any]]:
    owner_lower = owner.lower()
    return [
        {"type": "goto", "url": f"{base_url}/{owner_lower}?phase=parallel", "waitMs": 200},
        {"type": "wait_for_selector", "selector": "#target", "timeout": 5_000},
        {"type": "wait", "ms": PARALLEL_WAIT_MS},
        {"type": "click_selector", "selector": "#target"},
        {"type": "wait", "ms": 400},
        {"type": "click_selector", "selector": "#next"},
        {"type": "wait_for_selector", "selector": "#done", "timeout": 5_000},
        {"type": "cursor_move", "x": 80, "y": 80},
        {"type": "wait", "ms": 400},
        {"type": "cursor_status"},
        {"type": "screenshot", "format": "png"},
        {
            "type": "evaluate",
            "expression": (
                "({agent:document.body.dataset.agent,"
                "authenticated:document.body.dataset.auth,"
                "path:location.pathname,"
                "clicked:document.body.dataset.clicked,"
                "clickedAt:Number(document.body.dataset.clickedAt)})"
            ),
        },
    ]


def verify_agent_result(
    response: dict[str, Any], session: Session, expected_owner: str
) -> dict[str, Any]:
    cursor = result_of(response, "cursor_status")
    require(cursor.get("visible") is True, f"{session.label} cursor is visible")
    require(
        cursor.get("agent_label") == session.label,
        f"{session.label} cursor has the exact owner label",
    )
    require(
        cursor.get("agent_id") == session.session_id,
        f"{session.label} cursor has the exact owner id",
    )
    require(
        cursor.get("label_below_pointer") is True,
        f"{session.label} label is below its pointer",
    )
    verify_screenshot_capture(response, session, expected_owner)
    evaluated = result_of(response, "evaluate").get("result")
    require(isinstance(evaluated, dict), f"{session.label} evaluation returns page identity")
    require(evaluated.get("agent") == expected_owner, f"{session.label} stayed on its own page")
    require(
        evaluated.get("authenticated") == "true",
        f"{session.label} inherited the shared profile cookie",
    )
    require(
        evaluated.get("path") == f"/done/{expected_owner.lower()}",
        f"{session.label} navigation stayed isolated",
    )
    require(evaluated.get("clicked") == expected_owner, f"{session.label} clicked its own target")
    require(isinstance(evaluated.get("clickedAt"), int), f"{session.label} records click time")
    return evaluated


def verify_screenshot_capture(
    response: dict[str, Any], session: Session, expected_owner: str
) -> None:
    screenshot = result_of(response, "screenshot")
    screenshot_path = Path(str(screenshot.get("screenshot_path") or ""))
    require(screenshot_path.is_file(), f"{session.label} returns screenshot proof")
    require(
        screenshot_path.stat().st_size > 1_000,
        f"{session.label} screenshot proof is non-empty",
        str(screenshot_path),
    )
    expected_rgb = {
        "A": (209, 10, 50),
        "B": (10, 98, 209),
        "C": (18, 161, 80),
        "D": (192, 106, 0),
    }[expected_owner]
    with Image.open(screenshot_path) as screenshot_image:
        screenshot_rgb = screenshot_image.convert("RGB")
        actual_rgb = screenshot_rgb.getpixel(
            (screenshot_rgb.width - 8, screenshot_rgb.height - 8)
        )
    require(
        all(abs(actual - expected) <= 16 for actual, expected in zip(actual_rgb, expected_rgb)),
        f"{session.label} screenshot pixels belong to its leased page",
        f"expected={expected_rgb} actual={actual_rgb} path={screenshot_path}",
    )
    require(
        screenshot.get("capture_attempts") in {1, 2},
        f"{session.label} screenshot uses the bounded capture path",
    )


def main() -> int:
    print("comet-control multi-agent isolation suite\n")
    if not SOCK.exists():
        print(f"SKIP: bridge socket not found at {SOCK}")
        return 2
    try:
        status = bridge({"type": "status", "timeoutSeconds": 5}, timeout=7)
    except (OSError, TestFailure) as exc:
        print(f"SKIP: bridge unavailable ({exc})")
        return 2
    if status.get("success") is not True:
        print(f"SKIP: bridge status failed ({status.get('error')})")
        return 2
    socket_mode = stat.S_IMODE(SOCK.stat().st_mode)
    require(socket_mode & 0o077 == 0, "bridge socket is private to the user", oct(socket_mode))

    baseline_sockets = bridge_socket_paths()
    server: ThreadingHTTPServer | None = None
    server_thread: threading.Thread | None = None
    fixture: FixtureState | None = None
    base_url = ""
    fixture_port = 0
    sessions: list[Session] = []
    explicitly_closed: set[str] = set()
    logged_out = False
    failure: str | None = None
    cleanup_failures: list[str] = []

    try:
        server, server_thread, fixture, base_url, fixture_port = start_fixture()
        suffix = uuid.uuid4().hex[:10]
        session_a_id = f"comet-control-e2e-a-{suffix}"
        session_b_id = f"comet-control-e2e-b-{suffix}"
        label_a = f"Agent A {suffix}"
        label_b = f"Agent B {suffix}"

        def tracked_preflight(session_id: str, label: str, url: str) -> Session:
            session = preflight(session_id, label, url)
            sessions.append(session)
            return session

        print("=== PREFLIGHT: two isolated profile windows ===")
        require_failure(
            bridge({
                "type": "session_preflight",
                "agentLabel": "Missing Session ID",
                "url": f"{base_url}/a?phase=missing-id",
                "isolation": "window",
                "timeoutSeconds": BRIDGE_TIMEOUT_SECONDS,
            }),
            "preflight without sessionId",
            contains="sessionId is required",
        )
        require_failure(
            bridge({
                "type": "run",
                "sessionName": "legacy-active-tab-takeover",
                "useSelectedTab": True,
                "actions": [{"type": "wait", "ms": 1}],
                "timeoutSeconds": 5,
            }),
            "unleased active-tab run",
            contains="requires session_preflight",
        )
        missing_url_id = f"comet-control-e2e-missing-url-{suffix}"
        require_failure(
            bridge({
                "type": "session_preflight",
                "sessionId": missing_url_id,
                "agentLabel": "Missing URL",
                "isolation": "window",
                "timeoutSeconds": BRIDGE_TIMEOUT_SECONDS,
            }),
            "preflight without explicit URL",
            contains="explicit controllable",
        )
        tab_isolation_id = f"comet-control-e2e-tab-isolation-{suffix}"
        require_failure(
            bridge({
                "type": "session_preflight",
                "sessionId": tab_isolation_id,
                "agentLabel": "Tab Isolation",
                "url": f"{base_url}/a?phase=tab-isolation",
                "isolation": "tab",
                "timeoutSeconds": BRIDGE_TIMEOUT_SECONDS,
            }),
            "tab-only preflight",
            contains="isolation=window",
        )
        claimed_tab_id = f"comet-control-e2e-claimed-tab-{suffix}"
        require_failure(
            bridge({
                "type": "session_preflight",
                "sessionId": claimed_tab_id,
                "agentLabel": "Claimed Tab",
                "url": f"{base_url}/a?phase=claimed-tab",
                "claimTabId": 1,
                "timeoutSeconds": BRIDGE_TIMEOUT_SECONDS,
            }),
            "claimed-tab preflight",
            contains="claimed and tab-only targets are disabled",
        )
        invalid_ttl_id = f"comet-control-e2e-invalid-ttl-{suffix}"
        require_failure(
            bridge({
                "type": "session_preflight",
                "sessionId": invalid_ttl_id,
                "agentLabel": "Invalid TTL",
                "url": f"{base_url}/a?phase=invalid-ttl",
                "isolation": "window",
                "ttlSeconds": "not-a-number",
                "timeoutSeconds": BRIDGE_TIMEOUT_SECONDS,
            }),
            "preflight with invalid ttlSeconds",
            contains="finite number",
        )
        require_failure(
            bridge({"type": "status", "timeoutSeconds": "not-a-number"}),
            "request with invalid timeoutSeconds",
            contains="finite number",
        )
        expiring_id = f"comet-control-e2e-expiring-{suffix}"
        renewal_ttl_seconds = 2
        expiring_response = require_success(
            bridge({
                "type": "session_preflight",
                "sessionId": expiring_id,
                "agentLabel": "Expiring Lease",
                "url": f"{base_url}/a?phase=ttl-expiry",
                "isolation": "window",
                "ttlSeconds": renewal_ttl_seconds,
                "timeoutSeconds": BRIDGE_TIMEOUT_SECONDS,
            }),
            "short-lived lease preflight",
        )
        expiring = Session(
            session_id=expiring_id,
            label="Expiring Lease",
            lease_token=str(expiring_response["lease_token"]),
            window_id=expiring_response["window_id"],
            tab_id=expiring_response["tab_id"],
        )
        sessions.append(expiring)

        print("\n=== LEASE RENEWAL: one window survives a multi-test campaign ===")
        require_failure(
            bridge({
                "type": "session_renew",
                "sessionId": expiring.session_id,
                "ttlSeconds": renewal_ttl_seconds,
                "timeoutSeconds": 5,
            }),
            "lease renewal without token",
            contains="Invalid browser lease token",
        )
        require_failure(
            bridge({
                "type": "session_renew",
                "sessionId": expiring.session_id,
                "leaseToken": f"wrong-{expiring.lease_token}",
                "ttlSeconds": renewal_ttl_seconds,
                "timeoutSeconds": 5,
            }),
            "lease renewal with wrong token",
            contains="Invalid browser lease token",
        )

        claim_token: str | None = None
        try:
            claim_response = require_success(
                bridge({
                    "type": "cua_runtime_claim",
                    "intent": "native-dialog",
                    "sessionId": expiring.session_id,
                    "leaseToken": expiring.lease_token,
                    "ttlSeconds": 15,
                    "timeoutSeconds": 5,
                }),
                "native-dialog claim for the persistent lease",
            )
            raw_claim_token = claim_response.get("claim_token")
            require(
                isinstance(raw_claim_token, str) and bool(raw_claim_token),
                "native-dialog claim returns an opaque release credential",
            )
            claim_token = raw_claim_token

            claimed_renewal = require_success(
                bridge({
                    "type": "session_renew",
                    "sessionId": expiring.session_id,
                    "leaseToken": expiring.lease_token,
                    "ttlSeconds": renewal_ttl_seconds,
                    "timeoutSeconds": 5,
                }),
                "lease renewal while native-dialog CUA owns Comet",
            )
            require(
                claimed_renewal.get("window_id") == expiring.window_id,
                "claimed renewal preserves window identity",
            )
            require(
                claimed_renewal.get("tab_id") == expiring.tab_id,
                "claimed renewal preserves tab identity",
            )

            claimed_run = require_failure(
                bridge({
                    "type": "run",
                    "sessionId": expiring.session_id,
                    "leaseToken": expiring.lease_token,
                    "timeoutSeconds": 5,
                    "actions": [{"type": "page_context"}],
                }),
                "page run while native-dialog CUA owns Comet",
                contains="reserved",
            )
            require(
                claimed_run.get("error_code") == "CUA_RUNTIME_CLAIMED",
                "claimed page run fails with the ownership error",
            )
        finally:
            if claim_token is not None:
                released_claim = require_success(
                    bridge({
                        "type": "cua_runtime_release",
                        "claimToken": claim_token,
                        "timeoutSeconds": 5,
                    }),
                    "native-dialog claim release",
                )
                require(
                    released_claim.get("released") is True,
                    "native-dialog claim is released",
                )

        renewal_started_at = time.monotonic()
        for renewal_round in range(3):
            time.sleep(0.8)
            renewed = require_success(
                bridge({
                    "type": "session_renew",
                    "sessionId": expiring.session_id,
                    "leaseToken": expiring.lease_token,
                    "ttlSeconds": renewal_ttl_seconds,
                    "timeoutSeconds": 5,
                }),
                f"lease renewal round {renewal_round + 1}",
            )
            require(
                renewed.get("window_id") == expiring.window_id,
                f"renewal round {renewal_round + 1} preserves window identity",
            )
            require(
                renewed.get("tab_id") == expiring.tab_id,
                f"renewal round {renewal_round + 1} preserves tab identity",
            )
            require(
                renewed.get("ttl_seconds") == renewal_ttl_seconds,
                f"renewal round {renewal_round + 1} preserves requested TTL",
            )
            require(
                isinstance(renewed.get("renewed_at"), (int, float))
                and isinstance(renewed.get("expires_at"), (int, float))
                and renewed["expires_at"] > renewed["renewed_at"],
                f"renewal round {renewal_round + 1} returns a future expiry",
            )

        require(
            time.monotonic() - renewal_started_at > renewal_ttl_seconds,
            "renewal campaign crosses the lease's original expiry",
        )
        active_reaper = tracked_preflight(
            f"comet-control-e2e-active-reaper-{suffix}",
            "Active Reaper Trigger",
            f"{base_url}/b?phase=ttl-active-reaper",
        )
        close_session(active_reaper)
        explicitly_closed.add(active_reaper.session_id)
        renewed_inventory = session_inventory()
        require(expiring_id in renewed_inventory, "renewed lease survives lifecycle reaping")
        require(
            renewed_inventory[expiring_id].get("window_id") == expiring.window_id,
            "renewed lease retains its original window",
        )
        require(
            renewed_inventory[expiring_id].get("tab_id") == expiring.tab_id,
            "renewed lease retains its original tab",
        )

        time.sleep(renewal_ttl_seconds + 0.2)
        # Inventory is strictly read-only. A new host-locked lifecycle request
        # performs TTL cleanup before creating its own isolated window.
        reaper_trigger = tracked_preflight(
            f"comet-control-e2e-reaper-{suffix}",
            "Reaper Trigger",
            f"{base_url}/b?phase=ttl-reaper",
        )
        close_session(reaper_trigger)
        explicitly_closed.add(reaper_trigger.session_id)
        expiry_check = require_success(
            bridge({"type": "sessions", "sessionId": expiring_id, "timeoutSeconds": 5}),
            "expired lease inventory",
        )
        require(not expiry_check.get("sessions"), "expired lease is absent from inventory")
        require(
            any(item.get("reason") == "lease-expired" for item in expiry_check.get("removals", [])),
            "TTL reaper records lease-expired removal",
        )
        expired_cleanup = close_session(expiring)
        require(expired_cleanup.get("already_closed") is True, "expired lease cleanup is idempotent")
        explicitly_closed.add(expiring.session_id)
        timed_out_id = f"comet-control-e2e-timeout-{suffix}"
        require_failure(
            bridge({
                "type": "session_preflight",
                "sessionId": timed_out_id,
                "agentLabel": "Timed Out Preflight",
                "url": f"{base_url}/slow",
                "isolation": "window",
                "timeoutSeconds": 2,
            }, timeout=5),
            "preflight that exceeds its deadline",
            contains="preflight",
        )
        session_a, session_b = parallel_pair(
            lambda: tracked_preflight(
                session_a_id, label_a, f"{base_url}/a?phase=preflight"
            ),
            lambda: tracked_preflight(
                session_b_id, label_b, f"{base_url}/b?phase=preflight"
            ),
        )
        require(session_a.window_id != session_b.window_id, "agents receive distinct windows")
        require(session_a.tab_id != session_b.tab_id, "agents receive distinct tabs")
        inventory = session_inventory()
        require(session_a.session_id in inventory, "Agent A lease is registered")
        require(session_b.session_id in inventory, "Agent B lease is registered")
        require(invalid_ttl_id not in inventory, "invalid preflight leaves no lease")
        require(timed_out_id not in inventory, "timed-out preflight rolls back its lease")
        require(missing_url_id not in inventory, "missing-URL preflight leaves no lease")
        require(tab_isolation_id not in inventory, "tab-only preflight leaves no lease")
        require(claimed_tab_id not in inventory, "claimed-tab preflight leaves no lease")
        require("agent" not in inventory, "missing sessionId leaves no shared fallback lease")
        require(
            all("lease_token" not in entry and "leaseToken" not in entry for entry in inventory.values()),
            "session inventory does not expose lease tokens",
        )
        require_tiled_layout(inventory, [session_a, session_b], 2)

        print("\n=== LAYOUT: three corners, then four quadrants ===")
        session_c = tracked_preflight(
            f"comet-control-e2e-c-{suffix}", f"Agent C {suffix}", f"{base_url}/c?phase=layout"
        )
        inventory = session_inventory()
        require_tiled_layout(inventory, [session_a, session_b, session_c], 3)

        session_d = tracked_preflight(
            f"comet-control-e2e-d-{suffix}", f"Agent D {suffix}", f"{base_url}/d?phase=layout"
        )
        inventory = session_inventory()
        require_tiled_layout(inventory, [session_a, session_b, session_c, session_d], 4)
        if LAYOUT_PROOF_HOLD_SECONDS > 0:
            print(
                "  [INFO] four-window layout ready for desktop capture; "
                f"holding {LAYOUT_PROOF_HOLD_SECONDS:.1f}s",
                flush=True,
            )
            time.sleep(LAYOUT_PROOF_HOLD_SECONDS)

        closed_d = close_session(session_d)
        require(closed_d.get("windows_closed") == 1, "Agent D layout window is closed")
        explicitly_closed.add(session_d.session_id)
        inventory = session_inventory()
        require_tiled_layout(inventory, [session_a, session_b, session_c], 3)

        closed_c = close_session(session_c)
        require(closed_c.get("windows_closed") == 1, "Agent C layout window is closed")
        explicitly_closed.add(session_c.session_id)
        inventory = session_inventory()
        require_tiled_layout(inventory, [session_a, session_b], 2)

        print("\n=== OWNERSHIP: token and target boundaries ===")
        require_failure(
            bridge({
                "type": "session_preflight",
                "sessionId": session_a.session_id,
                "agentLabel": "Unauthorized Relabel",
                "url": f"{base_url}/a",
                "isolation": "window",
                "timeoutSeconds": BRIDGE_TIMEOUT_SECONDS,
            }),
            "existing-session preflight without lease token",
            contains="already leased",
        )
        require_failure(
            bridge({
                "type": "session_preflight",
                "sessionId": session_a.session_id,
                "leaseToken": "wrong-token",
                "agentLabel": "Unauthorized Relabel",
                "url": f"{base_url}/a",
                "isolation": "window",
                "timeoutSeconds": BRIDGE_TIMEOUT_SECONDS,
            }),
            "existing-session preflight with wrong lease token",
            contains="already leased",
        )
        reused = require_success(
            bridge({
                "type": "session_preflight",
                "sessionId": session_a.session_id,
                "leaseToken": session_a.lease_token,
                "agentLabel": session_a.label,
                "url": f"{base_url}/a",
                "isolation": "window",
                "timeoutSeconds": BRIDGE_TIMEOUT_SECONDS,
            }),
            "existing-session preflight with correct lease token",
        )
        require(reused.get("reused") is True, "authorized preflight reuses the lease")
        require(reused.get("window_id") == session_a.window_id, "authorized reuse keeps its window")
        require_failure(
            bridge({"type": "reload", "timeoutSeconds": 5}, timeout=7),
            "extension reload while leases are active",
            contains="active",
        )
        require_failure(
            bridge({
                "type": "run",
                "sessionId": session_a.session_id,
                "leaseToken": session_a.lease_token,
                "timeoutSeconds": 10,
                "actions": [{"type": "page_context", "tabId": session_b.tab_id}],
            }, timeout=12),
            "cross-session tab targeting",
            contains="another session's tab",
        )

        print("\n=== PROFILE: bootstrap one window, observe cookie in both ===")
        run_session(
            session_a,
            [
                {"type": "goto", "url": f"{base_url}/bootstrap", "waitMs": 200},
                {"type": "wait_for_selector", "selector": "#bootstrap", "timeout": 5_000},
            ],
        )

        print("\n=== OPERATE: concurrent waits, identical selectors, isolated state ===")
        response_a, response_b = parallel_pair(
            lambda: run_session(session_a, parallel_actions(base_url, "A")),
            lambda: run_session(session_b, parallel_actions(base_url, "B")),
        )
        verified_a = verify_agent_result(response_a, session_a, "A")
        verified_b = verify_agent_result(response_b, session_b, "B")
        event_gap = abs(verified_a["clickedAt"] - verified_b["clickedAt"]) / 1000
        require(
            event_gap >= PARALLEL_WAIT_MS / 1000,
            "visual command slices serialize across agents",
            f"click gap={event_gap:.2f}s",
        )

        print("\n=== CAPTURE STABILITY: repeated concurrent lease-owned screenshots ===")
        for capture_round in range(3):
            capture_a, capture_b = parallel_pair(
                lambda: run_session(
                    session_a,
                    [{"type": "cursor_move", "x": 80, "y": 80}, {"type": "screenshot", "format": "png"}],
                ),
                lambda: run_session(
                    session_b,
                    [{"type": "cursor_move", "x": 80, "y": 80}, {"type": "screenshot", "format": "png"}],
                ),
            )
            verify_screenshot_capture(capture_a, session_a, "A")
            verify_screenshot_capture(capture_b, session_b, "B")
            require(True, f"concurrent screenshot round {capture_round + 1} preserves both leases")

        print("\n=== TARGET SAFETY: reject stale coordinates and retry moved selector ===")
        moving_locator_cases = (
            ("click_selector", {"type": "click_selector", "selector": ".action"}, "selector"),
            ("click_text", {"type": "click_text", "text": "Open search"}, "text"),
        )
        for action_type, locator_action, verified_by in moving_locator_cases:
            moving_response = run_session(
                session_a,
                [
                    {
                        "type": "goto",
                        "url": f"{base_url}/moving-target?case={verified_by}",
                        "waitMs": 150,
                    },
                    {
                        "type": "wait_for_selector",
                        "selector": "#moving-target",
                        "timeout": 5_000,
                    },
                    {
                        "type": "locator",
                        "locator": {"by": "css", "selector": "#arm-moving-target"},
                        "operation": "click",
                    },
                    locator_action,
                    {
                        "type": "evaluate",
                        "expression": (
                            "({moved:document.body.dataset.moved,"
                            "targetClicks:Number(document.body.dataset.targetClicks),"
                            "decoyClicks:Number(document.body.dataset.decoyClicks)})"
                        ),
                    },
                ],
            )
            moved_click = result_of(moving_response, action_type)
            click_proof = moved_click.get("click") or {}
            require(
                moved_click.get("retried") is True,
                f"moved {verified_by} target is re-resolved once",
            )
            require(
                click_proof.get("verified_by") == verified_by,
                f"retry is {verified_by}-verified",
            )
            require(click_proof.get("tag") == "BUTTON", "semantic button receives the click")
            moving_evaluations = [
                item
                for item in moving_response.get("results", [])
                if isinstance(item, dict) and item.get("type") == "evaluate"
            ]
            require(len(moving_evaluations) == 1, "moving-target run returns its read-only evaluation")
            moving_state = moving_evaluations[-1].get("result")
            require(isinstance(moving_state, dict), "moving-target state is readable")
            require(moving_state.get("moved") == "true", "fixture moved during cursor glide")
            require(moving_state.get("targetClicks") == 1, "moved target is clicked exactly once")
            require(moving_state.get("decoyClicks") == 0, "stale coordinate never clicks decoy")

        print("\n=== CLOSEOUT: close A while B remains operable ===")
        closed_a = close_session(session_a)
        require(closed_a.get("cursors_hidden") == 1, "Agent A cursor is hidden")
        require(closed_a.get("tabs_closed") == 1, "Agent A tab is closed")
        require(closed_a.get("windows_closed") == 1, "Agent A window is closed")
        closed_a_again = close_session(session_a)
        require(closed_a_again.get("already_closed") is True, "Agent A closeout is idempotent")
        explicitly_closed.add(session_a.session_id)
        inventory = session_inventory()
        require(session_a.session_id not in inventory, "Agent A lease is removed")
        require(session_b.session_id in inventory, "Agent B lease survives Agent A closeout")
        require_tiled_layout(inventory, [session_b], 1)

        continuation = run_session(
            session_b,
            [
                {"type": "goto", "url": f"{base_url}/b?phase=continue", "waitMs": 200},
                {"type": "wait_for_selector", "selector": "#target", "timeout": 5_000},
                {"type": "click_selector", "selector": "#target"},
                {"type": "wait", "ms": 400},
                {"type": "cursor_status"},
                {
                    "type": "evaluate",
                    "expression": (
                        "({agent:document.body.dataset.agent,"
                        "authenticated:document.body.dataset.auth,"
                        "clicked:document.body.dataset.clicked})"
                    ),
                },
            ],
        )
        cursor_b = result_of(continuation, "cursor_status")
        require(cursor_b.get("agent_label") == session_b.label, "Agent B keeps its cursor label")
        require(
            cursor_b.get("agent_id") == session_b.session_id,
            "Agent B keeps its cursor owner id",
        )
        continued_page = result_of(continuation, "evaluate").get("result")
        require(
            isinstance(continued_page, dict) and continued_page.get("agent") == "B",
            "Agent B still operates its own page",
        )
        require(
            continued_page.get("clicked") == "B",
            "Agent B click succeeds after Agent A closeout",
        )
        inventory = session_inventory()
        entry_b = inventory.get(session_b.session_id, {})
        require(
            (entry_b.get("window_id") or entry_b.get("windowId")) == session_b.window_id,
            "Agent B retains its original window",
        )
        require(
            (entry_b.get("tab_id") or entry_b.get("tabId")) == session_b.tab_id,
            "Agent B retains its original tab",
        )

        print("\n=== PROFILE CLEANUP: remove the temporary cookie ===")
        logout = run_session(
            session_b,
            [
                {"type": "goto", "url": f"{base_url}/logout", "waitMs": 200},
                {"type": "wait_for_selector", "selector": "#logged-out", "timeout": 5_000},
            ],
        )
        require(result_of(logout, "wait_for_selector").get("selector") == "#logged-out", "temporary profile cookie is cleared")
        logged_out = True

        closed_b = close_session(session_b)
        require(closed_b.get("cursors_hidden") == 1, "Agent B cursor is hidden")
        require(closed_b.get("tabs_closed") == 1, "Agent B tab is closed")
        require(closed_b.get("windows_closed") == 1, "Agent B window is closed")
        closed_b_again = close_session(session_b)
        require(closed_b_again.get("already_closed") is True, "Agent B closeout is idempotent")
        explicitly_closed.add(session_b.session_id)
        inventory = session_inventory()
        require(session_a.session_id not in inventory, "Agent A remains absent after all closeout")
        require(session_b.session_id not in inventory, "Agent B lease is removed")

    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if not logged_out and base_url:
            for session in reversed(sessions):
                if session.session_id in explicitly_closed:
                    continue
                try:
                    run_session(
                        session,
                        [
                            {"type": "goto", "url": f"{base_url}/logout", "waitMs": 100},
                            {
                                "type": "wait_for_selector",
                                "selector": "#logged-out",
                                "timeout": 2_000,
                            },
                        ],
                        timeout=8,
                    )
                    logged_out = True
                    break
                except Exception:
                    continue

        for session in reversed(sessions):
            try:
                close_session(session)
            except Exception as exc:
                cleanup_failures.append(f"closeout {session.label}: {exc}")

        if sessions:
            try:
                remaining = session_inventory()
                leaked = [s.session_id for s in sessions if s.session_id in remaining]
                if leaked:
                    cleanup_failures.append(f"session leases remain: {', '.join(leaked)}")
            except Exception as exc:
                cleanup_failures.append(f"session inventory unavailable: {exc}")

        if server is not None and server_thread is not None:
            stopped, port_closed = stop_fixture(server, server_thread, fixture_port)
            if not stopped:
                cleanup_failures.append("fixture server thread is still alive")
            if not port_closed:
                cleanup_failures.append(f"fixture TCP port {fixture_port} is still listening")

        after_sockets = bridge_socket_paths()
        if after_sockets != baseline_sockets:
            cleanup_failures.append(
                f"bridge socket set changed: before={sorted(baseline_sockets)} "
                f"after={sorted(after_sockets)}"
            )
        try:
            final_status = bridge({"type": "status", "timeoutSeconds": 5}, timeout=7)
            if final_status.get("success") is not True:
                cleanup_failures.append("bridge is not healthy after closeout")
        except Exception as exc:
            cleanup_failures.append(f"bridge health check failed after closeout: {exc}")

    print("\n=== SUMMARY ===")
    if failure:
        print(f"  [FAIL] {failure}")
    for item in cleanup_failures:
        print(f"  [FAIL] cleanup: {item}")
    if not failure and not cleanup_failures:
        print("  [PASS] multi-agent isolation and 2/3/4-window tiling; profile shared; closeout clean")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
