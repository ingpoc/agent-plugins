"""Unit tests for catalog, island bus, tool defs, redaction."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from voice_cua.catalog import (  # noqa: E402
    find_by_role,
    find_key,
    load_catalog,
    save_key,
    secrets_dir,
    upsert_key,
)
from voice_cua.cua_bridge import redact_for_model  # noqa: E402
from voice_cua.inventory import build_inventory, find_by_label  # noqa: E402
from voice_cua.island_state import IslandBus  # noqa: E402
from voice_cua.audio_io import FRAME_BYTES, BLOCKSIZE, SpeakerPlayer  # noqa: E402
from voice_cua.realtime_session import RealtimeSession, build_session_update  # noqa: E402
from voice_cua.tools import tool_definitions  # noqa: E402
from voice_cua import SYSTEM_INSTRUCTIONS  # noqa: E402
from voice_cua.activity_log import log_event, tail_events  # noqa: E402
from voice_cua.gateway import Handler  # noqa: E402
from voice_cua.voice_settings import MODELS  # noqa: E402


class CatalogTests(unittest.TestCase):
    def test_rejects_secret_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text(json.dumps({"id": "x", "label": "L", "service": "s", "account": "a", "value": "nope"}))
            with patch("voice_cua.catalog.secrets_dir", return_value=Path(td)):
                with self.assertRaises(ValueError):
                    load_catalog()

    def test_upsert_and_find(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            with patch("voice_cua.catalog.secrets_dir", return_value=td_path):
                cat = {"version": 1, "keys": []}
                upsert_key(
                    cat,
                    {
                        "id": "render-api",
                        "label": "Render API",
                        "service": "com.test.render",
                        "account": "a",
                        "platform": "render",
                    },
                )
                loaded = load_catalog()
                row = find_key(loaded, "render-api")
                assert row is not None
                self.assertEqual(row["label"], "Render API")
                self.assertTrue((td_path / "render-api.json").exists())
                self.assertNotIn("value", row)


class InventoryTests(unittest.TestCase):
    def test_label_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            with patch("voice_cua.catalog.secrets_dir", return_value=td_path):
                save_key(
                    {
                        "id": "openai-api",
                        "role": "openai-runtime",
                        "label": "Open AI Realtime Key",
                        "service": "openai",
                        "account": "a",
                        "platform": "openai",
                    }
                )
                save_key(
                    {
                        "id": "render-api",
                        "label": "Render API",
                        "service": "render",
                        "account": "b",
                        "platform": "render",
                    }
                )
                inv = build_inventory()
                self.assertEqual(inv["total"], 2)
                self.assertEqual(find_by_role(load_catalog(), "openai-runtime")["id"], "openai-api")

                with patch("voice_cua.inventory.keychain_exists") as mock_exists:
                    mock_exists.side_effect = lambda label: label == "Open AI Realtime Key"
                    inv = build_inventory()
                    self.assertEqual(inv["available_count"], 1)
                    self.assertEqual(inv["missing_count"], 1)
                    self.assertEqual(inv["available_labels"], ["Open AI Realtime Key"])
                    self.assertEqual(inv["missing_labels"], ["Render API"])
                    item = find_by_label("Render API")
                    assert item is not None
                    self.assertEqual(item["status"], "missing")


class LabelsTrackerTests(unittest.TestCase):
    def test_build_and_save(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            secret_dir = Path(td) / ".secret"
            labels_file = Path(td) / "labels.json"
            secret_dir.mkdir()
            (secret_dir / "openai-api.json").write_text(
                json.dumps(
                    {
                        "id": "openai-api",
                        "label": "Open AI Realtime Key",
                        "service": "openai",
                        "account": "a",
                        "platform": "openai",
                    }
                )
            )
            with patch("voice_cua.catalog.secrets_dir", return_value=secret_dir):
                with patch("voice_cua.labels_tracker.labels_tracker_path", return_value=labels_file):
                    with patch("voice_cua.labels_tracker.scan_login_keychain") as mock_scan:
                        with patch("voice_cua.labels_tracker.keychain_exists", return_value=False):
                            mock_scan.return_value = [
                                {
                                    "kind": "generic",
                                    "label": "",
                                    "service": "zai-api-key",
                                    "account": "openclaw",
                                    "display": "zai-api-key",
                                }
                            ]
                            from voice_cua.labels_tracker import refresh_labels_tracker

                            data = refresh_labels_tracker()
                            self.assertTrue(labels_file.exists())
                            self.assertIn("Open AI Realtime Key", data["missing"])
                            self.assertTrue(any("zai-api-key" in k for k in data["available"]))


class IslandTests(unittest.TestCase):
    def test_confirm_flow(self) -> None:
        bus = IslandBus()
        result: list[bool] = []

        def worker() -> None:
            result.append(bus.request_confirm("c1", "ok?", timeout=2.0))

        t = threading.Thread(target=worker)
        t.start()
        for _ in range(50):
            if bus.state.kind == "confirm":
                break
            import time

            time.sleep(0.02)
        self.assertTrue(bus.resolve_confirm("c1", True))
        t.join(timeout=3)
        self.assertEqual(result, [True])


class ToolsTests(unittest.TestCase):
    def test_tool_names(self) -> None:
        names = {t["name"] for t in tool_definitions()}
        self.assertEqual(
            names,
            {
                "cua_state",
                "cua_act",
                "confirm_risky",
                "secrets_list",
                "secrets_label",
                "secrets_get",
                "secrets_put",
                "secrets_inject",
                "secrets_provide",
            },
        )
        for n in names:
            self.assertNotIn("cursor", n.lower())
            self.assertNotIn("grok", n.lower())

    def test_redact_screenshots(self) -> None:
        out = redact_for_model({
            "ok": True,
            "text": "hi",
            "screenshot": "AAAA",
            "screenshot_before": "BB",
            "screenshot_after": "CC",
        })
        self.assertNotIn("screenshot", out)
        self.assertNotIn("screenshot_before", out)
        self.assertEqual(out["text"], "hi")

    def test_cua_act_exposes_verified_completion_contract(self) -> None:
        act = next(t for t in tool_definitions() if t["name"] == "cua_act")
        props = act["parameters"]["properties"]
        self.assertIn("oneOf", props["expect"])
        self.assertIn("allow_unverified", props)
        self.assertIn("op", props)
        self.assertIn("open", props["op"]["enum"])
        self.assertEqual(props["path"]["type"], "string")
        self.assertIn("ok only when expect verifies", act["description"])
        self.assertIn("never search Finder for a known path", SYSTEM_INSTRUCTIONS)
        self.assertIn("never a preliminary Finder open", SYSTEM_INSTRUCTIONS)
        self.assertIn("never say done", SYSTEM_INSTRUCTIONS)

    def test_activity_log_concurrent_lines_remain_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "activity.jsonl"
            with patch.dict(os.environ, {"VOICE_CUA_ACTIVITY_LOG": str(path)}):
                threads = [
                    threading.Thread(target=log_event, args=("probe",), kwargs={"n": n})
                    for n in range(20)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                self.assertEqual(len(tail_events(limit=30)), 20)
                for line in path.read_text().splitlines():
                    json.loads(line)


class AudioIOTests(unittest.TestCase):
    def test_frame_bytes(self) -> None:
        self.assertEqual(FRAME_BYTES, BLOCKSIZE * 2)

    def test_speaker_buffer_and_clear(self) -> None:
        import base64

        player = SpeakerPlayer()
        silence = b"\x00\x01" * 100
        player.push_b64(base64.b64encode(silence).decode())
        self.assertTrue(player.has_audio())
        player.clear()
        self.assertFalse(player.has_audio())

    def test_session_update_ga_shape(self) -> None:
        with patch.dict(
            os.environ,
            {"VOICE_CUA_EAGERNESS": "balanced", "VOICE_CUA_MIC_PROFILE": "near_field"},
        ):
            upd = build_session_update()
        self.assertEqual(upd["session"]["type"], "realtime")
        self.assertIn("audio", upd["session"])
        td = upd["session"]["audio"]["input"]["turn_detection"]
        self.assertEqual(td["type"], "semantic_vad")
        self.assertEqual(td["eagerness"], "low")
        nr = upd["session"]["audio"]["input"]["noise_reduction"]
        self.assertEqual(nr["type"], "near_field")

    def test_realtime_model_choices(self) -> None:
        self.assertEqual(
            [model for model, _ in MODELS],
            ["gpt-realtime-2", "gpt-realtime-2.1-mini"],
        )

    def test_text_wait_ignores_unrelated_response(self) -> None:
        class FakeWS:
            def __init__(self):
                self.events = []

            def send(self, raw):
                self.events.append(json.loads(raw))

        session = RealtimeSession(api_key="test", enable_audio=False)
        session.ws = FakeWS()
        result = {}
        thread = threading.Thread(
            target=lambda: result.update(session.send_text_and_wait("ping", timeout=2)),
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and len(session.ws.events) < 2:
            time.sleep(0.01)
        create = session.ws.events[-1]
        request_id = create["response"]["metadata"]["voice_cua_request_id"]
        session._on_message(None, json.dumps({
            "type": "response.done",
            "response": {
                "id": "resp_other",
                "status": "completed",
                "metadata": {"voice_cua_request_id": "other"},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "WRONG"}]}],
            },
        }))
        self.assertTrue(thread.is_alive())
        session._on_message(None, json.dumps({
            "type": "response.done",
            "response": {
                "id": "resp_expected",
                "status": "completed",
                "metadata": {"voice_cua_request_id": request_id},
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "RIGHT"}]}],
            },
        }))
        thread.join(timeout=1)
        self.assertEqual(result, {"ok": True, "reply": "RIGHT"})

    def test_failed_mic_preflight_disables_audio_status(self) -> None:
        session = RealtimeSession(api_key="test", enable_audio=True)
        with (
            patch("voice_cua.audio_io.audio_available", return_value=True),
            patch("voice_cua.audio_io.wait_mic_preflight", return_value=1),
            patch("voice_cua.realtime_session.island_publish"),
        ):
            session._start_audio()
        self.assertFalse(session._audio_enabled)

    def test_tool_followup_waits_for_active_response_done(self) -> None:
        class FakeWS:
            def __init__(self):
                self.events = []

            def send(self, raw):
                self.events.append(json.loads(raw))

        session = RealtimeSession(api_key="test", enable_audio=False)
        session.ws = FakeWS()
        with session._response_condition:
            session._active_response_ids.add("resp_active")
        with patch(
            "voice_cua.realtime_session.dispatch",
            return_value={"ok": True, "verified": True},
        ):
            session._run_tool("call_1", "cua_state", '{"app":"Calculator"}')
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and not session.ws.events:
                time.sleep(0.01)
            self.assertEqual(
                [event["type"] for event in session.ws.events],
                ["conversation.item.create"],
            )
            session._finish_response("resp_active")
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and len(session.ws.events) < 2:
                time.sleep(0.01)
        self.assertEqual(session.ws.events[-1]["type"], "response.create")


class GatewayTests(unittest.TestCase):
    def test_browser_origin_cannot_call_local_gateway(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/api/tools/call",
                data=b"{}",
                headers={"Content-Type": "application/json", "Origin": "https://example.test"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(raised.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_production_shutdown_requires_control_token(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/api/shutdown"
        shutdown = Mock()
        try:
            with patch.dict(os.environ, {"VOICE_CUA_CONTROL_TOKEN": "owner-token"}), patch(
                "voice_cua.gateway.request_shutdown", shutdown
            ):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    urllib.request.urlopen(urllib.request.Request(url, data=b"", method="POST"), timeout=2)
                self.assertEqual(raised.exception.code, 403)
                request = urllib.request.Request(
                    url,
                    data=b"",
                    headers={"X-Voice-CUA-Control": "owner-token"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 200)
                shutdown.assert_called_once_with()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
