"""Island bus, tool-call facade, SSE latest-wins stream, and tool error paths."""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Keep test events out of the real ~/.cache/macos-cua activity log.
os.environ.setdefault(
    "VOICE_CUA_ACTIVITY_LOG",
    str(Path(tempfile.mkdtemp(prefix="voice-cua-test-")) / "activity.jsonl"),
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

import voice_cua.gateway  # noqa: E402

from voice_cua.island_facade import describe_tool, summarize_result  # noqa: E402
from voice_cua.island_state import ISLAND, IslandBus  # noqa: E402
from voice_cua.realtime_session import RealtimeSession  # noqa: E402


def _bus() -> IslandBus:
    return IslandBus()


class IslandBusTests(unittest.TestCase):
    def test_unsubscribe_stops_delivery(self) -> None:
        bus = _bus()
        seen: list = []
        bus.subscribe(seen.append)
        self.assertEqual(bus.listener_count(), 1)
        bus.unsubscribe(seen.append)
        self.assertEqual(bus.listener_count(), 0)
        bus.publish("acting", title="t")
        self.assertEqual(seen, [])

    def test_unsubscribe_unknown_fn_is_noop(self) -> None:
        bus = _bus()
        fn = lambda state: None  # noqa: E731
        bus.unsubscribe(fn)  # absent listener must not raise
        self.assertEqual(bus.listener_count(), 0)
        bus.subscribe(fn)
        other = lambda state: None  # noqa: E731
        bus.unsubscribe(other)
        self.assertEqual(bus.listener_count(), 1)


class DescribeToolTests(unittest.TestCase):
    def test_cua_state_with_app(self) -> None:
        self.assertEqual(
            describe_tool("cua_state", {"app": "Finder"}),
            ("acting", "Reading Finder", "", "Finder"),
        )

    def test_cua_state_without_app(self) -> None:
        self.assertEqual(
            describe_tool("cua_state", {}),
            ("acting", "Reading screen", "", ""),
        )

    def test_cua_act_with_step(self) -> None:
        self.assertEqual(
            describe_tool("cua_act", {"app": "Calculator", "step": "open Calculator"}),
            ("acting", "Using Calculator", "open Calculator", "Calculator"),
        )

    def test_cua_act_without_step_uses_op(self) -> None:
        self.assertEqual(
            describe_tool("cua_act", {"op": "click"}),
            ("acting", "Working", "click", ""),
        )

    def test_comet_begin(self) -> None:
        self.assertEqual(
            describe_tool("comet_begin", {}),
            ("acting", "Opening website", "Comet", "Comet"),
        )

    def test_secrets_get(self) -> None:
        self.assertEqual(
            describe_tool("secrets_get", {}),
            ("secrets", "Using Keychain", "get", ""),
        )

    def test_unknown_tool(self) -> None:
        self.assertEqual(
            describe_tool("unknown_tool", {}),
            ("acting", "Working", "unknown tool", ""),
        )

    def test_long_step_truncated_with_ellipsis(self) -> None:
        long_step = "s" * 100
        kind, _title, detail, _app = describe_tool("cua_act", {"step": long_step})
        self.assertEqual(kind, "acting")
        self.assertEqual(len(detail), 60)
        self.assertTrue(detail.endswith("…"))


class SummarizeResultTests(unittest.TestCase):
    def test_ok_with_summary(self) -> None:
        self.assertEqual(
            summarize_result("cua_act", {"ok": True, "summary": "Calculator opened"}),
            ("done", "Done", "Calculator opened"),
        )

    def test_ok_without_summary_uses_name(self) -> None:
        self.assertEqual(
            summarize_result("cua_act", {"ok": True}),
            ("done", "Done", "cua act ok"),
        )

    def test_failure_uses_first_error_line(self) -> None:
        self.assertEqual(
            summarize_result("cua_state", {"ok": False, "error": "aa\nbb"}),
            ("error", "Failed", "aa"),
        )

    def test_non_dict_result(self) -> None:
        self.assertEqual(summarize_result("cua_act", "junk"), ("error", "Failed", "no result"))


class SSEStreamTests(unittest.TestCase):
    def test_stream_latest_wins_and_leak_free(self) -> None:
        bus = IslandBus()
        server = None
        thread = None
        try:
            with (
                patch.object(voice_cua.gateway, "SSE_HEARTBEAT_S", 0.2),
                patch.object(voice_cua.gateway, "ISLAND", bus),
            ):
                server = voice_cua.gateway.ThreadingHTTPServer(
                    ("127.0.0.1", 0), voice_cua.gateway.Handler
                )
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                port = server.server_port

                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                conn.request("GET", "/api/island/stream")
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                self.assertTrue(resp.headers["Content-Type"].startswith("text/event-stream"))
                self.assertEqual(resp.headers["X-Voice-CUA"], "island")

                def read_data_line() -> dict:
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        raw = resp.readline()
                        if not raw:
                            raise AssertionError("SSE stream closed unexpectedly")
                        line = raw.decode("utf-8")
                        if line.startswith("data: "):
                            return json.loads(line[len("data: "):])
                    raise AssertionError("timed out waiting for SSE data line")

                initial = read_data_line()
                self.assertEqual(initial.get("kind"), "idle")

                # Tight loop — latest-wins should coalesce 30 publishes.
                for i in range(30):
                    bus.publish("acting", title=f"s{i}")

                found = False
                while True:
                    data = read_data_line()
                    if data.get("title") == "s29":
                        found = True
                        break

                self.assertTrue(found)
                # resp owns the socket makefile — close it too so the fd
                # actually closes and the server's next write errors out.
                resp.close()
                conn.close()

                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and bus.listener_count() != 0:
                    time.sleep(0.05)
                self.assertEqual(bus.listener_count(), 0)
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            if thread is not None:
                thread.join(timeout=2)

    def test_real_island_module_bus_untouched_by_gateway_patch(self) -> None:
        # gateway patching only its own ISLAND; module singleton stays the same object.
        self.assertIsInstance(ISLAND, IslandBus)


class RunToolIslandTests(unittest.TestCase):
    """RealtimeSession._run_tool island publishes + error resilience."""

    @staticmethod
    def _make_session() -> RealtimeSession:
        settings = {
            "realtime_model": "gpt-live-1",
            "realtime_voice": "alloy",
            "eagerness": "balanced",
            "mic_profile": "near_field",
        }
        with patch("voice_cua.voice_settings.load_settings", return_value=settings):
            session = RealtimeSession(api_key="test", enable_audio=False)
        session.ws = _RecordingWS()
        return session

    def _run(self, session: RealtimeSession, dispatch_result, *, raw_args: str) -> None:
        published: list[tuple] = []

        def record_publish(kind, **kwargs):
            published.append((kind, kwargs))

        with (
            patch("voice_cua.realtime_session.dispatch", dispatch_result),
            patch("voice_cua.realtime_session.island_publish", side_effect=record_publish),
            patch.object(session, "_wait_for_responses_idle", return_value=True),
            patch.object(session, "_send_response_create"),
        ):
            session._run_tool(
                "call-1",
                "cua_act",
                raw_args,
                request_id="req-1",
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and session._inflight_tools != 0:
                time.sleep(0.01)
        return published

    def test_success_publishes_acting_then_done(self) -> None:
        session = self._make_session()

        def good_dispatch(name, args):
            return {"ok": True, "summary": "Calculator opened"}

        published = self._run(
            session, good_dispatch, raw_args='{"app": "Calculator", "step": "open Calculator"}'
        )
        self.assertEqual(session._inflight_tools, 0)
        self.assertEqual([kind for kind, _kw in published], ["acting", "done"])
        self.assertEqual(published[0][1], {"title": "Using Calculator", "detail": "open Calculator", "app": "Calculator", "step": "cua_act"})
        self.assertEqual(
            published[1][1],
            {"title": "Done", "detail": "Calculator opened", "app": "Calculator"},
        )
        outputs = [
            e for e in session.ws.events if e.get("type") == "conversation.item.create"
        ]
        self.assertEqual(len(outputs), 1)
        output = json.loads(outputs[0]["item"]["output"])
        self.assertTrue(output.get("ok"))

    def test_dispatch_exception_still_sends_output(self) -> None:
        session = self._make_session()

        def bad_dispatch(name, args):
            raise RuntimeError("boom")

        published = self._run(session, bad_dispatch, raw_args="{}")
        self.assertEqual(session._inflight_tools, 0)
        self.assertEqual([kind for kind, _kw in published][-1], "error")
        last = published[-1][1]
        self.assertEqual(last.get("detail"), "boom")
        outputs = [e for e in session.ws.events if e.get("type") == "conversation.item.create"]
        self.assertEqual(len(outputs), 1)
        output = json.loads(outputs[0]["item"]["output"])
        self.assertFalse(output.get("ok"))
        self.assertEqual(output.get("error"), "boom")


class _RecordingWS:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def send(self, raw: str) -> None:
        self.events.append(json.loads(raw))

    def close(self) -> None:
        self.events.append({"type": "__closed__"})


class SummarizeFallbackTests(unittest.TestCase):
    def test_ok_without_summary_uses_action_fallback(self) -> None:
        from voice_cua.island_facade import summarize_result

        self.assertEqual(
            summarize_result("cua_act", {"ok": True}, fallback="open Calculator"),
            ("done", "Done", "open Calculator"),
        )
        self.assertEqual(
            summarize_result("cua_act", {"ok": True, "summary": "Calculator opened"}, fallback="x"),
            ("done", "Done", "Calculator opened"),
        )



class IslandCopyContractTests(unittest.TestCase):
    def test_title_cap_moves_overflow_to_detail(self):
        from voice_cua.island_copy import MAX_TITLE, compact

        title, detail = compact("Searching for the best pizza places near me", "Comet")
        self.assertLessEqual(len(title), MAX_TITLE)
        self.assertEqual(title, "Searching for the best pizza")
        self.assertEqual(detail, "places near me · Comet")
        self.assertNotIn("…", title)
        self.assertEqual(compact("Opening netflix.com", "Comet"), ("Opening netflix.com", "Comet"))

    def test_error_mapping_and_benign(self):
        from voice_cua.island_copy import human_error

        self.assertIsNone(human_error("Audio content of 3200ms is already shorter than 4000ms"))
        self.assertEqual(human_error("label not found", target="Calculator"), "Couldn't click Calculator")
        self.assertEqual(human_error("websocket connection closed"), "Connection dropped, reconnecting")
        self.assertEqual(human_error("KeyError: 'x' at line 3"), "Missed that, retrying")

    def test_bus_maps_errors_and_drops_benign(self):
        from voice_cua.island_state import IslandBus

        bus = IslandBus()
        bus.publish("listening", title="Listening")
        self.assertIsNone(bus.publish("error", title="Realtime error",
                                      detail="Audio content of 5ms is already shorter than 9ms"))
        self.assertEqual(bus.state.kind, "listening")
        bus.publish("error", title="Failed", detail="RPC -32000 raw trace")
        self.assertEqual(bus.state.title, "Missed that, retrying")
        self.assertEqual(bus.state.detail, "")
        snap = bus.publish("acting", title="Searching for the best pizza places near me", expanded=True)
        self.assertEqual(snap.title, "Searching for the best pizza")
        self.assertTrue(snap.expanded)

    def test_describe_tool_verb_target(self):
        from voice_cua.island_facade import describe_tool

        self.assertEqual(describe_tool("comet_begin", {"url": "https://www.netflix.com/"})[1], "Opening netflix.com")
        self.assertEqual(describe_tool("cua_state", {"app": "Calculator"})[1], "Reading Calculator")
        self.assertEqual(describe_tool("comet_act", {"actions": [{"type": "page_context"}]})[1], "Reading page")
        for name, args in [("cua_act", {"app": "System Settings", "op": "click", "step": "x" * 90}),
                           ("comet_act", {"actions": [{"type": "click_text", "text": "Sign in to your account now"}]})]:
            self.assertLessEqual(len(describe_tool(name, args)[1]), 28)

    def test_active_apps_recency_order(self):
        from voice_cua import island_facade as f

        f._ACTIVE_APPS.clear()
        with patch.object(f, "use_remote", return_value=False):
            for app in ["Calculator", "Comet", "Calculator"]:
                f.island_publish("acting", title="x", app=app)
        self.assertEqual(f._ACTIVE_APPS, ["Comet", "Calculator"])
        f._ACTIVE_APPS.clear()


class ParentWatchTests(unittest.TestCase):
    """CUAService restart must not orphan the voice helper (old code, mic open)."""

    def test_parent_alive_detects_reparent_and_death(self):
        from voice_cua.parent_watch import parent_alive

        me = os.getpid()
        self.assertTrue(parent_alive(me, original_ppid=me, getppid=lambda: me))
        self.assertFalse(parent_alive(me, original_ppid=me, getppid=lambda: 1), "re-parented to launchd")
        self.assertFalse(parent_alive(999999, original_ppid=999999, getppid=lambda: 999999), "parent pid gone")

    def test_watch_fires_on_gone_once_parent_disappears(self):
        from voice_cua.parent_watch import start_parent_watch

        ppid = {"v": 4242}
        fired = threading.Event()
        with patch("os.kill") as kill:
            kill.side_effect = lambda pid, sig: None
            t = start_parent_watch(parent_pid=4242, interval=0.02, on_gone=fired.set, getppid=lambda: ppid["v"])
            self.assertIsNotNone(t)
            time.sleep(0.08)
            self.assertFalse(fired.is_set(), "parent alive → keep running")
            ppid["v"] = 1  # CUAService died, launchd adopted us
            self.assertTrue(fired.wait(1.0))

    def test_no_watch_without_supervisor(self):
        from voice_cua.parent_watch import start_parent_watch

        self.assertIsNone(start_parent_watch(parent_pid=0, getppid=lambda: 1))


class AgentIslandTests(unittest.TestCase):
    def test_agent_approval_takes_pill_and_resolves(self):
        from voice_cua.island_state import IslandBus

        bus = IslandBus()
        bus.publish("listening", title="Listening",
                    agents=[{"id": "a1", "name": "Codex", "app": "Codex", "status": "running"}],
                    agent_approval={"id": "ap1", "agent": "Codex", "title": "Approve migration?"})
        self.assertEqual(bus.state.agent_approval["id"], "ap1")
        bus.publish("thinking", title="Thinking")  # Samantha state changes; approval persists
        self.assertEqual(bus.state.agent_approval["id"], "ap1")
        self.assertEqual(len(bus.state.agents), 1)
        self.assertFalse(bus.resolve_agent_approval("nope", True))
        self.assertTrue(bus.resolve_agent_approval("ap1", True))
        self.assertIsNone(bus.state.agent_approval)

    def test_gateway_publish_null_agent_approval_clears(self):
        bus = IslandBus()
        bus.publish("listening", title="Listening", agent_approval={"id": "ap1", "agent": "Codex", "title": "Ok?"})
        with patch.object(voice_cua.gateway, "ISLAND", bus):
            server = voice_cua.gateway.ThreadingHTTPServer(("127.0.0.1", 0), voice_cua.gateway.Handler)
            th = threading.Thread(target=server.serve_forever, daemon=True)
            th.start()
            try:
                def post(body):
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
                    conn.request("POST", "/api/island/publish", json.dumps(body), {"Content-Type": "application/json"})
                    conn.getresponse().read()
                    conn.close()
                post({"kind": "thinking", "title": "Thinking"})  # key absent keeps it
                self.assertEqual(bus.state.agent_approval["id"], "ap1")
                post({"kind": "thinking", "title": "Thinking", "agent_approval": None})  # explicit null clears
                self.assertIsNone(bus.state.agent_approval)
            finally:
                server.shutdown()
                server.server_close()

    def test_step_progress_fields(self):
        from voice_cua.island_state import IslandBus

        snap = IslandBus().publish("acting", title="Opening netflix.com", app="Comet", step_index=2, step_total=5)
        self.assertEqual((snap.step_index, snap.step_total), (2, 5))


def setUpModule() -> None:  # loop tests run on the provider-neutral pass-through
    import loop_harness

    loop_harness.install()


def tearDownModule() -> None:
    import loop_harness

    loop_harness.uninstall()


if __name__ == "__main__":
    unittest.main()
