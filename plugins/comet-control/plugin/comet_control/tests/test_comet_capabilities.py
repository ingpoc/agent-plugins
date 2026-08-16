#!/usr/bin/env python3
"""Live acceptance test for the Comet Control capability surface."""

from __future__ import annotations

import base64
import shutil
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from test_multi_agent_isolation import (
    SOCK,
    Session,
    TestFailure,
    bridge,
    close_session,
    require,
    require_failure,
    require_success,
    result_of,
    session_inventory,
    stop_fixture,
)


class ParityHandler(BaseHTTPRequestHandler):
    nonce = ""

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _send(
        self,
        status: int,
        payload: bytes,
        content_type: str,
        *,
        disposition: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _html(self, body: str) -> None:
        self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        path = urlparse(self.path).path
        if path == "/frame":
            self._html(
                "<!doctype html><title>Parity frame</title>"
                '<button id="frame-button" onclick="document.querySelector(\'#frame-result\').textContent=\'clicked\'">Frame action</button>'
                '<label>Frame name <input id="frame-name"></label>'
                '<p id="frame-result">idle</p>'
            )
            return
        if path == "/download":
            payload = f"download-{self.nonce}".encode()
            self._send(
                200,
                payload,
                "text/plain",
                disposition=f'attachment; filename="comet-control-parity-{self.nonce}.txt"',
            )
            return
        if path == "/next":
            self._html('<!doctype html><title>Parity next</title><main id="next">Navigated</main>')
            return
        if path == "/asset.png":
            self._send(200, b"\x89PNG\r\n\x1a\nparity", "image/png")
            return
        if path == "/style.css":
            self._send(200, b"body{--comet-control-parity:1}", "text/css")
            return
        if path == "/":
            nonce = self.nonce
            self._html(
                f"""<!doctype html>
<html><head><title>Comet Control parity {nonce}</title><link rel="stylesheet" href="/style.css"></head>
<body data-parity="clean">
  <button id="save" aria-label="Save profile" data-testid="save-profile">Save</button>
  <button class="duplicate-action">Duplicate action</button><button class="duplicate-action">Duplicate action</button>
  <button id="disabled-action" disabled>Disabled action</button>
  <input id="readonly-action" readonly value="fixed">
  <div style="position:relative;width:180px;height:50px">
    <button id="covered-action" style="position:absolute;inset:0">Covered action</button>
    <div id="action-cover" style="position:absolute;inset:0;z-index:2;background:rgba(0,0,0,.01)"></div>
  </div>
  <div style="height:40px;width:280px"><button id="moving-action" style="animation:move-action .5s linear infinite alternate">Moving action</button></div>
  <button id="revision-mutate" onclick="document.body.append(Object.assign(document.createElement('button'),{{id:'revision-probe',textContent:'Revision probe'}}))">Mutate revision</button>
  <style>@keyframes move-action {{ from {{ transform:translateX(0) }} to {{ transform:translateX(80px) }} }}</style>
  <label>Display name <input id="display-name" placeholder="Your name" value="Existing value"></label>
  <button id="arm-remount" onclick="window.armRemountForm()">Prepare checkout form</button>
  <section id="remount-form" style="position:relative;height:420px"></section>
  <button id="arm-live-search" onclick="window.armLiveSearch()">Prepare search</button>
  <section id="live-search" style="min-height:180px"></section>
  <label>Plan <select id="plan"><option value="basic">Basic</option><option value="pro">Pro</option></select></label>
  <label><input id="enabled" type="checkbox"> Enabled</label>
  <input id="upload" type="file">
  <a id="download" href="/download">Download fixture</a>
  <button id="confirm" onclick="window.confirmResult=confirm('confirm-{nonce}')?'accepted':'dismissed';document.querySelector('#dialog-result').textContent=window.confirmResult">Confirm</button>
  <button id="prompt" onclick="window.promptResult=prompt('prompt-{nonce}','default');document.querySelector('#dialog-result').textContent=window.promptResult">Prompt</button>
  <button id="alert" onclick="alert('alert-{nonce}')">Alert</button>
  <button id="enable-beforeunload" onclick="window.onbeforeunload=()=>true">Enable beforeunload</button>
  <button id="disable-beforeunload" onclick="window.onbeforeunload=null">Disable beforeunload</button>
  <a id="leave" href="/next">Leave fixture</a>
  <p id="dialog-result">none</p>
  <img id="asset" src="/asset.png" alt="Parity asset">
  <svg id="inline-mark"><circle cx="5" cy="5" r="5"></circle></svg>
  <iframe id="child-frame" src="/frame"></iframe>
  <script>
    const remountForm = document.querySelector('#remount-form');
    const renderRemountForm = (moved = false) => {{
      remountForm.innerHTML = `
        <label id="full-name-label" style="position:absolute;left:${{moved ? 520 : 40}}px;top:${{moved ? 280 : 80}}px">
          Full name * <input id="checkout-full-name">
        </label>
        <label id="postal-label" style="position:absolute;left:40px;top:80px;display:${{moved ? 'block' : 'none'}}">
          Postal code * <input id="checkout-postal-code">
        </label>`;
    }};
    window.armRemountForm = () => setTimeout(() => {{
        renderRemountForm(false);
        const cursor = document.querySelector('#comet-control-agent-cursor-overlay');
        if (!cursor) return;
        const observer = new MutationObserver(() => {{
          observer.disconnect();
          renderRemountForm(true);
          document.body.dataset.remounted = 'true';
        }});
        observer.observe(cursor, {{ attributes: true, attributeFilter: ['style'] }});
      }}, 250);
    window.armLiveSearch = () => setTimeout(() => {{
        const host = document.querySelector('#live-search');
        host.innerHTML = `
          <button type="button" id="search-catalog">Search catalog</button>
          <h2>Search ONDC</h2>
          <form id="catalog-search" onsubmit="event.preventDefault(); this.dataset.submitted=document.querySelector('#search-query').value; return false;">
            <label for="search-query">Search</label>
            <input id="search-query" name="query" type="search" />
            <button type="submit" id="search-groceries" aria-label="Search groceries">Search groceries</button>
          </form>`;
        document.body.dataset.liveSearch = 'ready';
        const cursor = document.querySelector('#comet-control-agent-cursor-overlay');
        if (!cursor) return;
        const observer = new MutationObserver(() => {{
          observer.disconnect();
          const input = document.querySelector('#search-query');
          const form = document.querySelector('#catalog-search');
          if (input) input.replaceWith(input.cloneNode(true));
          if (form) {{
            const groceries = form.querySelector('#search-groceries');
            if (groceries) groceries.replaceWith(groceries.cloneNode(true));
          }}
          document.body.dataset.liveSearchRemounted = 'true';
        }});
        observer.observe(cursor, {{ attributes: true, attributeFilter: ['style'] }});
      }}, 250);
  </script>
</body></html>""",
            )
            return
        self.send_error(404)


def start_parity_fixture() -> tuple[ThreadingHTTPServer, threading.Thread, str, int]:
    ParityHandler.nonce = uuid.uuid4().hex[:10]
    server = ThreadingHTTPServer(("127.0.0.1", 0), ParityHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, ParityHandler.nonce, server.server_address[1]


def run(
    session: Session,
    actions: list[dict[str, Any]],
    label: str,
    *,
    timeout: float = 45,
) -> dict[str, Any]:
    return require_success(
        bridge(
            {
                "type": "run",
                "sessionId": session.session_id,
                "leaseToken": session.lease_token,
                "actions": actions,
                "timeoutSeconds": int(timeout),
            },
            timeout=timeout + 5,
        ),
        label,
    )


def expect_actionability_failure(
    session: Session,
    action: dict[str, Any],
    expected_code: str,
) -> dict[str, Any]:
    response = bridge(
        {
            "type": "run",
            "sessionId": session.session_id,
            "leaseToken": session.lease_token,
            "actions": [action],
            "timeoutSeconds": 20,
        },
        timeout=25,
    )
    require(response.get("success") is False, f"{expected_code} fails closed")
    require(response.get("error_code") == expected_code, f"{expected_code} is preserved")
    record_path = response.get("failure_record_path")
    require(record_path and Path(record_path).is_file(), f"{expected_code} retains a flight record")
    return response


def preflight(session_id: str, label: str, url: str) -> Session:
    response = require_success(
        bridge(
            {
                "type": "session_preflight",
                "sessionId": session_id,
                "agentId": session_id,
                "agentLabel": label,
                "sessionName": f"Comet Control parity · {label}",
                "url": url,
                "isolation": "window",
                "ttlSeconds": 120,
                "timeoutSeconds": 45,
            }
        ),
        f"preflight {label}",
    )
    return Session(
        session_id=session_id,
        label=label,
        lease_token=response["lease_token"],
        window_id=response["window_id"],
        tab_id=response["tab_id"],
    )


def main() -> int:
    print("comet-control capability suite\n")
    if not SOCK.exists():
        print(f"SKIP: bridge socket not found at {SOCK}")
        return 2

    server, thread, nonce, port = start_parity_fixture()
    url = f"http://127.0.0.1:{port}/"
    session: Session | None = None
    upload_path: Path | None = None
    downloaded_paths: list[Path] = []
    clipboard_restore_items: list[dict[str, str]] | None = None
    clipboard_restore_text = ""
    cleanup_failures: list[str] = []
    failure: str | None = None

    try:
        session = preflight(f"parity-{uuid.uuid4()}", "Parity", url)

        expect_actionability_failure(
            session,
            {"type": "click_text", "text": "Duplicate action"},
            "ACTIONABILITY_TARGET_COUNT",
        )
        expect_actionability_failure(
            session,
            {"type": "click_selector", "selector": "#disabled-action"},
            "ACTIONABILITY_DISABLED",
        )
        expect_actionability_failure(
            session,
            {"type": "fill_selector", "selector": "#readonly-action", "value": "changed"},
            "ACTIONABILITY_NOT_EDITABLE",
        )
        expect_actionability_failure(
            session,
            {"type": "click_selector", "selector": "#covered-action"},
            "ACTIONABILITY_OBSCURED",
        )
        expect_actionability_failure(
            session,
            {"type": "click_selector", "selector": "#moving-action"},
            "ACTIONABILITY_UNSTABLE",
        )

        revisions = run(
            session,
            [
                {"type": "snapshot"},
                {
                    "type": "click_selector",
                    "selector": "#revision-mutate",
                },
                {"type": "wait", "ms": 50},
                {"type": "snapshot"},
            ],
            "revision-scoped snapshot refs",
        )
        snapshots = [item for item in revisions["results"] if item.get("type") == "snapshot"]
        require(len(snapshots) == 2, "two revision snapshots returned")
        require(snapshots[0].get("page_revision") != snapshots[1].get("page_revision"), "DOM mutation invalidates page revision")
        for snapshot in snapshots:
            revision = snapshot.get("page_revision")
            refs = [item.get("ref") for item in snapshot.get("snapshot", [])]
            require(refs and all(f"-{revision}-" in str(ref) for ref in refs), "snapshot refs are revision scoped")

        semantic = run(
            session,
            [
                {"type": "locator", "locator": {"by": "role", "role": "button", "name": "Save profile", "exact": True}, "operation": "inspect"},
                {"type": "locator", "locator": {"by": "label", "label": "Display name", "exact": True}, "operation": "fill", "value": f"Comet Control {nonce}"},
                {"type": "locator", "locator": {"by": "placeholder", "placeholder": "Your name", "exact": True}, "operation": "value"},
                {"type": "locator", "locator": {"by": "css", "selector": "#plan"}, "operation": "select_option", "value": "pro"},
                {"type": "locator", "locator": {"by": "label", "label": "Enabled", "exact": True}, "operation": "check"},
                {"type": "locator", "locator": {"by": "testid", "testId": "save-profile"}, "operation": "count"},
            ],
            "semantic locator slice",
        )
        require(result_of(semantic, "locator")["count"] == 1, "role locator resolves exactly once")
        locator_results = [item for item in semantic["results"] if item.get("type") == "locator"]
        actual_fill_value = locator_results[2].get("value")
        require(
            actual_fill_value == f"Comet Control {nonce}",
            f"label fill replaces an existing value: {actual_fill_value!r}",
        )
        require(locator_results[3].get("selected", [{}])[0].get("value") == "pro", "select option works")
        require(locator_results[-1].get("count") == 1, "test-id locator works")

        remounted = run(
            session,
            [
                {
                    "type": "locator",
                    "locator": {"by": "css", "selector": "#arm-remount"},
                    "operation": "click",
                },
                {
                    "type": "locator",
                    "locator": {"by": "label", "label": "Full name"},
                    "operation": "type",
                    "value": f"Buyer {nonce}",
                    "timeout": 3000,
                },
                {
                    "type": "evaluate",
                    "expression": (
                        "({remounted:document.body.dataset.remounted,"
                        "name:document.querySelector('#checkout-full-name')?.value,"
                        "postal:document.querySelector('#checkout-postal-code')?.value})"
                    ),
                },
            ],
            "late-mounted remounting label input",
        )
        remounted_state = result_of(remounted, "evaluate").get("result", {})
        require(remounted_state.get("remounted") == "true", "input remounts during cursor movement")
        require(remounted_state.get("name") == f"Buyer {nonce}", "semantic type follows the remounted label target")
        require(remounted_state.get("postal") == "", "stale coordinates never type into the postal decoy")

        live_search = run(
            session,
            [
                {
                    "type": "locator",
                    "locator": {"by": "css", "selector": "#arm-live-search"},
                    "operation": "click",
                },
                {
                    "type": "locator",
                    "locator": {"by": "label", "label": "Search", "exact": True},
                    "operation": "fill",
                    "value": "rice",
                    "timeout": 3000,
                },
                {
                    "type": "locator",
                    "locator": {"by": "role", "role": "button", "name": "Search", "exact": True},
                    "operation": "click",
                    "timeout": 3000,
                },
                {
                    "type": "evaluate",
                    "expression": (
                        "({ready:document.body.dataset.liveSearch,"
                        "remounted:document.body.dataset.liveSearchRemounted,"
                        "query:document.querySelector('#search-query')?.value,"
                        "submitted:document.querySelector('#catalog-search')?.dataset.submitted,"
                        "active:document.activeElement?.id})"
                    ),
                },
            ],
            "live-style competing Search fill and submit",
        )
        live_search_state = result_of(live_search, "evaluate").get("result", {})
        require(live_search_state.get("ready") == "ready", "live search surface late-mounts")
        require(live_search_state.get("query") == "rice", "label Search fills the query field without recovery")
        require(
            live_search_state.get("submitted") == "rice",
            "exact role name Search clicks the submitter, not Search catalog",
        )
        require(
            live_search_state.get("active") != "search-catalog",
            "competing hero Search catalog is not the click target",
        )

        framed = run(
            session,
            [
                {"type": "locator", "frameSelector": "#child-frame", "locator": {"by": "role", "role": "button", "name": "Frame action", "exact": True}, "operation": "click"},
                {"type": "locator", "frameSelector": "#child-frame", "locator": {"by": "text", "text": "clicked", "exact": True}, "operation": "wait", "timeout": 3000},
                {"type": "locator", "frameSelector": "#child-frame", "locator": {"by": "label", "label": "Frame name", "exact": True}, "operation": "fill", "value": f"Frame {nonce}"},
                {"type": "locator", "frameSelector": "#child-frame", "locator": {"by": "css", "selector": "#frame-name"}, "operation": "value"},
            ],
            "frame locator slice",
        )
        frame_results = [item for item in framed["results"] if item.get("type") == "locator"]
        require(frame_results[-1].get("value") == f"Frame {nonce}", "frame-scoped fill works")

        viewport = run(
            session,
            [
                {"type": "viewport_set", "width": 640, "height": 480},
                {"type": "evaluate", "expression": "({width: innerWidth, height: innerHeight})"},
                {"type": "viewport_reset"},
            ],
            "viewport slice",
        )
        dimensions = result_of(viewport, "evaluate").get("result", {})
        require(dimensions.get("width") == 640 and dimensions.get("height") == 480, "viewport override is applied")

        readonly = run(
            session,
            [{"type": "evaluate", "expression": "document.body.dataset.parity"}],
            "read-only evaluate read",
        )
        require(result_of(readonly, "evaluate").get("result") == "clean", "read-only evaluation returns page state")
        mutation = bridge(
            {
                "type": "run",
                "sessionId": session.session_id,
                "leaseToken": session.lease_token,
                "actions": [{"type": "evaluate", "expression": "document.body.dataset.parity='mutated'"}],
            }
        )
        require_failure(mutation, "mutating evaluate is rejected")
        unchanged = run(
            session,
            [{"type": "evaluate", "expression": "document.body.dataset.parity"}],
            "post-rejection read",
        )
        require(result_of(unchanged, "evaluate").get("result") == "clean", "rejected evaluation leaves the DOM unchanged")

        cdp_setup = run(
            session,
            [
                {"type": "cdp_send", "method": "Network.enable"},
                {"type": "cdp_send", "method": "Performance.enable"},
                {"type": "cdp_send", "method": "Performance.getMetrics"},
                {"type": "cdp_events"},
            ],
            "safe CDP setup",
        )
        mark = result_of(cdp_setup, "cdp_events")["cursor"]
        metrics = [item for item in cdp_setup["results"] if item.get("method") == "Performance.getMetrics"][0]
        require(bool(metrics.get("result", {}).get("metrics")), "safe raw CDP command returns metrics")
        run(session, [{"type": "reload_page", "waitMs": 300}], "CDP observed reload")
        events = run(
            session,
            [{"type": "cdp_events", "afterSequence": mark, "methods": ["Network.responseReceived"], "limit": 20, "timeoutMs": 3000}],
            "filtered CDP event read",
        )
        require(bool(result_of(events, "cdp_events").get("events")), "raw CDP event cursor returns filtered events")

        run(
            session,
            [{"type": "locator", "locator": {"by": "css", "selector": "#confirm"}, "operation": "click"}],
            "open confirm dialog",
        )
        dialog = run(session, [{"type": "dialog_get"}], "inspect confirm dialog")
        require(result_of(dialog, "dialog_get").get("dialog", {}).get("type") == "confirm", "confirm dialog is observable")
        run(session, [{"type": "dialog_handle", "accept": True}], "accept confirm dialog")
        accepted = run(
            session,
            [{"type": "locator", "locator": {"by": "css", "selector": "#dialog-result"}, "operation": "inner_text"}],
            "confirm result",
        )
        require(result_of(accepted, "locator").get("value") == "accepted", "confirm dialog acceptance resumes the page")

        run(
            session,
            [{"type": "locator", "locator": {"by": "css", "selector": "#prompt"}, "operation": "click"}],
            "open prompt dialog",
        )
        run(session, [{"type": "dialog_handle", "accept": True, "promptText": f"prompted-{nonce}"}], "accept prompt dialog")
        prompted = run(
            session,
            [{"type": "locator", "locator": {"by": "css", "selector": "#dialog-result"}, "operation": "inner_text"}],
            "prompt result",
        )
        require(result_of(prompted, "locator").get("value") == f"prompted-{nonce}", "prompt text is delivered")

        run(
            session,
            [{"type": "locator", "locator": {"by": "css", "selector": "#alert"}, "operation": "click"}],
            "open alert dialog",
        )
        alert = run(session, [{"type": "dialog_get"}], "inspect alert dialog")
        require(result_of(alert, "dialog_get").get("dialog", {}).get("type") == "alert", "alert dialog is observable")
        run(session, [{"type": "dialog_handle", "dismiss": True}], "dismiss alert dialog")

        run(
            session,
            [
                {"type": "locator", "locator": {"by": "css", "selector": "#enable-beforeunload"}, "operation": "click"},
                {"type": "locator", "locator": {"by": "css", "selector": "#leave"}, "operation": "click"},
            ],
            "open beforeunload dialog",
        )
        beforeunload = run(session, [{"type": "dialog_get"}], "inspect beforeunload dialog")
        require(
            result_of(beforeunload, "dialog_get").get("dialog", {}).get("type") == "beforeunload",
            "beforeunload dialog is observable",
        )
        run(session, [{"type": "dialog_handle", "accept": True}], "accept beforeunload dialog")
        run(
            session,
            [
                {"type": "wait_for_selector", "selector": "#next", "timeout": 3000},
                {"type": "back", "waitMs": 300},
                {"type": "wait_for_selector", "selector": "#save", "timeout": 3000},
            ],
            "return after beforeunload navigation",
        )
        run(
            session,
            [{"type": "locator", "locator": {"by": "css", "selector": "#disable-beforeunload"}, "operation": "click"}],
            "disable fixture beforeunload handler",
        )

        original_clipboard = run(
            session,
            [{"type": "clipboard_read_text"}, {"type": "clipboard_read", "includeData": True}],
            "read original clipboard",
        )
        original_text = result_of(original_clipboard, "clipboard_read_text").get("text", "")
        original_items = result_of(original_clipboard, "clipboard_read").get("items", [])
        require(not any(item.get("data_omitted") for item in original_items), "original clipboard is fully restorable")
        clipboard_restore_text = original_text
        clipboard_restore_items = [
            {
                "type": item["type"],
                **({"base64": item["base64"]} if item.get("base64") else {"text": item.get("text", "")}),
            }
            for item in original_items
        ]
        run(session, [{"type": "clipboard_write_text", "text": f"clipboard-{nonce}"}], "write clipboard")
        clipboard = run(
            session,
            [{"type": "clipboard_read_text"}, {"type": "clipboard_read"}],
            "read clipboard",
        )
        require(result_of(clipboard, "clipboard_read_text").get("text") == f"clipboard-{nonce}", "clipboard text round-trip works")
        require(bool(result_of(clipboard, "clipboard_read").get("items")), "clipboard item inventory works")
        png_base64 = base64.b64encode(
            (Path(__file__).parents[1] / "extension" / "images" / "icon16.png").read_bytes()
        ).decode("ascii")
        binary_clipboard = run(
            session,
            [
                {
                    "type": "clipboard_write",
                    "items": [
                        {
                            "type": "image/png",
                            "base64": png_base64,
                        }
                    ],
                },
                {"type": "clipboard_read", "includeData": True},
            ],
            "binary clipboard round-trip",
        )
        binary_readable = any(
            item.get("type") == "image/png" and item.get("base64")
            for item in result_of(binary_clipboard, "clipboard_read").get("items", [])
        )
        if clipboard_restore_items:
            run(
                session,
                [{"type": "clipboard_write", "items": clipboard_restore_items}],
                "restore original clipboard",
            )
        else:
            run(
                session,
                [{"type": "clipboard_write_text", "text": clipboard_restore_text}],
                "restore original clipboard",
            )
        clipboard_restore_items = None
        require(binary_readable, "binary clipboard payload is readable")

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
            handle.write(f"upload-{nonce}")
            upload_path = Path(handle.name)
        upload = run(
            session,
            [
                {"type": "upload_files", "selector": "#upload", "paths": [str(upload_path)]},
                {"type": "evaluate", "expression": "document.querySelector('#upload').files[0].name"},
            ],
            "file upload slice",
        )
        require(result_of(upload, "evaluate").get("result") == upload_path.name, "file chooser receives local path")

        download = run(
            session,
            [{"type": "download_click", "locator": {"by": "css", "selector": "#download"}, "timeout": 30000}],
            "download click",
            timeout=40,
        )
        download_path = Path(result_of(download, "download_click")["filename"])
        downloaded_paths.append(download_path)
        require(download_path.exists(), "download materializes a local file")
        require(download_path.read_text() == f"download-{nonce}", "downloaded file content is correct")

        inventory = run(
            session,
            [{"type": "page_assets_list", "includeInlineSvg": True, "limit": 100}],
            "page asset inventory",
        )
        assets = result_of(inventory, "page_assets_list")
        require(any(item.get("kind") == "image" for item in assets.get("assets", [])), "page asset inventory includes images")
        require(assets.get("summary", {}).get("inline_svg_count", 0) >= 1, "page asset inventory includes inline SVG")
        bundled = run(
            session,
            [{"type": "page_assets_bundle", "inventoryId": assets["id"], "kinds": ["image"], "timeout": 30000}],
            "page asset bundle",
            timeout=45,
        )
        bundle_result = result_of(bundled, "page_assets_bundle")
        downloaded_paths.extend(Path(item["path"]) for item in bundle_result.get("assets", []))
        downloaded_paths.append(Path(bundle_result["manifest_path"]))
        require(bundle_result.get("summary", {}).get("downloaded_count", 0) >= 1, "page asset bundle downloads selected assets")
        require(Path(bundle_result["manifest_path"]).exists(), "page asset bundle writes a manifest")

        history = require_success(
            bridge({"type": "history", "query": nonce, "limit": 10}),
            "focused history lookup",
        )
        require(any(nonce in f"{item.get('title')} {item.get('url')}" for item in history.get("history", [])), "history lookup finds the fixture")

        close_session(session)
        session = None

        user_tabs = require_success(bridge({"type": "user_tabs", "filter": nonce, "limit": 20}), "list user tabs")
        require(isinstance(user_tabs.get("tabs"), list), "user tab inventory remains read only")
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if session is not None and clipboard_restore_items is not None:
            try:
                if clipboard_restore_items:
                    run(
                        session,
                        [{"type": "clipboard_write", "items": clipboard_restore_items}],
                        "restore clipboard after failure",
                    )
                else:
                    run(
                        session,
                        [{"type": "clipboard_write_text", "text": clipboard_restore_text}],
                        "restore clipboard after failure",
                    )
            except Exception as exc:
                cleanup_failures.append(f"clipboard restore: {exc}")
        if session is not None:
            try:
                close_session(session)
            except Exception as exc:
                cleanup_failures.append(f"session closeout: {exc}")
        if upload_path is not None:
            upload_path.unlink(missing_ok=True)
        for path in downloaded_paths:
            path.unlink(missing_ok=True)
        bundle_dirs = sorted({path.parent for path in downloaded_paths if "Comet Control Page Assets" in str(path)}, key=lambda path: len(path.parts), reverse=True)
        for directory in bundle_dirs:
            shutil.rmtree(directory, ignore_errors=True)
        try:
            (Path.home() / "Downloads" / "Comet Control Page Assets").rmdir()
        except OSError:
            pass
        stopped, port_closed = stop_fixture(server, thread, port)
        if not stopped:
            cleanup_failures.append("fixture thread is still alive")
        if not port_closed:
            cleanup_failures.append(f"fixture port {port} is still listening")
        try:
            remaining = session_inventory()
            leaked = [session_id for session_id in remaining if session_id.startswith("parity-")]
            if leaked:
                cleanup_failures.append(f"leases remain: {leaked}")
        except TestFailure as exc:
            cleanup_failures.append(f"inventory: {exc}")

    print("\n=== SUMMARY ===")
    if failure:
        print(f"  [FAIL] {failure}")
    for item in cleanup_failures:
        print(f"  [FAIL] cleanup: {item}")
    if not failure and not cleanup_failures:
        print("  [PASS] Comet capability rows are live-proven")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
