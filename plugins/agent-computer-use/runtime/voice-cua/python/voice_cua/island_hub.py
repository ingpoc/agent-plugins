"""Forward Samantha's island state to CUAService's island hub (source=samantha).

Preferred transport: CUAService's local Unix socket (VOICE_CUA_ISLAND_SOCKET, 0600, peer uid
checked by CUAService). No Keychain read happens here, so macOS never shows a Keychain prompt.
Fallback (socket not offered): HTTP 127.0.0.1:8777 with the per-source bearer token read from the
Keychain inside this process (never argv/env/logged). State changes are coalesced to <= 10 posts/s.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

from voice_cua.island_state import ISLAND, IslandBus, IslandState

KEYCHAIN_SERVICE = "samantha.island"
DEFAULT_HUB = "http://127.0.0.1:8777"
MIN_INTERVAL_S = 0.1


def read_token(source: str = "samantha") -> str | None:
    from voice_cua.catalog import keychain_approved

    if not keychain_approved(item_id=f"island.{source}"):
        _log_once("island token not yet approved in ACU onboarding › Keychain access; use the island socket")
        return None
    try:
        out = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", source, "-w"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except subprocess.TimeoutExpired:
        _log_once("keychain access was refused or timed out (HTTP mode); use the island socket")
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode not in (0, 44):  # 44 = item not found
        _log_once(f"keychain access refused (security exit {out.returncode})")
    tok = out.stdout.strip() if out.returncode == 0 else ""
    return tok or None


def _log_once(msg: str, _seen: set[str] = set()) -> None:  # noqa: B006
    if msg in _seen:
        return
    _seen.add(msg)
    try:
        from voice_cua.activity_log import log_event

        log_event("island_hub_error", detail=msg)
    except Exception:  # noqa: BLE001
        pass


def hub_url() -> str:
    """Loopback only: never send the token anywhere else."""
    from urllib.parse import urlsplit

    url = os.environ.get("VOICE_CUA_ISLAND_HUB", DEFAULT_HUB).rstrip("/")
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost"}:
        return DEFAULT_HUB
    return url


def valid_socket(p: str) -> bool:
    import stat

    try:
        st = os.lstat(p)
    except OSError:
        return False
    return stat.S_ISSOCK(st.st_mode) and st.st_uid == os.getuid()


def socket_path() -> str | None:
    """Only a socket CUAService handed us, and only if it is a socket owned by us."""
    import stat

    p = os.environ.get("VOICE_CUA_ISLAND_SOCKET", "")
    if not p:
        return None
    try:
        st = os.lstat(p)
    except OSError:
        return None
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid():
        return None
    return p


def socket_call(path: str, method: str, route: str, body: dict | None, timeout: float = 2.0) -> int:
    """One JSON line each way; returns the HTTP-style status (0 = unreachable)."""
    req = {"method": method, "path": route}
    if body is not None:
        req["body"] = body
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            s.sendall(json.dumps(req).encode("utf-8") + b"\n")
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return int(json.loads(buf or b"{}").get("status", 0))
    except (OSError, ValueError):
        return 0


class HubForwarder:
    def __init__(
        self,
        bus: IslandBus = ISLAND,
        *,
        url: str | None = None,
        token_reader: Callable[[], str | None] = read_token,
        min_interval: float = MIN_INTERVAL_S,
        sock: str | None = None,
    ) -> None:
        self.bus = bus
        # CUAService offered a socket → socket only (never fall back to HTTP + Keychain, which prompts).
        self.sock = sock if sock is not None else (None if url else (os.environ.get("VOICE_CUA_ISLAND_SOCKET") or None))
        self.url = (url or hub_url()) + "/api/island/publish"
        self._read_token = token_reader
        self._token: str | None = None
        self._min_interval = min_interval
        self._latest: dict | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.sent = 0
        self.last_status: int | None = None
        self._logged_auth = ""

    def start(self) -> "HubForwarder":
        self.bus.subscribe(self._on_state)
        self._thread = threading.Thread(target=self._run, name="island-hub", daemon=True)
        self._thread.start()
        self._on_state(self.bus.state)
        return self

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.bus.unsubscribe(self._on_state)

    def _on_state(self, state: IslandState) -> None:
        with self._lock:
            self._latest = state.to_dict()
        self._wake.set()

    def _run(self) -> None:
        last = 0.0
        backoff = 0.5
        while not self._stop.is_set():
            self._wake.wait(timeout=5.0)
            if self._stop.is_set():
                return
            wait = self._min_interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            with self._lock:
                item, self._latest = self._latest, None
                self._wake.clear()
            if item is None:
                continue
            last = time.monotonic()
            if self._post(item):
                backoff = 0.5
            else:
                with self._lock:  # keep the newest state for the retry
                    if self._latest is None:
                        self._latest = item
                time.sleep(backoff)
                backoff = min(backoff * 2, 5.0)
                self._wake.set()

    def _post(self, item: dict, *, retry_auth: bool = True) -> bool:
        if self.sock:
            if not valid_socket(self.sock):
                self._log_auth("island socket missing or not ours; retrying")
                self.last_status = None
                return False
            status = socket_call(self.sock, "POST", "/api/island/publish", {"source": "samantha", **item})
            self.last_status = status or None
            if status == 200:
                self.sent += 1
                return True
            if status not in (0, 409):
                self._log_auth(f"hub socket {status}")
            return False
        if self._token is None:
            self._token = self._read_token()
        if not self._token:
            self._log_auth("no samantha token in Keychain (turn Samantha on from the menu to create it)")
            time.sleep(2.0)
            return False
        body = json.dumps({"source": "samantha", **item}).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._token}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                self.last_status = resp.status
                self.sent += 1
                return True
        except urllib.error.HTTPError as exc:
            self.last_status = exc.code
            if exc.code == 401 and retry_auth:
                self._token = None  # rotated in Settings: re-read once
                return self._post(item, retry_auth=False)
            if exc.code == 409:
                return False  # CUAService hasn't marked the session active yet: retry (newest state kept)
            self._log_auth(f"hub HTTP {exc.code}")
            return False
        except (urllib.error.URLError, TimeoutError, OSError):
            self.last_status = None
            return False

    def _log_auth(self, msg: str) -> None:
        if self._logged_auth == msg:  # each distinct failure logged once
            return
        self._logged_auth = msg
        try:
            from voice_cua.activity_log import log_event

            log_event("island_hub_error", detail=msg)
        except Exception:  # noqa: BLE001
            pass


_FORWARDER: HubForwarder | None = None


def start_forwarder() -> HubForwarder:
    global _FORWARDER
    if _FORWARDER is None:
        _FORWARDER = HubForwarder().start()
    return _FORWARDER
