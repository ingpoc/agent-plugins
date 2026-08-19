import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import sys
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
import zlib
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock


_PREV_SOCKET_PATH = None
_ORIGINAL_RESTART = None
_RESTART_GUARD = None


def _structured_envelope(structured, text="x"):
    return {
        "ok": True,
        "result": {
            "content": [{"text": text, "type": "text"}],
            "structuredContent": structured,
        },
    }


def _envelope_line(structured, text="x"):
    return json.dumps(_structured_envelope(structured, text)).encode() + b"\n"


def _assert_no_tool_call_spawn(run_mock):
    for call in run_mock.call_args_list:
        argv = list(call.args[0]) if call.args else []
        if "call" in argv:
            raise AssertionError(f"tool call spawned subprocess: {argv}")


SCRIPT = Path(__file__).parents[1] / "scripts" / "macos-cua.py"
SPEC = importlib.util.spec_from_file_location("macos_cua", SCRIPT)
macos_cua = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(macos_cua)

DISPLAYS_SCRIPT = Path(__file__).parents[1] / "scripts" / "displays.py"
DISPLAYS_SPEC = importlib.util.spec_from_file_location("displays", DISPLAYS_SCRIPT)
displays = importlib.util.module_from_spec(DISPLAYS_SPEC)
DISPLAYS_SPEC.loader.exec_module(displays)

OPERATOR_SCRIPT = Path(__file__).parents[1] / "scripts" / "operator_ui.py"
OPERATOR_SPEC = importlib.util.spec_from_file_location("operator_ui", OPERATOR_SCRIPT)
operator_ui = importlib.util.module_from_spec(OPERATOR_SPEC)
OPERATOR_SPEC.loader.exec_module(operator_ui)

INSTALL_SCRIPT = Path(__file__).parents[1] / "scripts" / "install_harness.py"
INSTALL_SPEC = importlib.util.spec_from_file_location("install_harness", INSTALL_SCRIPT)
install_harness = importlib.util.module_from_spec(INSTALL_SPEC)
INSTALL_SPEC.loader.exec_module(install_harness)

WORKFLOW_SCRIPT = Path(__file__).parents[1] / "scripts" / "workflow.py"
WORKFLOW_SPEC = importlib.util.spec_from_file_location("workflow", WORKFLOW_SCRIPT)
workflow = importlib.util.module_from_spec(WORKFLOW_SPEC)
WORKFLOW_SPEC.loader.exec_module(workflow)

_ORIGINAL_RESTART = macos_cua._restart_driver_daemon


def setUpModule():
    global _PREV_SOCKET_PATH, _RESTART_GUARD
    _PREV_SOCKET_PATH = os.environ.get("MACOS_CUA_DRIVER_SOCKET")
    handle, path = tempfile.mkstemp(prefix="macos-cua-test-", suffix=".sock")
    os.close(handle)
    os.unlink(path)
    os.environ["MACOS_CUA_DRIVER_SOCKET"] = path
    _RESTART_GUARD = mock.patch.object(
        macos_cua, "_restart_driver_daemon", return_value=False
    )
    _RESTART_GUARD.start()


def tearDownModule():
    if _RESTART_GUARD is not None:
        _RESTART_GUARD.stop()
    path = os.environ.get("MACOS_CUA_DRIVER_SOCKET")
    if _PREV_SOCKET_PATH is None:
        os.environ.pop("MACOS_CUA_DRIVER_SOCKET", None)
    else:
        os.environ["MACOS_CUA_DRIVER_SOCKET"] = _PREV_SOCKET_PATH
    if path and path != _PREV_SOCKET_PATH:
        try:
            os.unlink(path)
        except OSError:
            pass


def _allow_daemon_restart():
    return mock.patch.object(
        macos_cua, "_restart_driver_daemon", side_effect=_ORIGINAL_RESTART
    )


class DriverTransportTests(unittest.TestCase):
    def setUp(self):
        macos_cua.reset_driver_socket()
        macos_cua.reset_driver_call_stats()
        macos_cua.telemetry_reset()

    def tearDown(self):
        macos_cua.reset_driver_socket()

    def test_timed_out_rpc_restarts_the_daemon_and_retries_once(self):
        timed_out = _FakeDriverSocket(recv_error=TimeoutError("timed out"))
        recovered = _FakeDriverSocket(chunks=[_envelope_line({"ok": True})])
        with _allow_daemon_restart():
            with mock.patch.object(
                macos_cua,
                "_connect_driver_socket",
                side_effect=[timed_out, recovered],
            ):
                with mock.patch.object(
                    macos_cua.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
                ) as run:
                    result = macos_cua.call_driver("get_window_state", timeout=5)

        self.assertTrue(result["ok"])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][:3], ["launchctl", "kickstart", "-k"])
        _assert_no_tool_call_spawn(run)

    def test_explicit_human_success_sentinel_is_normalized(self):
        payload = {"ok": True, "message": "Set AXValue on [1] AXTextArea."}
        fake = _FakeDriverSocket(chunks=[_envelope_line(payload)])
        with mock.patch.object(macos_cua, "_connect_driver_socket", return_value=fake):
            with mock.patch.object(macos_cua.subprocess, "run") as run:
                result = macos_cua.call_driver("set_value", {"pid": 42})
        self.assertEqual(result, payload)
        run.assert_not_called()

    def test_screen_region_capture_retries_once_after_transient_failure(self):
        header = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            + (230).to_bytes(4, "big")
            + (408).to_bytes(4, "big")
        )
        with (
            mock.patch.object(
                macos_cua.subprocess,
                "run",
                side_effect=[SimpleNamespace(returncode=1), SimpleNamespace(returncode=0)],
            ) as run,
            mock.patch("builtins.open", return_value=io.BytesIO(header)),
            mock.patch.object(macos_cua.time, "sleep") as sleep,
        ):
            result = macos_cua._capture_screen_region(
                {},
                {"x": 10, "y": 20, "width": 230, "height": 408},
                "/tmp/retry-proof.png",
                "test",
            )

        self.assertEqual((result["screenshot_width"], result["screenshot_height"]), (230, 408))
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(0.05)

    def test_arbitrary_non_json_driver_output_fails_closed(self):
        fake = _FakeDriverSocket(chunks=[b"operation probably worked\n"])
        with mock.patch.object(macos_cua, "_connect_driver_socket", return_value=fake):
            with mock.patch.object(macos_cua.subprocess, "run") as run:
                result = macos_cua.call_driver("set_value", {"pid": 42})
        self.assertIn("error", result)
        self.assertEqual(result["error"], "cua-driver socket returned invalid JSON")
        run.assert_not_called()

    def test_right_click_legacy_success_grammar_is_tool_scoped(self):
        accepted_sock = _FakeDriverSocket(
            chunks=[
                _envelope_line(
                    {
                        "ok": True,
                        "message": 'Shown menu for [1] AXTextArea "" (AXShowMenu).',
                    }
                )
            ]
        )
        rejected_sock = _FakeDriverSocket(
            chunks=[
                json.dumps(
                    {
                        "ok": True,
                        "result": {"content": [{"text": "x", "type": "text"}]},
                    }
                ).encode()
                + b"\n"
            ]
        )
        with mock.patch.object(
            macos_cua,
            "_connect_driver_socket",
            side_effect=[accepted_sock, rejected_sock],
        ):
            with mock.patch.object(macos_cua.subprocess, "run") as run:
                accepted = macos_cua.call_driver("right_click", {"pid": 42})
                rejected = macos_cua.call_driver("click", {"pid": 42})
        self.assertTrue(accepted["ok"])
        self.assertIn("error", rejected)
        run.assert_not_called()

    def test_right_click_pixel_fallback_success_grammar_is_normalized(self):
        payload = {
            "ok": True,
            "message": (
                'Right-clicked [5] AXButton "" at element center (2714, 618) '
                "(pixel right-click; element advertises no AXShowMenu)."
            ),
        }
        fake = _FakeDriverSocket(chunks=[_envelope_line(payload)])
        with mock.patch.object(macos_cua, "_connect_driver_socket", return_value=fake):
            with mock.patch.object(macos_cua.subprocess, "run") as run:
                result = macos_cua.call_driver("right_click", {"pid": 42})
        self.assertTrue(result["ok"])
        run.assert_not_called()


class BrowserCoexistenceTests(unittest.TestCase):
    def tearDown(self):
        macos_cua._HERMES_RUNTIME_CLAIM = None

    def _guard_path(self, directory):
        path = Path(directory) / "check-cua-coexistence.py"
        path.write_text("# test guard\n")
        return path

    def test_guard_allows_disjoint_native_target(self):
        packet = {
            "version": "coexistence-v1",
            "safe": True,
            "managed_browser": False,
            "target_pid": 10,
            "intent": "native-app",
            "reason": "disjoint-native-app",
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            macos_cua, "HERMES_CUA_GUARD", self._guard_path(directory)
        ), mock.patch.object(
            macos_cua.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=json.dumps(packet)),
        ):
            result = macos_cua.check_browser_coexistence(10)
        self.assertTrue(result["safe"])
        self.assertFalse(result["managed_browser"])

    def test_guard_block_is_preserved_as_structured_result(self):
        packet = {
            "version": "coexistence-v1",
            "safe": False,
            "managed_browser": True,
            "target_pid": 99,
            "intent": "chrome-admin",
            "error_code": "HERMES_RUNTIME_BUSY",
            "error": "active lease",
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            macos_cua, "HERMES_CUA_GUARD", self._guard_path(directory)
        ), mock.patch.object(
            macos_cua.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=2, stdout=json.dumps(packet)),
        ):
            result = macos_cua.check_browser_coexistence(
                99, "chrome-admin", "agent-a"
            )
        self.assertFalse(result["safe"])
        self.assertEqual(result["error_code"], "HERMES_RUNTIME_BUSY")

    def test_failed_guard_only_blocks_the_managed_user_data_dir(self):
        with mock.patch.object(
            macos_cua, "HERMES_CUA_GUARD", Path("/missing/hermes-guard")
        ), mock.patch.object(
            macos_cua,
            "_process_command",
            return_value=f"Google Chrome --user-data-dir={macos_cua.HERMES_USER_DATA_DIR}",
        ):
            managed = macos_cua.check_browser_coexistence(99, "chrome-admin")
        self.assertFalse(managed["safe"])
        self.assertEqual(managed["error_code"], "HERMES_RUNTIME_UNAVAILABLE")

        with mock.patch.object(
            macos_cua, "HERMES_CUA_GUARD", Path("/missing/hermes-guard")
        ), mock.patch.object(
            macos_cua, "_process_command", return_value="/System/Applications/Calculator"
        ):
            disjoint = macos_cua.check_browser_coexistence(10)
        self.assertTrue(disjoint["safe"])
        self.assertEqual(disjoint["reason"], "disjoint-native-app")

    def test_enforcement_holds_and_releases_atomic_claim(self):
        args = SimpleNamespace(
            browser_intent="chrome-admin",
            browser_session_id=None,
            browser_claim_token=None,
        )
        acquired = {
            "version": "coexistence-v1",
            "safe": True,
            "target_pid": 99,
            "intent": "chrome-admin",
            "claim_id": "claim-1",
            "claim_token": "claim-secret",
        }
        released = {
            "version": "coexistence-v1",
            "safe": True,
            "released": True,
            "claim_id": "claim-1",
        }
        with mock.patch.object(
            macos_cua, "check_browser_coexistence", return_value=acquired
        ) as check:
            public = macos_cua._enforce_browser_coexistence(99, args)
        self.assertNotIn("claim_token", public)
        self.assertEqual(macos_cua._HERMES_RUNTIME_CLAIM["token"], "claim-secret")
        self.assertTrue(check.call_args.kwargs["acquire"])

        with mock.patch.object(
            macos_cua.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout=json.dumps(released)),
        ):
            result = macos_cua._release_browser_coexistence_claim()
        self.assertTrue(result["released"])
        self.assertIsNone(macos_cua._HERMES_RUNTIME_CLAIM)


class WorkflowPreflightTests(unittest.TestCase):
    def test_generic_preflight_does_not_focus_or_launch_an_app(self):
        source = (Path(__file__).parents[1] / "scripts" / "workflow.py").read_text()
        preflight = source.split("def cmd_preflight", 1)[1].split("def cmd_smoke", 1)[0]
        self.assertIn('ap.add_argument("--app")', source)
        self.assertIn("smoke requires --app", source)
        self.assertNotIn("cursor-demo", source)
        self.assertNotIn('args.app or "Calculator"', source)
        self.assertNotIn("ACTIONS", preflight)
        self.assertNotIn("focus", preflight)

    def test_permissions_reject_only_known_capture_failure(self):
        granted_but_uncapturable = {
            "accessibility": True,
            "screen_recording": True,
            "screen_recording_capturable": False,
        }
        self.assertFalse(workflow.permissions_ready(granted_but_uncapturable))
        self.assertTrue(
            workflow.permissions_ready(
                {**granted_but_uncapturable, "screen_recording_capturable": True}
            )
        )
        self.assertTrue(
            workflow.permissions_ready(
                {**granted_but_uncapturable, "screen_recording_capturable": None}
            )
        )

    def test_compact_result_is_single_line(self):
        stream = io.StringIO()
        with redirect_stdout(stream):
            workflow._print_result({"ready": True})
        self.assertEqual(stream.getvalue(), '{"ready":true}\n')


class WindowResolutionTests(unittest.TestCase):
    def test_pid_selector_resolves_exact_same_bundle_instance(self):
        def running_app(pid, active):
            app = mock.Mock()
            app.processIdentifier.return_value = pid
            app.localizedName.return_value = "Google Chrome"
            app.bundleIdentifier.return_value = "com.google.Chrome"
            app.isActive.return_value = active
            return app

        workspace = mock.Mock()
        workspace.runningApplications.return_value = [
            running_app(1659, True),
            running_app(32734, False),
        ]
        nsworkspace = mock.Mock()
        nsworkspace.sharedWorkspace.return_value = workspace
        with mock.patch.dict(
            sys.modules,
            {"AppKit": SimpleNamespace(NSWorkspace=nsworkspace)},
        ):
            exact = macos_cua._running_app_identity("pid:32734")
            named = macos_cua._running_app_identity("Google Chrome")

        self.assertEqual(exact["pid"], 32734)
        self.assertEqual(exact["bundle_id"], "com.google.Chrome")
        self.assertEqual(named["pid"], 1659)

    def test_pid_selector_returns_none_for_non_running_process(self):
        workspace = mock.Mock()
        workspace.runningApplications.return_value = []
        nsworkspace = mock.Mock()
        nsworkspace.sharedWorkspace.return_value = workspace
        with mock.patch.dict(
            sys.modules,
            {"AppKit": SimpleNamespace(NSWorkspace=nsworkspace)},
        ):
            self.assertIsNone(macos_cua._running_app_identity("pid:99999"))

    def test_inactive_running_app_is_reactivated_before_cache_is_trusted(self):
        identity = {
            "pid": 899,
            "name": "LikemindedMac",
            "bundle_id": "com.likeminded.mac",
            "active": False,
        }
        windows = {
            "windows": [
                {
                    "pid": 899,
                    "window_id": 99,
                    "app_name": "Likeminded",
                    "title": "Likeminded",
                    "bounds": {"width": 1080, "height": 760},
                }
            ],
            "method": "quartz",
        }
        with (
            mock.patch.object(macos_cua, "_running_app_identity", return_value=identity),
            mock.patch.object(macos_cua, "_pid_alive", return_value=True),
            mock.patch.object(
                macos_cua,
                "_read_cache",
                return_value={"pid": 899, "window_id": 98},
            ) as read_cache,
            mock.patch.object(
                macos_cua, "_ax_window_candidates", return_value=[]
            ) as ax_windows,
            mock.patch.object(macos_cua, "list_windows", return_value=windows),
            mock.patch.object(macos_cua, "_write_cache"),
            mock.patch.object(
                macos_cua,
                "launch_or_activate",
                return_value={"ok": True},
            ) as activate,
            mock.patch.object(macos_cua.time, "sleep"),
        ):
            result = macos_cua.resolve_app(
                "com.likeminded.mac",
                launch_if_missing=False,
                activate_if_inactive=True,
            )

        activate.assert_called_once_with("com.likeminded.mac")
        read_cache.assert_not_called()
        ax_windows.assert_not_called()
        self.assertEqual(result, (899, 99, "LikemindedMac", None))

    def test_running_app_without_window_receives_reopen_event(self):
        identity = {
            "pid": 899,
            "name": "TextEdit",
            "bundle_id": "com.apple.TextEdit",
            "active": True,
        }
        target = {
            "pid": 899,
            "window_id": 99,
            "title": "Untitled",
            "bounds": {"width": 600, "height": 400},
            "ax_main": True,
            "ax_minimized": False,
        }
        calls = {"n": 0}

        def windows():
            calls["n"] += 1
            return {"windows": [] if calls["n"] == 1 else [target]}

        def ax_candidates(*_a, **_k):
            return [] if calls["n"] <= 1 else [target]

        with (
            mock.patch.object(macos_cua, "_running_app_identity", return_value=identity),
            mock.patch.object(macos_cua, "_pid_alive", return_value=True),
            mock.patch.object(macos_cua, "_read_cache", return_value=None),
            mock.patch.object(macos_cua, "list_windows", side_effect=windows),
            mock.patch.object(
                macos_cua, "_ax_window_candidates", side_effect=ax_candidates
            ),
            mock.patch.object(
                macos_cua, "_reopen_running_identity", return_value={"ok": True}
            ) as reopen,
            mock.patch.object(macos_cua, "launch_or_activate") as launch,
            mock.patch.object(macos_cua, "_write_cache"),
            mock.patch.object(macos_cua.time, "sleep"),
        ):
            result = macos_cua.resolve_app("TextEdit")

        self.assertEqual(result, (899, 99, "TextEdit", None))
        reopen.assert_called_once_with(899)
        launch.assert_not_called()

    def test_dead_identity_pid_launches_instead_of_reopening_stale_unix_id(self):
        dead = {
            "pid": 8434,
            "name": "AnyApp",
            "bundle_id": "com.example.anyapp",
            "active": True,
        }
        live = {
            "pid": 8513,
            "name": "AnyApp",
            "bundle_id": "com.example.anyapp",
            "active": True,
        }
        window = {
            "pid": 8513,
            "window_id": 12,
            "title": "AnyApp",
            "bounds": {"width": 400, "height": 400},
            "ax_main": True,
            "ax_minimized": False,
        }
        def identity(_name=None):
            identity.n = getattr(identity, "n", 0) + 1
            return dead if identity.n == 1 else live

        def windows():
            return {"windows": [] if getattr(identity, "n", 0) <= 1 else [window]}

        def ax_candidates(*_a, **_k):
            return [] if getattr(identity, "n", 0) <= 1 else [window]

        with (
            mock.patch.object(
                macos_cua, "_running_app_identity", side_effect=identity
            ),
            mock.patch.object(
                macos_cua, "_pid_alive", side_effect=lambda pid: pid != 8434
            ),
            mock.patch.object(macos_cua, "clear_resolution_cache") as clear,
            mock.patch.object(macos_cua, "_read_cache", return_value=None),
            mock.patch.object(macos_cua, "list_windows", side_effect=windows),
            mock.patch.object(
                macos_cua, "_ax_window_candidates", side_effect=ax_candidates
            ),
            mock.patch.object(macos_cua, "_reopen_running_identity") as reopen,
            mock.patch.object(
                macos_cua, "launch_or_activate", return_value={"ok": True}
            ) as launch,
            mock.patch.object(macos_cua, "_write_cache"),
            mock.patch.object(macos_cua.time, "sleep"),
        ):
            result = macos_cua.resolve_app("AnyApp")

        self.assertEqual(result, (8513, 12, "AnyApp", None))
        reopen.assert_not_called()
        launch.assert_called_once_with("AnyApp")
        clear.assert_called()

    def test_launch_or_activate_ignores_dead_identity_pid(self):
        dead = {
            "pid": 8434,
            "name": "AnyApp",
            "bundle_id": "com.example.anyapp",
            "active": True,
        }
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(macos_cua, "_running_app_identity", return_value=dead),
            mock.patch.object(macos_cua, "_pid_alive", return_value=False),
            mock.patch.object(macos_cua, "_activate_running_identity") as activate,
            mock.patch.object(
                macos_cua,
                "_resolve_bundle_id",
                return_value=("com.example.anyapp", "AnyApp"),
            ),
            mock.patch.object(macos_cua.subprocess, "run", return_value=completed),
        ):
            result = macos_cua.launch_or_activate("AnyApp")

        activate.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "bundle+osascript")

    def test_running_app_activation_avoids_open_and_applescript(self):
        identity = {
            "pid": 899,
            "name": "LikemindedMac",
            "bundle_id": "com.likeminded.mac",
            "active": False,
        }
        with (
            mock.patch.object(
                macos_cua, "_running_app_identity", return_value=identity
            ),
            mock.patch.object(macos_cua, "_pid_alive", return_value=True),
            mock.patch.object(
                macos_cua,
                "_activate_running_identity",
                return_value={"ok": True, "method": "nsworkspace", "pid": 899},
            ) as activate,
            mock.patch.object(macos_cua.subprocess, "run") as run,
        ):
            result = macos_cua.launch_or_activate("com.likeminded.mac")

        activate.assert_called_once_with(identity)
        run.assert_not_called()
        self.assertEqual(result["method"], "nsworkspace")
        self.assertEqual(result["pid"], 899)

    def test_running_app_activation_dispatch_does_not_require_is_active(self):
        app = mock.Mock()
        app.processIdentifier.return_value = 899
        app.activateWithOptions_.return_value = True
        workspace = mock.Mock()
        workspace.runningApplications.return_value = [app]
        appkit = SimpleNamespace(
            NSApplicationActivateAllWindows=1,
            NSApplicationActivateIgnoringOtherApps=2,
            NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace),
        )
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            mock.patch.dict(macos_cua.sys.modules, {"AppKit": appkit}),
            mock.patch.object(
                macos_cua.subprocess, "run", return_value=completed
            ) as run,
        ):
            result = macos_cua._activate_running_identity({"pid": 899})

        app.unhide.assert_called_once_with()
        app.activateWithOptions_.assert_called_once_with(3)
        app.isActive.assert_not_called()
        self.assertEqual(run.call_args.kwargs["timeout"], 3)
        self.assertIs(run.call_args.kwargs["stdin"], macos_cua.subprocess.DEVNULL)
        self.assertIn("unix id is 899", run.call_args.args[0][-1])
        self.assertEqual(
            result,
            {
                "ok": True,
                "method": "nsworkspace+system-events-dispatch",
                "pid": 899,
            },
        )

    def test_running_pid_activation_falls_back_to_system_events(self):
        workspace = mock.Mock()
        workspace.runningApplications.return_value = []
        appkit = SimpleNamespace(
            NSApplicationActivateAllWindows=1,
            NSApplicationActivateIgnoringOtherApps=2,
            NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace),
        )
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            mock.patch.dict(macos_cua.sys.modules, {"AppKit": appkit}),
            mock.patch.object(
                macos_cua.subprocess, "run", return_value=completed
            ) as run,
        ):
            result = macos_cua._activate_running_identity({"pid": 899})

        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "system-events-pid-dispatch")
        self.assertIn("not registered", result["appkit_warning"])
        self.assertIn("unix id is 899", run.call_args.args[0][-1])

    def test_running_app_reopen_uses_bounded_bundle_dispatch(self):
        app = mock.Mock()
        app.processIdentifier.return_value = 899
        app.bundleIdentifier.return_value = "com.likeminded.mac"
        app.localizedName.return_value = "LikemindedMac"
        workspace = mock.Mock()
        workspace.runningApplications.return_value = [app]
        appkit = SimpleNamespace(
            NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace),
        )
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            mock.patch.dict(macos_cua.sys.modules, {"AppKit": appkit}),
            mock.patch.object(
                macos_cua.subprocess, "run", return_value=completed
            ) as run,
        ):
            result = macos_cua._reopen_running_identity(899)

        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "bundle-reopen")
        self.assertEqual(run.call_args.args[0], ["open", "-b", "com.likeminded.mac"])
        self.assertEqual(run.call_args.kwargs["timeout"], 5)
        self.assertIs(run.call_args.kwargs["stdin"], macos_cua.subprocess.DEVNULL)

    def test_new_app_launch_and_applescript_are_bounded(self):
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(macos_cua, "_running_app_identity", return_value=None),
            mock.patch.object(
                macos_cua,
                "_resolve_bundle_id",
                return_value=("com.example.app", "Example"),
            ),
            mock.patch.object(
                macos_cua.subprocess, "run", return_value=completed
            ) as run,
        ):
            result = macos_cua.launch_or_activate("Example")

        self.assertTrue(result["ok"])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(
            run.call_args_list[0].args[0],
            ["open", "-n", "-b", "com.example.app"],
        )
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["timeout"], 5)
            self.assertIs(call.kwargs["stdin"], macos_cua.subprocess.DEVNULL)

    def test_foreground_window_uses_short_bounded_driver_call(self):
        readiness = {"ok": True, "pid": 899, "window_id": 99, "duration_ms": 84}
        with (
            mock.patch.object(
                macos_cua,
                "call_driver",
                return_value={"ok": True},
            ) as call_driver,
            mock.patch.object(
                macos_cua,
                "_wait_for_foreground_readiness",
                return_value=readiness,
            ) as wait_ready,
        ):
            result = macos_cua.bring_resolved_window_to_front(899, 99)

        self.assertEqual(result, {"ok": True, "readiness": readiness})
        call_driver.assert_called_once_with(
            "bring_to_front",
            {"pid": 899, "window_id": 99},
            timeout=5,
        )
        wait_ready.assert_called_once_with(899, 99, timeout=1.5)

    def test_foreground_window_fails_closed_without_readiness(self):
        readiness = {"ok": False, "error": "foreground acknowledgement timed out"}
        with (
            mock.patch.object(
                macos_cua, "call_driver", return_value={"ok": True}
            ) as call_driver,
            mock.patch.object(
                macos_cua,
                "_wait_for_foreground_readiness",
                return_value=readiness,
            ),
            mock.patch.object(
                macos_cua,
                "_activate_running_identity",
                return_value={"error": "activation rejected"},
            ),
        ):
            result = macos_cua.bring_resolved_window_to_front(899, 99)

        self.assertEqual(result["error"], readiness["error"])
        self.assertEqual(result["readiness"], readiness)
        self.assertEqual(result["recovery"]["error"], "activation rejected")

    def test_foreground_window_recovers_once_via_authoritative_pid(self):
        missed = {"ok": False, "error": "foreground acknowledgement timed out"}
        ready = {"ok": True, "pid": 899, "window_id": 99, "duration_ms": 82}
        with (
            mock.patch.object(macos_cua, "call_driver", return_value={"ok": True}),
            mock.patch.object(
                macos_cua,
                "_wait_for_foreground_readiness",
                side_effect=[missed, ready],
            ) as wait_ready,
            mock.patch.object(
                macos_cua,
                "_activate_running_identity",
                return_value={"ok": True, "pid": 899},
            ) as activate,
        ):
            result = macos_cua.bring_resolved_window_to_front(899, 99)

        self.assertTrue(result["ok"])
        self.assertTrue(result["recovered"])
        self.assertEqual(result["initial_readiness"], missed)
        self.assertEqual(result["readiness"], ready)
        activate.assert_called_once_with({"pid": 899})
        self.assertEqual(wait_ready.call_count, 2)

    def test_foreground_window_surfaces_driver_timeout(self):
        with mock.patch.object(
            macos_cua,
            "call_driver",
            return_value={"error": "timed out"},
        ):
            result = macos_cua.bring_resolved_window_to_front(899, 99)

        self.assertIn("timed out", result["error"])

    def test_foreground_window_prefers_native_exact_window(self):
        with (
            mock.patch.object(macos_cua, "_pid_alive", return_value=True),
            mock.patch.object(
                macos_cua, "_activate_running_identity", return_value={"ok": True}
            ),
            mock.patch.object(
                macos_cua, "_raise_resolved_ax_window", return_value={"ok": True}
            ),
            mock.patch.object(
                macos_cua,
                "_wait_for_foreground_readiness",
                return_value={"ok": True},
            ),
            mock.patch.object(macos_cua, "call_driver") as driver,
        ):
            result = macos_cua.bring_resolved_window_to_front(899, 99)

        self.assertEqual(result["foreground_method"], "native_activation+ax_raise")
        driver.assert_not_called()

    def test_foreground_window_raises_exact_ax_window_before_process_recovery(self):
        missed = {"ok": False, "error": "foreground window is not AX-ready"}
        ready = {"ok": True, "window_id": 99}
        with (
            mock.patch.object(macos_cua, "call_driver", return_value={"ok": True}),
            mock.patch.object(
                macos_cua,
                "_wait_for_foreground_readiness",
                side_effect=[missed, ready],
            ),
            mock.patch.object(
                macos_cua,
                "_raise_resolved_ax_window",
                return_value={"ok": True, "path": "native_ax_raise"},
            ),
            mock.patch.object(macos_cua, "_activate_running_identity") as activate,
        ):
            result = macos_cua.bring_resolved_window_to_front(899, 99)

        self.assertTrue(result["ok"])
        self.assertEqual(result["recovery"]["path"], "native_ax_raise")
        activate.assert_not_called()

    def test_foreground_window_surfaces_driver_rejection(self):
        with mock.patch.object(
            macos_cua,
            "call_driver",
            return_value={"ok": False, "reason": "window unavailable"},
        ):
            result = macos_cua.bring_resolved_window_to_front(899, 99)

        self.assertIn("driver rejected", result["error"])
        self.assertFalse(result["driver"]["ok"])

    def test_foreground_readiness_requires_positive_window_ack(self):
        with (
            mock.patch.object(macos_cua, "_frontmost_pid", return_value=899),
            mock.patch.object(macos_cua, "list_windows", return_value={"windows": []}),
            mock.patch.object(macos_cua, "_ax_window_candidates", return_value=[]),
        ):
            result = macos_cua._wait_for_foreground_readiness(899, 99, timeout=0.12)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "foreground window is not AX-ready")
        self.assertEqual(result["candidate_count"], 0)

    def test_foreground_readiness_accepts_pid_specific_system_events_ack(self):
        candidate = {
            "window_id": 99,
            "ax_main": True,
            "ax_focused": True,
            "ax_minimized": False,
        }
        with (
            mock.patch.object(macos_cua, "_frontmost_pid", return_value=900),
            mock.patch.object(
                macos_cua, "_system_events_process_frontmost", return_value=True
            ),
            mock.patch.object(macos_cua, "list_windows", return_value={"windows": []}),
            mock.patch.object(
                macos_cua, "_ax_window_candidates", return_value=[candidate]
            ),
        ):
            result = macos_cua._wait_for_foreground_readiness(899, 99, timeout=0.1)

        self.assertTrue(result["ok"])
        self.assertEqual(result["foreground_method"], "system-events-pid")

    def test_driver_error_returns_without_retrying(self):
        with (
            mock.patch.object(
                macos_cua,
                "call_driver",
                return_value={"error": "AX request timed out"},
            ) as call_driver,
            mock.patch.object(macos_cua.time, "sleep") as sleep,
            mock.patch.dict(os.environ, {"MACOS_CUA_STATE_TIMEOUT": "7"}),
        ):
            result = macos_cua.snapshot(899, 99, retries=2)

        self.assertEqual(result, {"error": "AX request timed out"})
        call_driver.assert_called_once_with(
            "get_window_state",
            {
                "pid": 899,
                "window_id": 99,
                "max_elements": 20,
                "include_screenshot": False,
                "capture_mode": "ax",
            },
            timeout=7,
        )
        sleep.assert_not_called()

    def test_vision_snapshot_uses_driver_schema_and_output_path(self):
        with mock.patch.object(
            macos_cua,
            "call_driver",
            return_value={"tree_markdown": "AXApplication > AXMenuBar"},
        ) as call_driver:
            macos_cua.snapshot(
                899,
                99,
                mode="vision",
                retries=0,
                include_screenshot=True,
                screenshot_out_file="/tmp/vision.png",
            )

        call_driver.assert_called_once_with(
            "get_window_state",
            {
                "pid": 899,
                "window_id": 99,
                "max_elements": 20,
                "include_screenshot": True,
                "capture_mode": "vision",
                "screenshot_out_file": "/tmp/vision.png",
            },
            timeout=12,
        )

    def test_snapshot_retries_until_required_screenshot_is_materialized(self):
        tree = "AXWindow " + ("content " * 20)
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "state.png"
            capture.write_bytes(b"png")
            responses = [
                {"tree_markdown": tree, "elements": []},
                {
                    "tree_markdown": tree,
                    "elements": [],
                    "screenshot_file_path": str(capture),
                },
            ]
            with (
                mock.patch.object(
                    macos_cua, "call_driver", side_effect=responses
                ) as call_driver,
                mock.patch.object(macos_cua.time, "sleep") as sleep,
            ):
                result = macos_cua.snapshot(
                    899,
                    99,
                    retries=2,
                    include_screenshot=True,
                    screenshot_out_file=str(capture),
                )

            self.assertEqual(result["screenshot_file_path"], str(capture))
            self.assertEqual(call_driver.call_count, 2)
            sleep.assert_called_once_with(0.4)

    def test_app_state_foregrounds_once_after_background_capture_is_exhausted(self):
        tree = "AXWindow " + ("content " * 20)
        missing = {"tree_markdown": tree, "elements": []}
        captured = {
            "tree_markdown": tree,
            "elements": [],
            "screenshot_file_path": "/tmp/recovered.png",
            "screenshot_width": 200,
            "screenshot_height": 100,
        }
        with (
            mock.patch.object(
                macos_cua, "snapshot", side_effect=[missing, captured]
            ) as snapshot,
            mock.patch.object(
                macos_cua,
                "bring_resolved_window_to_front",
                return_value={"ok": True},
            ) as foreground,
            mock.patch.object(macos_cua, "_operator_cursor", return_value=None),
            mock.patch.object(macos_cua, "operator_update"),
        ):
            result = macos_cua.app_state("Fixture", 899, 99)

        foreground.assert_called_once_with(899, 99)
        self.assertEqual(snapshot.call_count, 2)
        self.assertEqual(snapshot.call_args_list[1].kwargs["retries"], 1)
        self.assertEqual(result["screenshot"]["path"], "/tmp/recovered.png")
        self.assertEqual(
            result["capture_recovery"],
            {"attempted": True, "foreground": {"ok": True}, "captured": True},
        )

    def test_app_state_retries_native_ax_before_driver_fallback(self):
        empty = {"elements": [], "tree_markdown": ""}
        native = {
            "elements": [{"role": "AXWindow", "label": "Fixture"}],
            "tree_markdown": "[1] AXWindow Fixture",
        }
        with (
            mock.patch.object(
                macos_cua, "_native_ax_snapshot", side_effect=[empty, native]
            ) as native_snapshot,
            mock.patch.object(macos_cua, "snapshot") as driver_snapshot,
            mock.patch.object(macos_cua.time, "sleep"),
            mock.patch.object(macos_cua, "operator_update"),
        ):
            result = macos_cua.app_state(
                "Fixture", 10, 20, include_screenshot=False
            )

        self.assertTrue(result["ok"])
        self.assertEqual(native_snapshot.call_count, 2)
        driver_snapshot.assert_not_called()

    def test_app_state_absorbs_brief_native_ax_instability_without_driver(self):
        empty = {"elements": [], "tree_markdown": ""}
        native = {
            "elements": [{"role": "AXWindow", "label": "Fixture"}],
            "tree_markdown": "[1] AXWindow Fixture",
        }
        with (
            mock.patch.object(
                macos_cua,
                "_native_ax_snapshot",
                side_effect=[empty, empty, empty, native],
            ) as native_snapshot,
            mock.patch.object(macos_cua, "snapshot") as driver_snapshot,
            mock.patch.object(macos_cua.time, "sleep") as sleep,
            mock.patch.object(macos_cua, "operator_update"),
        ):
            result = macos_cua.app_state(
                "Fixture", 10, 20, include_screenshot=False
            )

        self.assertTrue(result["ok"])
        self.assertEqual(native_snapshot.call_count, 4)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [0.12, 0.24, 0.36],
        )
        driver_snapshot.assert_not_called()

    def test_capture_geometry_rejects_stage_manager_thumbnail(self):
        with mock.patch.object(
            macos_cua,
            "_quartz_window_bounds",
            return_value=({"x": 0, "y": 0, "width": 230, "height": 408}, None),
        ):
            thumbnail = macos_cua._capture_geometry_proof(
                {"screenshot_width": 212, "screenshot_height": 353, "elements": []},
                10,
                20,
                max_image_dimension=1568,
            )
            full = macos_cua._capture_geometry_proof(
                {"screenshot_width": 230, "screenshot_height": 404, "elements": []},
                10,
                20,
                max_image_dimension=1568,
            )
        self.assertFalse(thumbnail["verified"])
        self.assertTrue(full["verified"])

    def test_capture_geometry_accepts_driver_declared_uniform_downscale(self):
        with mock.patch.object(
            macos_cua,
            "_quartz_window_bounds",
            return_value=({"x": 0, "y": 30, "width": 1920, "height": 1050}, None),
        ):
            proof = macos_cua._capture_geometry_proof(
                {
                    "screenshot_width": 1568,
                    "screenshot_height": 858,
                    "elements": [
                        {
                            "role": "AXWindow",
                            "frame": {"x": 0, "y": 30, "w": 1920, "h": 1050},
                        }
                    ],
                },
                32734,
                125876,
                max_image_dimension=1568,
            )

        self.assertTrue(proof["verified"])
        self.assertEqual(proof["identity"]["method"], "exact-quartz-window-id")
        self.assertEqual(proof["driver"]["max_image_dimension"], 1568)
        self.assertEqual(proof["matched_candidate"]["width"], 1568)

    def test_capture_geometry_exact_quartz_beats_larger_sibling_ax_window(self):
        raw = {
            "screenshot_width": 500,
            "screenshot_height": 400,
            "elements": [
                {
                    "role": "AXWindow",
                    "frame": {"x": 10, "y": 20, "w": 500, "h": 400},
                },
                {
                    "role": "AXWindow",
                    "frame": {"x": 900, "y": 20, "w": 1000, "h": 800},
                },
            ],
        }
        with mock.patch.object(
            macos_cua,
            "_quartz_window_bounds",
            return_value=({"x": 10, "y": 20, "width": 500, "height": 400}, None),
        ):
            proof = macos_cua._capture_geometry_proof(
                raw,
                10,
                20,
                max_image_dimension=1568,
            )

        self.assertTrue(proof["verified"])
        self.assertEqual(proof["expected"]["source"], "quartz")
        self.assertEqual(proof["expected"]["width"], 500)
        self.assertEqual(proof["ax_window_count"], 2)

    def test_capture_geometry_uses_sole_ax_for_stage_manager_quartz_proxy(self):
        ax_window = {
            "role": "AXWindow",
            "frame": {"x": 0, "y": 30, "w": 230, "h": 408},
        }
        with (
            mock.patch.object(
                macos_cua,
                "_quartz_window_bounds",
                return_value=(
                    {"x": 40, "y": 100, "width": 212, "height": 353},
                    None,
                ),
            ),
            mock.patch.object(
                macos_cua,
                "_exact_ax_window_frame",
                return_value=(
                    {
                        "source": "ax-window-id",
                        "window_id": 20,
                        "x": 0,
                        "y": 30,
                        "width": 230,
                        "height": 408,
                    },
                    None,
                ),
            ),
        ):
            thumbnail = macos_cua._capture_geometry_proof(
                {
                    "screenshot_width": 212,
                    "screenshot_height": 353,
                    "elements": [ax_window],
                },
                10,
                20,
                max_image_dimension=1568,
            )
            recapture = macos_cua._capture_geometry_proof(
                {
                    "screenshot_width": 230,
                    "screenshot_height": 404,
                    "elements": [ax_window],
                },
                10,
                20,
                max_image_dimension=1568,
            )

        self.assertFalse(thumbnail["verified"])
        self.assertTrue(recapture["verified"])
        self.assertEqual(
            recapture["identity"]["method"],
            "stage-manager-exact-ax-window-id-override",
        )
        self.assertEqual(recapture["expected"]["source"], "ax-window-id")
        self.assertEqual(recapture["expected"]["window_id"], 20)

    def test_capture_geometry_keeps_smaller_exact_quartz_for_unrelated_sole_ax(self):
        raw = {
            "screenshot_width": 800,
            "screenshot_height": 500,
            "elements": [
                {
                    "role": "AXWindow",
                    "frame": {"x": 900, "y": 20, "w": 1200, "h": 900},
                }
            ],
        }
        exact_ax = {
            "source": "ax-window-id",
            "window_id": 20,
            "x": 10,
            "y": 20,
            "width": 800,
            "height": 500,
        }
        with (
            mock.patch.object(
                macos_cua,
                "_quartz_window_bounds",
                return_value=(
                    {"x": 10, "y": 20, "width": 800, "height": 500},
                    None,
                ),
            ),
            mock.patch.object(
                macos_cua,
                "_exact_ax_window_frame",
                return_value=(exact_ax, None),
            ) as exact_identity,
        ):
            proof = macos_cua._capture_geometry_proof(
                raw,
                10,
                20,
                max_image_dimension=1568,
            )

        self.assertTrue(proof["verified"])
        self.assertEqual(proof["expected"]["source"], "quartz")
        self.assertEqual(proof["identity"]["method"], "exact-quartz-window-id")
        self.assertEqual(proof["identity"]["ax_identity"], "exact-window-not-proxy")
        exact_identity.assert_called_once_with(10, 20)

    def test_capture_geometry_suspected_proxy_without_exact_ax_is_unresolved(self):
        with (
            mock.patch.object(
                macos_cua,
                "_quartz_window_bounds",
                return_value=(
                    {"x": 10, "y": 20, "width": 800, "height": 500},
                    None,
                ),
            ),
            mock.patch.object(
                macos_cua,
                "_exact_ax_window_frame",
                return_value=(None, "no exact AX identity"),
            ),
        ):
            proof = macos_cua._capture_geometry_proof(
                {
                    "screenshot_width": 800,
                    "screenshot_height": 500,
                    "elements": [
                        {
                            "role": "AXWindow",
                            "frame": {"x": 900, "y": 20, "w": 1200, "h": 900},
                        }
                    ],
                },
                10,
                20,
                max_image_dimension=1568,
            )

        self.assertIsNone(proof["verified"])
        self.assertIsNone(proof["expected"])
        self.assertEqual(proof["identity"]["status"], "unresolved")
        self.assertEqual(
            proof["identity"]["reason"],
            "suspected-quartz-proxy-without-exact-ax-identity",
        )

    def test_exact_ax_window_frame_uses_matching_cg_window_id(self):
        candidates = [
            {
                "window_id": 19,
                "ax_frame": {"x": 0, "y": 0, "width": 1200, "height": 900},
            },
            {
                "window_id": 20,
                "ax_frame": {"x": 10, "y": 20, "width": 800, "height": 500},
            },
        ]
        with mock.patch.object(
            macos_cua,
            "_ax_window_candidates",
            return_value=candidates,
        ):
            frame, error = macos_cua._exact_ax_window_frame(10, 20)

        self.assertIsNone(error)
        self.assertEqual(frame["window_id"], 20)
        self.assertEqual((frame["width"], frame["height"]), (800, 500))

    def test_capture_geometry_leaves_ambiguous_ax_identity_unresolved(self):
        with mock.patch.object(
            macos_cua,
            "_quartz_window_bounds",
            return_value=(None, "exact Quartz window unavailable"),
        ):
            proof = macos_cua._capture_geometry_proof(
                {
                    "screenshot_width": 500,
                    "screenshot_height": 400,
                    "elements": [
                        {
                            "role": "AXWindow",
                            "frame": {"x": 0, "y": 0, "w": 500, "h": 400},
                        },
                        {
                            "role": "AXWindow",
                            "frame": {"x": 600, "y": 0, "w": 600, "h": 500},
                        },
                    ],
                },
                10,
                20,
                max_image_dimension=1568,
            )

        self.assertIsNone(proof["verified"])
        self.assertIsNone(proof["expected"])
        self.assertEqual(proof["identity"]["status"], "unresolved")
        self.assertEqual(proof["identity"]["reason"], "ambiguous-ax-windows")

    def test_driver_max_image_dimension_reads_current_config(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"max_image_dimension": 1568}),
            stderr="",
        )
        with mock.patch.object(
            macos_cua.subprocess,
            "run",
            return_value=completed,
        ) as run:
            value, error = macos_cua._driver_max_image_dimension()

        self.assertIsNone(error)
        self.assertEqual(value, 1568)
        self.assertEqual(run.call_args.args[0], [macos_cua.CUA_DRIVER, "config"])
        self.assertIs(run.call_args.kwargs["stdin"], macos_cua.subprocess.DEVNULL)

    def test_app_state_recaptures_geometry_mismatch_once(self):
        tree = "AXWindow " + ("content " * 20)
        bad = {
            "tree_markdown": tree,
            "elements": [],
            "screenshot_file_path": "/tmp/bad.png",
            "screenshot_width": 212,
            "screenshot_height": 353,
        }
        good = {
            "tree_markdown": tree,
            "elements": [],
            "screenshot_file_path": "/tmp/good.png",
            "screenshot_width": 230,
            "screenshot_height": 404,
        }
        with (
            mock.patch.object(macos_cua, "snapshot", side_effect=[bad, good]) as snapshot,
            mock.patch.object(
                macos_cua,
                "bring_resolved_window_to_front",
                return_value={"ok": True},
            ) as foreground,
            mock.patch.object(
                macos_cua,
                "_quartz_window_bounds",
                return_value=({"x": 0, "y": 0, "width": 230, "height": 408}, None),
            ),
            mock.patch.object(
                macos_cua,
                "_driver_max_image_dimension",
                return_value=(1568, None),
            ),
            mock.patch.object(macos_cua, "_operator_cursor", return_value=None),
            mock.patch.object(macos_cua, "operator_update"),
            mock.patch.object(macos_cua.time, "sleep"),
        ):
            result = macos_cua.app_state("Fixture", 10, 20)

        self.assertTrue(result["ok"])
        self.assertEqual(snapshot.call_count, 2)
        self.assertEqual(foreground.call_count, 2)
        self.assertEqual(result["screenshot"]["raw_path"], "/tmp/good.png")
        self.assertEqual(result["capture_recovery"]["reason"], "capture_geometry_mismatch")
        self.assertTrue(result["capture_geometry"]["verified"])

    def test_required_capture_with_fresh_verified_geometry_is_ok(self):
        raw = {
            "tree_markdown": "AXWindow " + ("content " * 20),
            "elements": [
                {
                    "role": "AXWindow",
                    "frame": {"x": 0, "y": 0, "w": 230, "h": 408},
                }
            ],
            "screenshot_file_path": "/tmp/fresh.png",
            "screenshot_width": 230,
            "screenshot_height": 404,
        }
        with (
            mock.patch.object(macos_cua, "snapshot", return_value=raw) as snapshot,
            mock.patch.object(
                macos_cua,
                "bring_resolved_window_to_front",
                return_value={"ok": True},
            ),
            mock.patch.object(
                macos_cua,
                "_quartz_window_bounds",
                return_value=({"x": 0, "y": 0, "width": 230, "height": 408}, None),
            ),
            mock.patch.object(
                macos_cua,
                "_driver_max_image_dimension",
                return_value=(1568, None),
            ),
            mock.patch.object(macos_cua, "_operator_cursor", return_value=None),
            mock.patch.object(macos_cua, "operator_update"),
            mock.patch.object(macos_cua.time, "sleep"),
        ):
            result = macos_cua.app_state("Fixture", 10, 20)

        self.assertTrue(result["ok"])
        self.assertTrue(result["capture_geometry"]["verified"])
        snapshot.assert_called_once()

    def test_required_capture_reuses_already_prepared_foreground(self):
        raw = {
            "snapshot_id": "fresh-1",
            "tree_markdown": "AXWindow " + ("content " * 20),
            "elements": [
                {
                    "role": "AXWindow",
                    "frame": {"x": 0, "y": 0, "w": 230, "h": 408},
                }
            ],
            "screenshot_file_path": "/tmp/fresh.png",
            "screenshot_width": 230,
            "screenshot_height": 404,
        }
        with (
            mock.patch.object(macos_cua, "snapshot", return_value=raw),
            mock.patch.object(macos_cua, "bring_resolved_window_to_front") as foreground,
            mock.patch.object(
                macos_cua,
                "_quartz_window_bounds",
                return_value=({"x": 0, "y": 0, "width": 230, "height": 408}, None),
            ),
            mock.patch.object(
                macos_cua, "_driver_max_image_dimension", return_value=(1568, None)
            ),
            mock.patch.object(macos_cua, "_operator_cursor", return_value=None),
            mock.patch.object(macos_cua, "operator_update") as operator,
        ):
            result = macos_cua.app_state(
                "Fixture", 10, 20, foreground_prepared=True
            )

        self.assertTrue(result["ok"])
        foreground.assert_not_called()
        self.assertEqual(operator.call_args.kwargs["snapshot_id"], "fresh-1")
        self.assertEqual(operator.call_args.kwargs["screenshot_width"], 230)

    def test_required_capture_fails_closed_for_ambiguous_ax_identity(self):
        raw = {
            "tree_markdown": "AXWindow " + ("content " * 20),
            "elements": [
                {
                    "role": "AXWindow",
                    "frame": {"x": 0, "y": 0, "w": 500, "h": 400},
                },
                {
                    "role": "AXWindow",
                    "frame": {"x": 600, "y": 0, "w": 600, "h": 500},
                },
            ],
            "screenshot_file_path": "/tmp/ambiguous.png",
            "screenshot_width": 500,
            "screenshot_height": 400,
        }
        with (
            mock.patch.object(macos_cua, "snapshot", return_value=raw) as snapshot,
            mock.patch.object(
                macos_cua,
                "bring_resolved_window_to_front",
                return_value={"ok": True},
            ),
            mock.patch.object(
                macos_cua,
                "_quartz_window_bounds",
                return_value=(None, "exact Quartz window unavailable"),
            ),
            mock.patch.object(
                macos_cua,
                "_driver_max_image_dimension",
                return_value=(1568, None),
            ),
            mock.patch.object(macos_cua, "_operator_cursor", return_value=None),
            mock.patch.object(macos_cua, "operator_update"),
            mock.patch.object(macos_cua.time, "sleep"),
        ):
            result = macos_cua.app_state("Fixture", 10, 20)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "capture_geometry_unresolved")
        self.assertEqual(
            result["capture_geometry"]["identity"]["reason"],
            "ambiguous-ax-windows",
        )
        snapshot.assert_called_once()

    def test_prefers_titled_window_over_larger_untitled_desktop(self):
        original_read_cache = macos_cua._read_cache
        original_list_windows = macos_cua.list_windows
        original_identity = macos_cua._running_app_identity
        original_write_cache = macos_cua._write_cache
        try:
            macos_cua._read_cache = lambda _app, **_kwargs: None
            macos_cua._running_app_identity = lambda _app: None
            macos_cua._write_cache = lambda *_args: None
            macos_cua.list_windows = lambda: {
                "windows": [
                    {
                        "pid": 100,
                        "window_id": 1,
                        "app_name": "Finder",
                        "title": "",
                        "bounds": {"width": 2560, "height": 1440},
                    },
                    {
                        "pid": 100,
                        "window_id": 2,
                        "app_name": "Finder",
                        "title": "Downloads",
                        "bounds": {"width": 920, "height": 436},
                    },
                ],
                "method": "quartz",
            }

            pid, window_id, name, error = macos_cua.resolve_app(
                "Finder", launch_if_missing=False
            )

            self.assertIsNone(error)
            self.assertEqual((pid, window_id, name), (100, 2, "Finder"))
        finally:
            macos_cua._read_cache = original_read_cache
            macos_cua.list_windows = original_list_windows
            macos_cua._running_app_identity = original_identity
            macos_cua._write_cache = original_write_cache

    def test_bundle_identity_uses_pid_and_ignores_title_collision(self):
        original_read_cache = macos_cua._read_cache
        original_list_windows = macos_cua.list_windows
        original_identity = macos_cua._running_app_identity
        original_ax_windows = macos_cua._ax_window_candidates
        original_write_cache = macos_cua._write_cache
        original_pid_alive = macos_cua._pid_alive
        try:
            macos_cua._read_cache = lambda _app, **_kwargs: None
            macos_cua._write_cache = lambda *_args: None
            macos_cua._pid_alive = lambda _pid: True
            macos_cua._running_app_identity = lambda _app: {
                "pid": 899,
                "name": "ChatGPT",
                "bundle_id": "com.openai.codex",
            }
            macos_cua._ax_window_candidates = lambda _pid, _windows: [
                {
                    "pid": 899,
                    "window_id": 98,
                    "title": "ChatGPT",
                    "bounds": {"width": 1000, "height": 1000},
                    "ax_main": False,
                    "ax_minimized": False,
                },
                {
                    "pid": 899,
                    "window_id": 99,
                    "title": "ChatGPT",
                    "bounds": {"width": 163, "height": 183},
                    "ax_main": True,
                    "ax_minimized": False,
                },
            ]
            macos_cua.list_windows = lambda: {
                "windows": [
                    {
                        "pid": 1116,
                        "window_id": 13,
                        "app_name": "Notification Center",
                        "title": "com.gurusharan.CodexWidget",
                        "bounds": {"width": 1000, "height": 1000},
                    },
                    {
                        "pid": 899,
                        "window_id": 99,
                        "app_name": "ChatGPT",
                        "title": "ChatGPT",
                        "bounds": {"width": 900, "height": 700},
                    },
                ],
                "method": "quartz",
            }

            pid, window_id, name, error = macos_cua.resolve_app(
                "com.openai.codex", launch_if_missing=False
            )

            self.assertIsNone(error)
            self.assertEqual((pid, window_id, name), (899, 99, "ChatGPT"))
        finally:
            macos_cua._read_cache = original_read_cache
            macos_cua.list_windows = original_list_windows
            macos_cua._running_app_identity = original_identity
            macos_cua._ax_window_candidates = original_ax_windows
            macos_cua._write_cache = original_write_cache
            macos_cua._pid_alive = original_pid_alive


class DisplayAlignmentTests(unittest.TestCase):
    def test_default_display_is_first_secondary_not_vendor_specific(self):
        screens = [
            {"name": "Built-in Display", "main": True},
            {"name": "Studio Display", "main": False},
        ]
        with (
            mock.patch.dict(os.environ, {"MACOS_CUA_DISPLAY": ""}),
            mock.patch.object(displays, "list_displays", return_value=screens),
        ):
            self.assertEqual(displays.resolve_display_token(), "Studio Display")

    def test_window_override_requires_same_resolved_pid(self):
        windows = {
            "windows": [
                {"pid": 42, "window_id": 100},
                {"pid": 99, "window_id": 200},
            ]
        }
        with mock.patch.object(macos_cua, "list_windows", return_value=windows):
            self.assertEqual(macos_cua._validated_window_override(42, 100, 100), 100)
            with self.assertRaisesRegex(ValueError, "does not belong"):
                macos_cua._validated_window_override(42, 100, 200)

    def test_frame_match_accepts_small_os_position_clamp_but_not_size_drift(self):
        clamped = {"x": 2580, "y": 317, "width": 1880, "height": 1000}
        self.assertTrue(
            displays.frame_matches_requested(
                clamped, x=2580, y=307, width=1880, height=1000
            )
        )
        self.assertFalse(
            displays.frame_matches_requested(
                {**clamped, "width": 1820},
                x=2580,
                y=307,
                width=1880,
                height=1000,
            )
        )

    def test_move_process_window_selects_largest_window_and_verifies_frame(self):
        target = {
            "name": "DELL P2719H",
            "x": 2560,
            "y": 73,
            "width": 1920,
            "height": 1080,
        }
        after = {
            "display": {"name": "DELL P2719H"},
            "bounds": {"x": 2580, "y": 307, "width": 1880, "height": 1000},
        }
        completed = SimpleNamespace(returncode=0, stdout="OK", stderr="")
        with (
            mock.patch.object(displays, "find_display", return_value=target),
            mock.patch.object(
                displays, "applescript_position", return_value=(2580, 307)
            ),
            mock.patch.object(
                displays.subprocess, "run", return_value=completed
            ) as run,
            mock.patch.object(displays, "window_on_display", return_value=after),
        ):
            result = displays.move_process_window(
                "TestApp", "DELL", width=1880, height=1000, margin=20
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["frame_matches"])
        apple_script = next(
            call.args[0][-1]
            for call in run.call_args_list
            if "set size of targetWindow" in call.args[0][-1]
        )
        self.assertIn("repeat with candidateWindow in windows", apple_script)
        self.assertIn("set position of targetWindow", apple_script)
        self.assertLess(
            apple_script.index("set size of targetWindow"),
            apple_script.index("set position of targetWindow"),
        )

    def test_move_process_window_waits_for_windowserver_settle(self):
        target = {
            "name": "DELL P2719H",
            "x": 0,
            "y": 0,
            "width": 1920,
            "height": 1080,
        }
        transient = {
            "bounds": {"x": 226, "y": 140, "width": 1880, "height": 1000},
            "display": target,
        }
        settled = {
            "bounds": {"x": 20, "y": 20, "width": 1880, "height": 1000},
            "display": target,
        }
        completed = SimpleNamespace(returncode=0, stdout="OK", stderr="")
        with (
            mock.patch.object(displays, "find_display", return_value=target),
            mock.patch.object(displays, "applescript_position", return_value=(20, 20)),
            mock.patch.object(displays.subprocess, "run", return_value=completed),
            mock.patch.object(
                displays,
                "window_on_display",
                side_effect=[transient, settled],
            ),
            mock.patch.object(displays.time, "sleep") as sleep,
        ):
            result = displays.move_process_window(
                "TestApp", "DELL", width=1880, height=1000, margin=20
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["window_after"], settled)
        sleep.assert_called_once_with(0.1)

    def test_move_process_window_uses_logical_frame_for_stage_manager(self):
        target = {
            "name": "DELL P2719H",
            "x": 0,
            "y": 0,
            "width": 1920,
            "height": 1080,
        }
        thumbnail = {
            "bounds": {"x": -285, "y": 569, "width": 215, "height": 148},
            "display": None,
        }
        logical = {
            "bounds": {"x": 20, "y": 30, "width": 1880, "height": 1000},
            "display": target,
        }
        completed = SimpleNamespace(returncode=0, stdout="OK", stderr="")
        with (
            mock.patch.object(displays, "find_display", return_value=target),
            mock.patch.object(displays, "applescript_position", return_value=(20, 20)),
            mock.patch.object(displays.subprocess, "run", return_value=completed),
            mock.patch.object(displays, "window_on_display", return_value=thumbnail),
            mock.patch.object(
                displays, "logical_window_on_display", return_value=logical
            ),
            mock.patch.object(displays.time, "monotonic", side_effect=[0, 0, 3]),
            mock.patch.object(displays.time, "sleep"),
        ):
            result = displays.move_process_window(
                "TestApp", "DELL", width=1880, height=1000, margin=20
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["verification"], "accessibility-logical")
        self.assertEqual(result["quartz_window_after"], thumbnail)

    def test_move_process_window_uses_native_ax_when_system_events_has_no_window(self):
        target = {
            "name": "DELL P2719H",
            "x": 0,
            "y": 0,
            "width": 1920,
            "height": 1080,
        }
        logical = {
            "bounds": {"x": 20, "y": 20, "width": 1880, "height": 1000},
            "display": target,
        }
        completed = SimpleNamespace(returncode=0, stdout="NO_WIN", stderr="")
        native_move = {"ok": True, "method": "native-ax", "pid": 95659}
        with (
            mock.patch.object(displays, "find_display", return_value=target),
            mock.patch.object(displays, "applescript_position", return_value=(20, 20)),
            mock.patch.object(displays.subprocess, "run", return_value=completed),
            mock.patch.object(
                displays, "set_native_ax_window_frame", return_value=native_move
            ) as set_native,
            mock.patch.object(displays, "window_on_display", return_value=None),
            mock.patch.object(
                displays, "logical_window_on_display", return_value=logical
            ),
            mock.patch.object(displays.time, "monotonic", side_effect=[0, 0, 3]),
            mock.patch.object(displays.time, "sleep"),
        ):
            result = displays.move_process_window(
                "TestApp", "DELL", width=1880, height=1000, margin=20
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["move_method"], "native-ax")
        self.assertEqual(result["native_move"], native_move)
        set_native.assert_called_once_with(
            "TestApp", x=20, y=20, width=1880, height=1000
        )

    def test_ensure_display_reframes_window_already_on_target(self):
        before = {
            "display": {"name": "DELL P2719H"},
            "bounds": {"x": 2670, "y": 518, "width": 1280, "height": 720},
        }
        target = {
            "name": "DELL P2719H",
            "x": 2560,
            "y": 73,
            "width": 1920,
            "height": 1080,
        }
        with (
            mock.patch.object(displays, "find_display", return_value=target),
            mock.patch.object(displays, "window_on_display", return_value=before),
            mock.patch.object(
                displays, "applescript_position", return_value=(2580, 307)
            ),
            mock.patch.object(
                displays,
                "move_process_window",
                return_value={"ok": True, "frame_matches": True},
            ) as move,
        ):
            result = displays.ensure_on_test_display(
                "TestApp", "DELL", width=1880, height=1000, margin=20
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["moved"])
        self.assertEqual(result["reason"], "frame_mismatch")
        move.assert_called_once_with(
            "TestApp", "DELL", width=1880, height=1000, margin=20
        )

    def test_ensure_display_is_idempotent_for_matching_frame(self):
        before = {
            "display": {"name": "DELL P2719H"},
            "bounds": {"x": 2580, "y": 307, "width": 1880, "height": 1000},
        }
        target = {
            "name": "DELL P2719H",
            "x": 2560,
            "y": 73,
            "width": 1920,
            "height": 1080,
        }
        with (
            mock.patch.object(displays, "find_display", return_value=target),
            mock.patch.object(displays, "window_on_display", return_value=before),
            mock.patch.object(
                displays, "applescript_position", return_value=(2580, 307)
            ),
            mock.patch.object(displays, "move_process_window") as move,
        ):
            result = displays.ensure_on_test_display(
                "TestApp", "DELL", width=1880, height=1000, margin=20
            )

        self.assertTrue(result["ok"])
        self.assertFalse(result["moved"])
        self.assertTrue(result["frame_matches"])
        move.assert_not_called()

    def test_ensure_display_preserves_current_size_when_unspecified(self):
        before = {
            "display": {"name": "Built-in Display"},
            "bounds": {"x": 100, "y": 100, "width": 1280, "height": 720},
        }
        target = {
            "name": "Studio Display",
            "x": 2560,
            "y": 0,
            "width": 1920,
            "height": 1080,
        }
        with (
            mock.patch.object(displays, "find_display", return_value=target),
            mock.patch.object(displays, "window_on_display", return_value=before),
            mock.patch.object(displays, "applescript_position", return_value=(2680, 120)),
            mock.patch.object(
                displays,
                "move_process_window",
                return_value={"ok": True, "frame_matches": True},
            ) as move,
        ):
            result = displays.ensure_on_test_display("TestApp", "Studio")

        self.assertTrue(result["ok"])
        move.assert_called_once_with(
            "TestApp", "Studio", width=1280, height=720, margin=120
        )

    def test_ensure_display_does_not_reposition_window_already_on_target(self):
        before = {
            "display": {"name": "DELL P2719H"},
            "bounds": {"x": 2576, "y": 466, "width": 40, "height": 104},
        }
        with (
            mock.patch.object(
                displays, "find_display", return_value={"name": "DELL P2719H"}
            ),
            mock.patch.object(displays, "window_on_display", return_value=before),
            mock.patch.object(displays, "move_process_window") as move,
        ):
            result = displays.ensure_on_test_display("Calculator", "DELL P2719H")

        self.assertTrue(result["ok"])
        self.assertFalse(result["moved"])
        self.assertEqual(result["reason"], "already_on_target_display")
        move.assert_not_called()

    def test_overlay_move_requires_quartz_confirmation(self):
        screens = [
            {"name": "LG IPS QHD", "x": 0, "y": 0, "width": 2560, "height": 1440},
            {"name": "DELL P2719H", "x": 2560, "y": 0, "width": 1920, "height": 1080},
        ]
        actual = {"display": {"name": "DELL P2719H"}}
        with (
            mock.patch.object(displays, "list_displays", return_value=screens),
            mock.patch.object(
                displays.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=0, stdout="OK", stderr=""),
            ),
            mock.patch.object(
                displays,
                "window_bounds_for_process",
                return_value={"x": 2560, "y": 287, "width": 1920, "height": 1080},
            ),
            mock.patch.object(displays, "window_on_display", return_value=actual),
        ):
            result = displays.ensure_overlay_on_display("LG")

        self.assertFalse(result["ok"])
        self.assertEqual(result["actual_display"], "DELL P2719H")


class CursorProofTests(unittest.TestCase):
    @staticmethod
    def _png_header(width=96, height=96):
        return (
            b"\x89PNG\r\n\x1a\n"
            + (13).to_bytes(4, "big")
            + b"IHDR"
            + int(width).to_bytes(4, "big")
            + int(height).to_bytes(4, "big")
        )

    @classmethod
    def _png_bytes(cls, width=96, height=96, alpha=255):
        def chunk(name, payload):
            checksum = zlib.crc32(name + payload) & 0xFFFFFFFF
            return (
                len(payload).to_bytes(4, "big")
                + name
                + payload
                + checksum.to_bytes(4, "big")
            )

        ihdr = (
            int(width).to_bytes(4, "big")
            + int(height).to_bytes(4, "big")
            + bytes((8, 6, 0, 0, 0))
        )
        pixel = bytes((16, 32, 48, int(alpha)))
        scanlines = (b"\x00" + pixel * int(width)) * int(height)
        return (
            cls._png_header(width, height)[:8]
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(scanlines))
            + chunk(b"IEND", b"")
        )

    def test_cursor_ack_requires_matching_update_and_target_identity(self):
        original_cache = macos_cua.CACHE_DIR
        try:
            with tempfile.TemporaryDirectory() as directory:
                macos_cua.CACHE_DIR = directory
                state_path = Path(directory) / "operator-state.json"
                base = {
                    "app": "Calculator",
                    "pid": 10,
                    "window_id": 20,
                    "cursor_update_id": "new",
                    "cursor_rendered_update_id": "old",
                    "cursor_rendered_x": 0.25,
                    "cursor_rendered_y": 0.75,
                }
                state_path.write_text(json.dumps(base))
                with mock.patch.dict(
                    os.environ, {"MACOS_CUA_CURSOR_SYNC_TIMEOUT": "0.03"}
                ):
                    stale = macos_cua._wait_for_operator_cursor(
                        0.25,
                        0.75,
                        update_id="new",
                        app_name="Calculator",
                        pid=10,
                        window_id=20,
                    )
                self.assertFalse(stale["ok"])

                state_path.write_text(
                    json.dumps({**base, "cursor_rendered_update_id": "new"})
                )
                current = macos_cua._wait_for_operator_cursor(
                    0.25,
                    0.75,
                    update_id="new",
                    app_name="Calculator",
                    pid=10,
                    window_id=20,
                )
                self.assertTrue(current["ok"])
                self.assertEqual(current["update_id"], "new")
        finally:
            macos_cua.CACHE_DIR = original_cache

    def test_pointer_position_is_normalized_to_the_logical_window(self):
        snapshot = {
            "elements": [
                {"role": "AXWindow", "frame": {"x": 100, "y": 200, "w": 400, "h": 600}},
            ],
        }
        with (
            mock.patch.object(macos_cua, "find_clickable_index", return_value=3),
            mock.patch.object(macos_cua, "element_center", return_value=(300, 500)),
            mock.patch.object(macos_cua, "operator_update", return_value={"ok": True}) as update,
            mock.patch.object(
                macos_cua,
                "_wait_for_operator_cursor",
                return_value={"ok": True, "duration_ms": 320},
            ),
            mock.patch.object(macos_cua, "cursor") as driver_cursor,
            mock.patch.object(macos_cua, "click_with_retry", return_value={"ok": True}),
            mock.patch.object(macos_cua.time, "sleep"),
        ):
            result = macos_cua.click_label_pointer(
                10, 20, "Target", snapshot_data=snapshot, app_name="App"
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["cursor_normalized"], {"x": 0.5, "y": 0.5})
        self.assertTrue(result["move"]["ok"])
        update.assert_called_once()
        driver_cursor.assert_not_called()

    def test_pointer_position_republishes_once_when_render_ack_is_missing(self):
        snapshot = {
            "elements": [
                {
                    "role": "AXWindow",
                    "frame": {"x": 100, "y": 200, "w": 400, "h": 600},
                },
            ],
        }
        with (
            mock.patch.object(macos_cua, "find_clickable_index", return_value=3),
            mock.patch.object(macos_cua, "element_center", return_value=(300, 500)),
            mock.patch.object(
                macos_cua, "operator_update", return_value={"ok": True}
            ) as update,
            mock.patch.object(
                macos_cua,
                "_wait_for_operator_cursor",
                side_effect=[
                    {"ok": False, "error": "timed out"},
                    {"ok": True, "duration_ms": 320},
                ],
            ) as wait,
            mock.patch.object(macos_cua, "click_with_retry", return_value={"ok": True}),
        ):
            result = macos_cua.click_label_pointer(
                10, 20, "Target", snapshot_data=snapshot, app_name="App"
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["move"]["ok"])
        self.assertEqual(update.call_count, 2)
        self.assertEqual(wait.call_count, 2)
        self.assertEqual(result["move"]["recovery"], {"ok": True})

    def test_pointer_click_falls_back_to_reactivated_native_ax_content(self):
        driver_snapshot = {
            "tree_markdown": "AXApplication > AXMenuBar",
            "elements": [
                {"element_index": 1, "role": "AXApplication", "label": "App"},
                {"element_index": 2, "role": "AXMenuBar", "label": ""},
            ],
        }
        services = SimpleNamespace(
            AXUIElementPerformAction=mock.Mock(return_value=0),
        )
        native_snapshot = {
            "source": "native_ax",
            "tree_markdown": "[1] AXWindow App\n[2] AXButton Members",
            "elements": [
                {
                    "element_index": 1,
                    "role": "AXWindow",
                    "label": "App",
                    "frame": {"x": 100, "y": 200, "w": 400, "h": 600},
                },
                {
                    "element_index": 2,
                    "role": "AXButton",
                    "label": "Members",
                    "frame": {"x": 180, "y": 280, "w": 80, "h": 40},
                    "_native_element": "native-members",
                    "_native_services": services,
                },
            ],
        }
        with (
            mock.patch.object(
                macos_cua,
                "_native_ax_snapshot",
                return_value=native_snapshot,
            ) as native_state,
            mock.patch.object(macos_cua.time, "sleep"),
        ):
            result = macos_cua.click_label_pointer(
                10,
                20,
                "Members",
                snapshot_data=driver_snapshot,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["method"], "agent-cursor-glide+native-axpress-fallback"
        )
        self.assertEqual(result["element"], 2)
        native_state.assert_called_once_with(10, max_elements=50, window_id=20)
        services.AXUIElementPerformAction.assert_called_once_with(
            "native-members", "AXPress"
        )

    def test_click_label_uses_ax_frame_hid_when_row_press_unsupported(self):
        snap = {
            "source": "native_ax",
            "elements": [
                {
                    "element_index": 2,
                    "role": "AXRow",
                    "label": "Folder",
                    "frame": {"x": 10, "y": 20, "w": 30, "h": 40},
                },
            ],
        }
        native = SimpleNamespace(
            post_mouse_click=mock.Mock(
                return_value={
                    "ok": True,
                    "accepted": True,
                    "path": "native_hid_mouse",
                }
            )
        )
        with (
            mock.patch.object(macos_cua, "find_clickable_index", return_value=2),
            mock.patch.object(macos_cua, "snapshot_content_error", return_value=None),
            mock.patch.object(
                macos_cua,
                "glide_operator_to_element",
                return_value={
                    "ok": True,
                    "move": {"ok": True},
                    "coords": {"x": 25, "y": 40},
                },
            ),
            mock.patch.object(
                macos_cua,
                "_native_ax_press_label_with_retry",
                return_value=(
                    {"error": "AXUIElementPerformAction(AXPress) returned -25206"},
                    snap,
                    2,
                ),
            ),
            mock.patch.object(macos_cua, "_native_input", return_value=native),
            mock.patch.object(macos_cua, "_cleanup_driver_cursors"),
        ):
            result = macos_cua.click_label_pointer(
                10, 20, "Folder", snapshot_data=snap, app_name="App"
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "agent-cursor-glide+ax-frame-hid")
        native.post_mouse_click.assert_called_once()

    def test_cleanup_driver_cursors_disables_then_ends_auto_sessions(self):
        state = {
            "enabled": True,
            "cursors": [
                {
                    "config": {
                        "cursor_id": "auto-cyan",
                        "cursor_color": "#00FFFF",
                        "cursor_icon": None,
                        "enabled": True,
                    }
                },
                {
                    "config": {
                        "cursor_id": "macos-cua",
                        "enabled": True,
                    }
                },
            ],
        }
        calls = []

        def fake_call(tool, params=None, timeout=None):
            calls.append((tool, params or {}))
            if tool == "get_agent_cursor_state":
                return state
            return {"ok": True}

        with mock.patch.object(macos_cua, "call_driver", side_effect=fake_call):
            result = macos_cua._cleanup_driver_cursors()

        self.assertEqual(result["ended"], ["auto-cyan"])
        self.assertEqual(
            calls,
            [
                ("get_agent_cursor_state", {}),
                (
                    "set_agent_cursor_enabled",
                    {"enabled": False, "session": "auto-cyan"},
                ),
                ("end_session", {"session": "auto-cyan"}),
                ("set_agent_cursor_enabled", {"enabled": False}),
            ],
        )

    def test_click_at_desktop_wipes_driver_cursors_before_and_after(self):
        with (
            mock.patch.object(
                macos_cua, "_cleanup_driver_cursors", return_value={"ended": []}
            ) as cleanup,
            mock.patch.object(
                macos_cua, "call_driver", return_value={"ok": True}
            ) as call_driver,
        ):
            result = macos_cua.click_at_desktop(10.0, 20.0)

        self.assertTrue(result["ok"])
        self.assertTrue(result["user_interruptive"])
        self.assertFalse(result["isolated_pointer"])
        self.assertFalse(result["pointer_preserve_requested"])
        self.assertIsNone(result["pointer_restored"])
        self.assertEqual(cleanup.call_count, 2)
        call_driver.assert_called_once_with(
            "click",
            {
                "x": 10.0,
                "y": 20.0,
                "scope": "desktop",
                "button": "left",
                "count": 1,
            },
        )

    def test_click_at_desktop_passes_right_button(self):
        with (
            mock.patch.object(
                macos_cua, "_cleanup_driver_cursors", return_value={"ended": []}
            ),
            mock.patch.object(
                macos_cua, "call_driver", return_value={"ok": True}
            ) as call_driver,
        ):
            result = macos_cua.click_at_desktop(
                3480.0, 220.0, button="right", click_count=1
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["user_interruptive"])
        call_driver.assert_called_once_with(
            "click",
            {
                "x": 3480.0,
                "y": 220.0,
                "scope": "desktop",
                "button": "right",
                "count": 1,
            },
        )

    def test_click_at_desktop_can_restore_pointer_as_explicit_mitigation(self):
        with (
            mock.patch.object(
                macos_cua, "_cleanup_driver_cursors", return_value={"ended": []}
            ),
            mock.patch.object(
                macos_cua, "_system_pointer_position", return_value=(91.0, 42.0)
            ) as position,
            mock.patch.object(
                macos_cua, "_restore_system_pointer", return_value=True
            ) as restore,
            mock.patch.object(
                macos_cua, "call_driver", return_value={"ok": True}
            ),
        ):
            result = macos_cua.click_at_desktop(
                10.0, 20.0, preserve_pointer=True
            )

        position.assert_called_once_with()
        restore.assert_called_once_with((91.0, 42.0))
        self.assertTrue(result["pointer_preserve_requested"])
        self.assertTrue(result["pointer_restored"])
        self.assertTrue(result["user_interruptive"])
        self.assertFalse(result["isolated_pointer"])

    def test_pointer_click_only_uses_vision_coordinates_when_explicit(self):
        menu_only = {
            "tree_markdown": "AXApplication > AXMenuBar",
            "elements": [
                {"element_index": 1, "role": "AXApplication", "label": "App"},
            ],
        }
        native_missing = {
            "error": "snapshot has no target-window AX content",
            "elements": [],
            "tree_markdown": "",
        }
        visual = {
            "source": "driver_vision",
            "tree_markdown": "[7] VisionText Members",
            "elements": [
                {
                    "element_index": 7,
                    "role": "VisionText",
                    "label": "Members",
                    "frame": {"x": 180, "y": 280, "w": 80, "h": 40},
                }
            ],
        }
        with (
            mock.patch.object(
                macos_cua,
                "_native_ax_snapshot",
                return_value=native_missing,
            ),
            mock.patch.object(
                macos_cua,
                "_vision_snapshot_after_activation",
                return_value=visual,
            ) as vision_state,
            mock.patch.object(
                macos_cua, "click_at_desktop", return_value={"ok": True}
            ) as click_at,
            mock.patch.dict(os.environ, {"MACOS_CUA_PIXEL_CLICK": "1"}),
            mock.patch.object(macos_cua.time, "sleep"),
        ):
            result = macos_cua.click_label_pointer(
                10,
                20,
                "Members",
                snapshot_data=menu_only,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["method"], "agent-cursor-glide+vision-desktop-click-fallback"
        )
        vision_state.assert_called_once_with(10, 20, max_elements=50)
        click_at.assert_called_once_with(220.0, 300.0)

    def test_pointer_click_rejects_implicit_vision_coordinate_fallback(self):
        visual = {
            "source": "driver_vision",
            "tree_markdown": "[7] VisionText Members",
            "elements": [
                {
                    "element_index": 7,
                    "role": "VisionText",
                    "label": "Members",
                    "frame": {"x": 180, "y": 280, "w": 80, "h": 40},
                }
            ],
        }
        with (
            mock.patch.object(
                macos_cua,
                "_native_ax_snapshot",
                return_value={"error": "no AX", "elements": []},
            ),
            mock.patch.object(
                macos_cua, "_vision_snapshot_after_activation", return_value=visual
            ) as vision_state,
            mock.patch.object(macos_cua, "click_at_desktop") as click_at,
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("MACOS_CUA_PIXEL_CLICK", None)
            result = macos_cua.click_label_pointer(
                10,
                20,
                "Members",
                snapshot_data={"elements": [], "tree_markdown": ""},
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "not found")
        vision_state.assert_not_called()
        click_at.assert_not_called()

    def test_driver_vision_without_frames_falls_back_to_native_vision(self):
        driver_visual = {
            "source": "driver_vision",
            "tree_markdown": "AXApplication > AXMenuBar",
            "screenshot_file_path": "/tmp/driver-vision.png",
            "elements": [
                {"element_index": 1, "role": "AXMenuBar", "label": ""}
            ],
        }
        native_visual = {
            "source": "native_vision",
            "tree_markdown": "[1] VisionText Upcoming",
            "elements": [
                {
                    "element_index": 1,
                    "role": "VisionText",
                    "label": "Upcoming",
                    "frame": {"x": 10, "y": 20, "w": 60, "h": 20},
                }
            ],
        }
        with (
            mock.patch.object(
                macos_cua, "_activate_running_identity", return_value={"ok": True}
            ),
            mock.patch.object(macos_cua, "snapshot", return_value=driver_visual),
            mock.patch.object(
                macos_cua,
                "_native_vision_snapshot",
                return_value=native_visual,
            ) as native_vision,
        ):
            result = macos_cua._vision_snapshot_after_activation(10, 20, 50)

        self.assertEqual(result["source"], "native_vision")
        native_vision.assert_called_once_with(
            10,
            20,
            "/tmp/driver-vision.png",
            max_elements=50,
        )

    def test_native_vision_snapshot_runs_verified_window_helper(self):
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "source": "native_vision",
                    "tree_markdown": "[1] VisionText Members",
                    "elements": [],
                }
            ),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as directory:
            screenshot = Path(directory) / "driver.png"
            screenshot.touch()
            with (
                mock.patch.object(
                    macos_cua,
                    "_quartz_window_bounds",
                    return_value=(
                        {"x": 100.0, "y": 200.0, "width": 1080.0, "height": 760.0},
                        None,
                    ),
                ),
                mock.patch.object(
                    macos_cua,
                    "_ensure_native_vision_binary",
                    return_value=("/tmp/vision-window-ocr", None),
                ),
                mock.patch.object(
                    macos_cua.subprocess, "run", return_value=completed
                ) as run,
            ):
                result = macos_cua._native_vision_snapshot(
                    10,
                    20,
                    screenshot,
                    50,
                )

        self.assertEqual(result["source"], "native_vision")
        self.assertEqual(
            run.call_args.args[0],
            [
                "/tmp/vision-window-ocr",
                "--image",
                str(screenshot),
                "--origin-x",
                "100.0",
                "--origin-y",
                "200.0",
                "--logical-width",
                "1080.0",
                "--logical-height",
                "760.0",
                "--max",
                "50",
            ],
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 15)

    def test_cursor_proof_uses_the_hermes_extension_asset(self):
        from AppKit import (
            NSBitmapImageFileTypePNG,
            NSBitmapImageRep,
            NSColor,
            NSImage,
            NSMakeRect,
            NSRectFill,
        )

        self.assertTrue(Path(macos_cua.CURSOR_ICON).is_file())
        self.assertEqual(Path(macos_cua.CURSOR_ICON).name, "pointer-shape-animated.svg")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            image = NSImage.alloc().initWithSize_((120, 120))
            image.lockFocus()
            NSColor.windowBackgroundColor().setFill()
            NSRectFill(NSMakeRect(0, 0, 120, 120))
            representation = NSBitmapImageRep.alloc().initWithFocusedViewRect_(
                NSMakeRect(0, 0, 120, 120)
            )
            image.unlockFocus()
            data = representation.representationUsingType_properties_(
                NSBitmapImageFileTypePNG, {}
            )
            data.writeToFile_atomically_(str(source), True)

            result = macos_cua.annotate_cursor_screenshot(source, 0.5, 0.5)

            self.assertTrue(result["ok"], result)
            self.assertEqual(result["cursor_asset"], macos_cua.CURSOR_ICON)
            self.assertGreater(
                Path(result["path"]).stat().st_size, source.stat().st_size
            )

    def test_native_cursor_raster_uses_the_same_hermes_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "pointer.png"
            result = macos_cua.cursor_raster_path(
                macos_cua.CURSOR_ICON, output, size=96
            )

            self.assertEqual(Path(result), output)
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 100)
            self.assertEqual(macos_cua._validate_cursor_raster(output, 96), (96, 96))

    def test_real_cursor_svg_uses_appkit_when_sips_exits_13(self):
        failure = SimpleNamespace(
            returncode=13,
            stderr="Cannot extract image from file / Error 13 unknown",
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(macos_cua.subprocess, "run", return_value=failure) as run,
            mock.patch.object(macos_cua.time, "sleep") as sleep,
        ):
            output = Path(directory) / "pointer.png"
            result = macos_cua.cursor_raster_path(
                macos_cua.CURSOR_ICON, output, size=96
            )

            self.assertEqual(Path(result), output)
            self.assertGreater(output.stat().st_size, 100)
            self.assertEqual(macos_cua._validate_cursor_raster(output, 96), (96, 96))
            self.assertEqual(run.call_count, 2)
            sleep.assert_called_once_with(0.05)

    def test_cursor_raster_retries_once_after_sips_failure_then_succeeds(self):
        calls = []

        def fake_sips(arguments, **_kwargs):
            temporary = Path(arguments[-1])
            calls.append(temporary)
            if len(calls) == 1:
                self.assertFalse(temporary.exists())
                return SimpleNamespace(returncode=1, stderr="transient launch failure")
            temporary.write_bytes(self._png_bytes())
            return SimpleNamespace(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pointer.svg"
            output = root / "pointer.png"
            source.write_text("<svg/>")
            with (
                mock.patch.object(macos_cua.subprocess, "run", side_effect=fake_sips),
                mock.patch.object(macos_cua.time, "sleep") as sleep,
                mock.patch.object(
                    macos_cua, "_rasterize_cursor_with_appkit"
                ) as appkit,
            ):
                result = macos_cua.cursor_raster_path(source, output)

            self.assertEqual(Path(result), output)
            self.assertEqual(output.read_bytes(), self._png_bytes())
            self.assertEqual(len(calls), 2)
            self.assertNotEqual(calls[0], calls[1])
            self.assertTrue(all(not path.exists() for path in calls))
            sleep.assert_called_once()
            self.assertLessEqual(sleep.call_args.args[0], 0.1)
            appkit.assert_not_called()

    def test_cursor_raster_uses_appkit_after_two_sips_failures(self):
        sips_calls = []
        appkit_calls = []

        def failing_sips(arguments, **_kwargs):
            sips_calls.append(Path(arguments[-1]))
            return SimpleNamespace(returncode=13, stderr="cannot extract SVG")

        def successful_appkit(source, output, size):
            appkit_calls.append((Path(source), Path(output), size))
            Path(output).write_bytes(self._png_bytes(size, size))
            return str(output)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pointer.svg"
            output = root / "pointer.png"
            source.write_text("<svg/>")
            with (
                mock.patch.object(
                    macos_cua.subprocess, "run", side_effect=failing_sips
                ),
                mock.patch.object(
                    macos_cua,
                    "_rasterize_cursor_with_appkit",
                    side_effect=successful_appkit,
                ),
                mock.patch.object(macos_cua.time, "sleep") as sleep,
            ):
                result = macos_cua.cursor_raster_path(source, output)

            self.assertEqual(Path(result), output)
            self.assertEqual(output.read_bytes(), self._png_bytes())
            self.assertEqual(len(sips_calls), 2)
            self.assertEqual(len(appkit_calls), 1)
            self.assertEqual(appkit_calls[0][0], source)
            self.assertEqual(appkit_calls[0][2], 96)
            self.assertNotIn(appkit_calls[0][1], sips_calls)
            self.assertTrue(all(not path.exists() for path in sips_calls))
            self.assertFalse(appkit_calls[0][1].exists())
            sleep.assert_called_once_with(0.05)

    def test_cursor_raster_two_failures_raise_explicit_error(self):
        calls = []

        def failing_sips(arguments, **_kwargs):
            calls.append(Path(arguments[-1]))
            return SimpleNamespace(
                returncode=7,
                stderr=f"sips failure {len(calls)}",
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pointer.svg"
            output = root / "pointer.png"
            source.write_text("<svg/>")
            with (
                mock.patch.object(
                    macos_cua.subprocess, "run", side_effect=failing_sips
                ),
                mock.patch.object(
                    macos_cua,
                    "_rasterize_cursor_with_appkit",
                    side_effect=OSError("AppKit conversion failed"),
                ) as appkit,
                mock.patch.object(macos_cua.time, "sleep") as sleep,
                self.assertRaises(macos_cua.CursorRasterError) as raised,
            ):
                macos_cua.cursor_raster_path(source, output)

            message = str(raised.exception)
            self.assertIn("attempt 1/2", message)
            self.assertIn("attempt 2/2", message)
            self.assertIn("sips failure 1", message)
            self.assertIn("sips failure 2", message)
            self.assertIn("AppKit conversion failed", message)
            self.assertNotEqual(message, str(source))
            self.assertFalse(output.exists())
            self.assertEqual(len(calls), 2)
            self.assertNotEqual(calls[0], calls[1])
            self.assertTrue(all(not path.exists() for path in calls))
            sleep.assert_called_once()
            appkit.assert_called_once()
            self.assertFalse(Path(appkit.call_args.args[1]).exists())

    def test_cursor_raster_rejects_missing_or_invalid_sips_output(self):
        cases = {
            "missing": (lambda _path: None, "missing PNG output"),
            "zero": (lambda path: path.write_bytes(b""), "truncated PNG output"),
            "truncated": (
                lambda path: path.write_bytes(b"\x89PNG\r\n\x1a\n"),
                "truncated PNG output",
            ),
            "wrong-size": (
                lambda path: path.write_bytes(self._png_bytes(48, 48)),
                "unexpected cursor raster size",
            ),
            "transparent": (
                lambda path: path.write_bytes(self._png_bytes(alpha=0)),
                "cursor raster has no visible pixels",
            ),
        }
        for label, (write_output, expected_error) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "pointer.svg"
                output = root / "pointer.png"
                source.write_text("<svg/>")
                calls = []

                def fake_sips(arguments, **_kwargs):
                    temporary = Path(arguments[-1])
                    calls.append(temporary)
                    write_output(temporary)
                    return SimpleNamespace(returncode=0, stderr=f"{label} diagnostic")

                with (
                    mock.patch.object(
                        macos_cua.subprocess, "run", side_effect=fake_sips
                    ),
                    mock.patch.object(
                        macos_cua,
                        "_rasterize_cursor_with_appkit",
                        side_effect=OSError(f"{label} AppKit failure"),
                    ),
                    mock.patch.object(macos_cua.time, "sleep"),
                    self.assertRaises(macos_cua.CursorRasterError) as raised,
                ):
                    macos_cua.cursor_raster_path(source, output)

                message = str(raised.exception)
                self.assertIn(expected_error, message)
                self.assertIn(f"{label} diagnostic", message)
                self.assertIn(f"{label} AppKit failure", message)
                self.assertIn("attempt 1/2", message)
                self.assertIn("attempt 2/2", message)
                self.assertEqual(len(calls), 2)
                self.assertTrue(all(not path.exists() for path in calls))
                self.assertFalse(output.exists())

    def test_cursor_raster_regenerates_corrupt_or_wrong_size_fresh_cache(self):
        cache_cases = {
            "corrupt": b"not a png",
            "wrong-size": self._png_bytes(48, 48),
            "transparent": self._png_bytes(alpha=0),
        }
        for label, cached_bytes in cache_cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "pointer.svg"
                output = root / "pointer.png"
                source.write_text("<svg/>")
                output.write_bytes(cached_bytes)
                fresh = source.stat().st_mtime + 10
                os.utime(output, (fresh, fresh))
                calls = []

                def fake_sips(arguments, **_kwargs):
                    calls.append(Path(arguments[-1]))
                    Path(arguments[-1]).write_bytes(self._png_bytes())
                    return SimpleNamespace(returncode=0, stderr="")

                with mock.patch.object(
                    macos_cua.subprocess, "run", side_effect=fake_sips
                ):
                    result = macos_cua.cursor_raster_path(source, output)

                self.assertEqual(Path(result), output)
                self.assertEqual(output.read_bytes(), self._png_bytes())
                self.assertEqual(len(calls), 1)

    def test_cursor_raster_uses_unique_temporary_paths_across_calls(self):
        calls = []

        def fake_sips(arguments, **_kwargs):
            temporary = Path(arguments[-1])
            calls.append(temporary)
            temporary.write_bytes(self._png_bytes())
            return SimpleNamespace(returncode=0, stderr="")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "pointer.svg"
            output = root / "pointer.png"
            source.write_text("<svg/>")
            with mock.patch.object(
                macos_cua.subprocess, "run", side_effect=fake_sips
            ):
                macos_cua.cursor_raster_path(source, output)
                output.unlink()
                macos_cua.cursor_raster_path(source, output)

            self.assertEqual(len(calls), 2)
            self.assertNotEqual(calls[0], calls[1])
            self.assertTrue(all(not path.exists() for path in calls))

    def test_cursor_raster_supports_basename_output(self):
        def fake_sips(arguments, **_kwargs):
            Path(arguments[-1]).write_bytes(self._png_bytes())
            return SimpleNamespace(returncode=0, stderr="")

        previous_directory = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "pointer.svg"
                source.write_text("<svg/>")
                os.chdir(root)
                with mock.patch.object(
                    macos_cua.subprocess, "run", side_effect=fake_sips
                ):
                    result = macos_cua.cursor_raster_path(source, "pointer.png")

                self.assertEqual(result, "pointer.png")
                self.assertEqual(Path(result).read_bytes(), self._png_bytes())
        finally:
            os.chdir(previous_directory)

    def test_operator_update_surfaces_cursor_raster_failure(self):
        with (
            mock.patch.object(
                macos_cua,
                "cursor_raster_path",
                side_effect=macos_cua.CursorRasterError("raster failed"),
            ),
            mock.patch.object(macos_cua, "_operator_ui") as operator_ui,
        ):
            result = macos_cua.operator_update("Fixture")

        self.assertFalse(result["ok"])
        self.assertIn("raster failed", result["error"])
        operator_ui.assert_not_called()

    def test_app_state_fails_closed_when_required_cursor_proof_fails(self):
        raw = {
            "tree_markdown": "AXWindow " + ("content " * 20),
            "elements": [],
            "screenshot_file_path": "/tmp/raw-state.png",
            "screenshot_width": 200,
            "screenshot_height": 100,
        }
        with (
            mock.patch.object(macos_cua, "snapshot", return_value=raw),
            mock.patch.object(
                macos_cua, "_operator_cursor", return_value={"x": 0.5, "y": 0.5}
            ),
            mock.patch.object(
                macos_cua,
                "annotate_cursor_screenshot",
                return_value={
                    "ok": False,
                    "error": "cursor rasterization failed after 2 attempts",
                    "path": "/tmp/raw-state.png",
                },
            ),
            mock.patch.object(
                macos_cua,
                "operator_update",
                return_value={"ok": False, "error": "operator raster failed"},
            ),
        ):
            result = macos_cua.app_state(
                "Fixture", 10, 20, prepare_foreground=False
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "cursor_proof_failed")
        self.assertIn("after 2 attempts", result["cursor_proof_error"])
        self.assertEqual(result["operator_error"], "operator raster failed")
        self.assertEqual(result["screenshot"]["path"], "/tmp/raw-state.png")
        self.assertFalse(result["screenshot"]["cursor_included"])

    def test_legacy_cursor_configuration_fails_closed_on_raster_error(self):
        with (
            mock.patch.object(
                macos_cua,
                "cursor_raster_path",
                side_effect=macos_cua.CursorRasterError("raster failed"),
            ),
            mock.patch.object(macos_cua, "ensure_session") as ensure_session,
            mock.patch.object(macos_cua, "call_driver") as call_driver,
            self.assertRaisesRegex(macos_cua.CursorRasterError, "raster failed"),
        ):
            macos_cua.configure_cursor_icon("test")

        ensure_session.assert_not_called()
        call_driver.assert_not_called()

    def test_native_cursor_defaults_to_custom_raster_not_generic_arrow(self):
        calls = []
        with (
            mock.patch.object(macos_cua, "ensure_session"),
            mock.patch.object(
                macos_cua, "cursor_raster_path", return_value="/tmp/hermes-pointer.png"
            ),
            mock.patch.object(macos_cua.os.path, "isfile", return_value=True),
            mock.patch.object(
                macos_cua,
                "call_driver",
                side_effect=lambda tool, params, **_kwargs: (
                    calls.append((tool, params)) or {"ok": True}
                ),
            ),
            mock.patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("MACOS_CUA_CURSOR_ARROW", None)
            result = macos_cua.configure_cursor_icon("test")

        self.assertTrue(result["ok"])
        self.assertFalse(result["using_generic_arrow"])
        self.assertEqual(calls[0][0], "set_agent_cursor_style")
        self.assertEqual(calls[0][1]["image_path"], "/tmp/hermes-pointer.png")
        self.assertEqual(calls[1][1]["cursor_icon"], "/tmp/hermes-pointer.png")
        self.assertTrue(calls[1][1]["cursor_label"].startswith("macos-cua · "))


class OperatorUITests(unittest.TestCase):
    def test_visible_cursor_overlay_matches_hermes_render_contract(self):
        operator = Path(__file__).parents[1] / "operator"
        source = "\n".join(path.read_text() for path in sorted(operator.glob("*.swift")))
        self.assertIn("cursorOverlayPanel.level = .popUpMenu", source)
        self.assertNotIn("cursorOverlayPanel.level = .screenSaver", source)
        self.assertIn("order(.above, relativeTo:", source)
        self.assertIn("isCursorPointVisible", source)
        self.assertIn("macos-cua · \\(agent)", source)
        self.assertNotIn("Using your computer", source)
        self.assertNotIn("Esc to cancel", source)
        self.assertIn("cursorOverlayPanel.ignoresMouseEvents = true", source)
        self.assertIn("width: 28, height: 28", source)
        self.assertIn("cyanGlow.shadowBlurRadius = 12", source)
        self.assertIn("depthShadow.shadowBlurRadius = 4", source)
        self.assertIn('forKey: "hermes-pointer-idle"', source)
        self.assertIn('stateURL.path + ".lock"', source)
        self.assertIn("flock(lockDescriptor, LOCK_EX)", source)

    def test_bundle_and_launch_agent_contracts(self):
        originals = {
            name: getattr(operator_ui, name)
            for name in (
                "APP_BUNDLE",
                "BINARY",
                "INFO_PLIST",
                "LAUNCH_AGENT",
                "STATE",
                "LOG_FILE",
            )
        }
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                operator_ui.APP_BUNDLE = root / "Operator.app"
                operator_ui.BINARY = operator_ui.APP_BUNDLE / "Contents/MacOS/operator"
                operator_ui.INFO_PLIST = operator_ui.APP_BUNDLE / "Contents/Info.plist"
                operator_ui.LAUNCH_AGENT = root / "operator.plist"
                operator_ui.STATE = root / "state.json"
                operator_ui.LOG_FILE = root / "operator.log"
                operator_ui._write_bundle_metadata()
                operator_ui._write_launch_agent()

                with operator_ui.INFO_PLIST.open("rb") as handle:
                    bundle = plistlib.load(handle)
                with operator_ui.LAUNCH_AGENT.open("rb") as handle:
                    service = plistlib.load(handle)
                self.assertTrue(bundle["LSUIElement"])
                self.assertEqual(
                    bundle["CFBundleIdentifier"], operator_ui.SERVICE_LABEL
                )
                self.assertTrue(service["KeepAlive"])
                self.assertEqual(
                    service["ProgramArguments"][0], str(operator_ui.BINARY)
                )
        finally:
            for name, value in originals.items():
                setattr(operator_ui, name, value)

    def test_process_record_tracks_the_built_binary_version(self):
        original_binary = operator_ui.BINARY
        original_pid_file = operator_ui.PID_FILE
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                operator_ui.BINARY = root / "operator"
                operator_ui.PID_FILE = root / "operator.pid"
                operator_ui.BINARY.write_text("v1")
                operator_ui._atomic_json(
                    operator_ui.PID_FILE,
                    {
                        "binary_mtime_ns": operator_ui.BINARY.stat().st_mtime_ns,
                        "pid": 123,
                    },
                )

                self.assertTrue(operator_ui._process_uses_current_binary())
                operator_ui.BINARY.write_text("v2")
                os.utime(
                    operator_ui.BINARY,
                    ns=(
                        operator_ui.BINARY.stat().st_atime_ns,
                        operator_ui.BINARY.stat().st_mtime_ns + 1,
                    ),
                )
                self.assertFalse(operator_ui._process_uses_current_binary())
        finally:
            operator_ui.BINARY = original_binary
            operator_ui.PID_FILE = original_pid_file

    def test_state_contract_is_harness_independent(self):
        original_state = operator_ui.STATE
        original_pid_file = operator_ui.PID_FILE
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                operator_ui.STATE = root / "state.json"
                operator_ui.PID_FILE = root / "operator.pid"
                result = operator_ui.update(
                    start=False,
                    active=True,
                    app="Calculator",
                    pid=10,
                    window_id=20,
                    screenshot_path="/tmp/calculator.png",
                    status="observing",
                    harness="Cursor",
                    cursor_x=0.25,
                    cursor_y=0.75,
                    cursor_screen_x=640,
                    cursor_screen_y=480,
                    cursor_visible=True,
                    cursor_image_path=macos_cua.CURSOR_ICON,
                )

                state = result["state"]
                self.assertTrue(result["ok"])
                self.assertEqual(state["harness"], "Cursor")
                self.assertEqual(state["window_id"], 20)
                self.assertEqual(state["screenshot_path"], "/tmp/calculator.png")
                self.assertEqual(state["cursor_x"], 0.25)
                self.assertEqual(state["cursor_screen_x"], 640)
                self.assertTrue(state["cursor_visible"])
                self.assertTrue(state["cursor_update_id"])
                self.assertTrue(operator_ui.STATE.is_file())

                previous_update_id = state["cursor_update_id"]
                operator_ui._atomic_json(
                    operator_ui.STATE,
                    {
                        **state,
                        "cursor_rendered_x": 0.25,
                        "cursor_rendered_y": 0.75,
                        "cursor_rendered_update_id": previous_update_id,
                    },
                )
                moved_again = operator_ui.update(
                    start=False,
                    active=True,
                    app="Calculator",
                    pid=10,
                    window_id=20,
                    cursor_x=0.25,
                    cursor_y=0.75,
                )
                self.assertNotEqual(
                    moved_again["state"]["cursor_update_id"], previous_update_id
                )
                self.assertNotIn("cursor_rendered_update_id", moved_again["state"])

                switched = operator_ui.update(
                    start=False,
                    active=True,
                    app="TextEdit",
                    pid=11,
                    window_id=21,
                    screenshot_path="/tmp/textedit.png",
                )
                self.assertEqual(switched["state"]["screenshot_path"], "/tmp/textedit.png")
                self.assertFalse(switched["state"]["cursor_visible"])
                self.assertNotIn("cursor_screen_x", switched["state"])

                changed = operator_ui.update(
                    start=False,
                    active=True,
                    app="Finder",
                    status="observing",
                    harness="Cursor",
                )
                self.assertEqual(changed["state"]["screenshot_path"], "")
                self.assertFalse(changed["state"]["cursor_visible"])

                inactive = operator_ui.update(
                    start=False,
                    active=False,
                    status="idle",
                    message="No controlled app",
                )
                inactive_state = inactive["state"]
                self.assertEqual(inactive_state["app"], "")
                self.assertEqual(inactive_state["screenshot_path"], "")
                self.assertEqual(inactive_state["raw_screenshot_path"], "")
                self.assertFalse(inactive_state["cursor_visible"])
                for stale_key in (
                    "pid",
                    "window_id",
                    "snapshot_id",
                    "window_frame",
                    "cursor_x",
                    "cursor_y",
                    "cursor_update_id",
                    "cursor_rendered_update_id",
                ):
                    self.assertNotIn(stale_key, inactive_state)
        finally:
            operator_ui.STATE = original_state
            operator_ui.PID_FILE = original_pid_file

    def test_state_lock_prevents_stale_ack_from_overwriting_new_publish(self):
        original_state = operator_ui.STATE
        original_pid_file = operator_ui.PID_FILE
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                operator_ui.STATE = root / "state.json"
                operator_ui.PID_FILE = root / "operator.pid"
                operator_ui._atomic_json(
                    operator_ui.STATE,
                    {
                        "app": "Calculator",
                        "pid": 10,
                        "window_id": 20,
                        "cursor_update_id": "A",
                        "cursor_x": 0.1,
                        "cursor_y": 0.2,
                    },
                )
                publisher_read = threading.Event()
                original_read = operator_ui._read_json

                def observed_read(path):
                    if threading.current_thread().name == "publisher-B":
                        publisher_read.set()
                    return original_read(path)

                def publish_b():
                    operator_ui.update(
                        start=False,
                        app="Calculator",
                        pid=10,
                        window_id=20,
                        cursor_x=0.7,
                        cursor_y=0.8,
                        cursor_update_id="B",
                    )

                with mock.patch.object(
                    operator_ui, "_read_json", side_effect=observed_read
                ):
                    with operator_ui._state_lock():
                        stale = original_read(operator_ui.STATE)
                        publisher = threading.Thread(
                            target=publish_b, name="publisher-B"
                        )
                        publisher.start()
                        self.assertFalse(publisher_read.wait(0.05))
                        operator_ui._atomic_json(
                            operator_ui.STATE,
                            {
                                **stale,
                                "cursor_rendered_x": 0.1,
                                "cursor_rendered_y": 0.2,
                                "cursor_rendered_update_id": "A",
                            },
                        )
                    publisher.join(timeout=1)

                self.assertFalse(publisher.is_alive())
                self.assertTrue(publisher_read.is_set())
                final = original_read(operator_ui.STATE)
                self.assertEqual(final["cursor_update_id"], "B")
                self.assertNotIn("cursor_rendered_update_id", final)
        finally:
            operator_ui.STATE = original_state
            operator_ui.PID_FILE = original_pid_file

    def test_pip_visibility_is_explicit_and_reversible(self):
        with mock.patch.object(operator_ui, "update", return_value={"ok": True}) as update:
            shown = operator_ui.set_pip_visible(True)
            hidden = operator_ui.set_pip_visible(False)

        self.assertTrue(shown["pip_visible"])
        self.assertFalse(hidden["pip_visible"])
        self.assertEqual(
            [call.kwargs for call in update.call_args_list],
            [{"pip_visible": True}, {"pip_visible": False}],
        )


class HarnessInstallTests(unittest.TestCase):
    def test_install_is_idempotent_and_keeps_single_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            skills = Path(directory)
            first = install_harness.install_link(skills)
            second = install_harness.install_link(skills)

            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertTrue((skills / "macos-cua").is_symlink())
            self.assertEqual(
                (skills / "macos-cua").resolve(), install_harness.SKILL_DIR
            )

    def test_replace_copy_retargets_stale_skill_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            skills = Path(directory)
            other = Path(directory) / "other-macos-cua"
            other.mkdir()
            (other / "SKILL.md").write_text("stale\n")
            (other / "scripts").mkdir()
            (other / "scripts" / "macos-cua.py").write_text("# stale\n")
            stale = skills / "macos-cua"
            stale.symlink_to(other, target_is_directory=True)
            refused = install_harness.install_link(skills)
            self.assertFalse(refused["ok"])
            linked = install_harness.install_link(skills, replace_copy=True)
            self.assertTrue(linked["ok"])
            self.assertTrue(linked["changed"])
            self.assertEqual(stale.resolve(), install_harness.SKILL_DIR)


class KeyboardTests(unittest.TestCase):
    def test_right_click_prefers_native_show_menu(self):
        state = {
            "elements": [
                {
                    "element_index": 3,
                    "role": "AXTextArea",
                    "actions": ["AXShowMenu"],
                }
            ],
            "tree_markdown": "[3] AXTextArea",
        }
        with (
            mock.patch.object(macos_cua, "_native_ax_snapshot", return_value=state),
            mock.patch.object(
                macos_cua,
                "perform_action",
                return_value={"ok": True, "path": "native_ax"},
            ),
            mock.patch.object(macos_cua, "call_driver") as driver,
        ):
            result = macos_cua.right_click(10, 20, 3)

        self.assertEqual(result["path"], "native_ax")
        driver.assert_not_called()

    def test_background_cmd_a_requires_native_text_readback_when_available(self):
        with (
            mock.patch.object(macos_cua, "call_driver") as driver,
            mock.patch.object(
                macos_cua,
                "_verify_or_repair_native_select_all",
                return_value={
                    "ok": True,
                    "path": "hotkey+native_ax_readback",
                    "verified": True,
                    "repaired": True,
                },
            ) as verify,
        ):
            result = macos_cua.press_key(10, 20, "cmd+a", "background")

        self.assertTrue(result["verified"])
        self.assertTrue(result["repaired"])
        driver.assert_not_called()
        verify.assert_called_once_with(10)

    def test_system_events_key_targets_exact_pid_and_requires_readback(self):
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(
            macos_cua.subprocess, "run", return_value=completed
        ) as run:
            result = macos_cua.press_key(
                32734, 125239, "Return", "system_events"
            )

        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["osascript", "-e"])
        self.assertIn("unix id is 32734", command[2])
        self.assertIn("key code 36", command[2])
        self.assertIs(run.call_args.kwargs["stdin"], macos_cua.subprocess.DEVNULL)
        self.assertEqual(result["path"], "system_events_pid")
        self.assertTrue(result["accepted"])
        self.assertFalse(result["verified"])

    def test_system_events_key_rejects_unknown_key_before_dispatch(self):
        with mock.patch.object(macos_cua.subprocess, "run") as run:
            result = macos_cua.press_key(
                32734, 125239, "not-a-real-key", "system_events"
            )
        run.assert_not_called()
        self.assertIn("unsupported System Events key", result["error"])

    def test_combo_uses_hotkey_with_normalized_modifiers(self):
        calls = []
        original_call_driver = macos_cua.call_driver
        foreground = None
        try:
            macos_cua.call_driver = lambda tool, params: (
                calls.append((tool, params)) or {"ok": True}
            )
            foreground = mock.patch.object(
                macos_cua, "bring_resolved_window_to_front", return_value={"ok": True}
            )
            foreground.start()

            macos_cua.press_key(10, 20, "super+shift+A", "foreground")

            self.assertEqual(
                calls,
                [
                    (
                        "hotkey",
                        {
                            "pid": 10,
                            "window_id": 20,
                            "keys": ["cmd", "shift", "a"],
                            "delivery_mode": "foreground",
                        },
                    )
                ],
            )
        finally:
            if foreground is not None:
                foreground.stop()
            macos_cua.call_driver = original_call_driver

    def test_single_key_uses_press_key(self):
        calls = []
        original_call_driver = macos_cua.call_driver
        try:
            macos_cua.call_driver = lambda tool, params: (
                calls.append((tool, params)) or {"ok": True}
            )
            macos_cua.press_key(10, 20, "Escape")

            self.assertEqual(calls[0][0], "press_key")
            self.assertEqual(calls[0][1]["key"], "escape")
            self.assertEqual(calls[0][1]["delivery_mode"], "background")
        finally:
            macos_cua.call_driver = original_call_driver

    def test_press_key_uses_native_pid_when_driver_session_ended(self):
        ended = {
            "refusal": {
                "code": "session_ended",
                "message": "this session has ended; call start_session explicitly to reuse its label",
            },
            "status": "refused",
        }
        native = SimpleNamespace(
            press_key_after_dropped_session=mock.Mock(
                return_value={
                    "ok": True,
                    "accepted": True,
                    "path": "native_cg_key",
                    "session_recovered": True,
                }
            )
        )
        with (
            mock.patch.object(macos_cua, "call_driver", return_value=ended),
            mock.patch.object(macos_cua, "_native_input", return_value=native),
            mock.patch.object(
                macos_cua, "bring_resolved_window_to_front", return_value={"ok": True}
            ),
        ):
            result = macos_cua.press_key(10, 20, "Escape", "foreground")

        self.assertTrue(result["ok"])
        self.assertTrue(result["session_recovered"])
        self.assertEqual(result["path"], "native_cg_key")
        native.press_key_after_dropped_session.assert_called_once()

    def test_press_key_rejects_recovered_global_input(self):
        recovered = {
            "delivery": {"mode": "foreground"},
            "effect": "unverifiable",
            "route": "global_input",
            "session_recovered": True,
        }
        native = SimpleNamespace(
            press_key_after_dropped_session=mock.Mock(
                return_value={
                    "ok": True,
                    "accepted": True,
                    "path": "native_cg_key",
                    "session_recovered": True,
                }
            )
        )
        with (
            mock.patch.object(macos_cua, "call_driver", return_value=recovered),
            mock.patch.object(macos_cua, "_native_input", return_value=native),
            mock.patch.object(
                macos_cua, "bring_resolved_window_to_front", return_value={"ok": True}
            ),
        ):
            result = macos_cua.press_key(10, 20, "Escape", "foreground")

        self.assertEqual(result["path"], "native_cg_key")
        native.press_key_after_dropped_session.assert_called_once()

    def test_background_key_retries_once_when_foreground_recommended(self):
        calls = []

        def driver(tool, params):
            calls.append((tool, params))
            if params.get("delivery_mode") == "background":
                return {
                    "ok": False,
                    "code": "off_space_or_ax_unresolved",
                    "escalation": {"recommended": "foreground"},
                }
            return {"ok": True}

        with (
            mock.patch.object(macos_cua, "call_driver", side_effect=driver),
            mock.patch.object(
                macos_cua, "bring_resolved_window_to_front", return_value={"ok": True}
            ),
        ):
            result = macos_cua.press_key(10, 20, "cmd+c")

        self.assertTrue(result["ok"])
        self.assertEqual(result["escalated"], "foreground")
        self.assertEqual(calls[0][1]["delivery_mode"], "background")
        self.assertEqual(calls[1][1]["delivery_mode"], "foreground")

    def test_key_list_is_normalized_before_driver_dispatch(self):
        calls = []
        with mock.patch.object(
            macos_cua,
            "call_driver",
            side_effect=lambda tool, params: calls.append((tool, params)) or {"ok": True},
        ):
            result = macos_cua.press_key(10, 20, ["cmd", "shift", "A"])

        self.assertTrue(result["ok"])
        self.assertEqual(calls[0][0], "hotkey")
        self.assertEqual(calls[0][1]["keys"], ["cmd", "shift", "a"])

    def test_invalid_key_list_returns_structured_error(self):
        with mock.patch.object(macos_cua, "call_driver") as call_driver:
            result = macos_cua.press_key(10, 20, ["cmd", ""])

        call_driver.assert_not_called()
        self.assertIn("non-empty string", result["error"])

    def test_hold_key_posts_down_waits_and_posts_up(self):
        calls = []
        with (
            mock.patch.object(
                macos_cua,
                "_post_key_event",
                side_effect=lambda pid, code, down, mode: calls.append(
                    (pid, code, down, mode)
                ),
            ),
            mock.patch.object(macos_cua.time, "sleep") as sleep,
        ):
            result = macos_cua.hold_key(10, "w", 0.75)

        self.assertTrue(result["ok"])
        self.assertEqual(
            calls,
            [(10, 13, True, "background"), (10, 13, False, "background")],
        )
        sleep.assert_called_once_with(0.75)

    def test_hold_key_releases_after_wait_failure(self):
        calls = []
        with (
            mock.patch.object(
                macos_cua,
                "_post_key_event",
                side_effect=lambda pid, code, down, mode: calls.append(
                    (pid, code, down, mode)
                ),
            ),
            mock.patch.object(
                macos_cua.time, "sleep", side_effect=RuntimeError("stop")
            ),
        ):
            with self.assertRaises(RuntimeError):
                macos_cua.hold_key(10, "w", 0.5)

        self.assertEqual(
            calls,
            [(10, 13, True, "background"), (10, 13, False, "background")],
        )

    def test_hold_key_foreground_fronts_window_and_targets_pid(self):
        calls = []
        with (
            mock.patch.object(
                macos_cua,
                "bring_resolved_window_to_front",
                return_value={"ok": True},
            ) as foreground,
            mock.patch.object(
                macos_cua,
                "_post_key_event",
                side_effect=lambda pid, code, down, mode: calls.append(
                    (pid, code, down, mode)
                ),
            ),
            mock.patch.object(macos_cua.time, "sleep"),
        ):
            result = macos_cua.hold_key(
                10, "w", 0.25, window_id=20, foreground=True
            )

        foreground.assert_called_once_with(10, 20)
        self.assertEqual(result["delivery_mode"], "foreground_pid")
        self.assertEqual(
            calls,
            [(10, 13, True, "foreground_pid"), (10, 13, False, "foreground_pid")],
        )

    def test_physical_pointer_warp_path_is_not_present(self):
        source = SCRIPT.read_text()

        self.assertNotIn("CGWarpMouseCursorPosition", source)
        self.assertNotIn("physical_mouse_look", source)
        self.assertNotIn('add_parser("mouse-look"', source)


class StateEnrichmentTests(unittest.TestCase):
    def test_snapshot_fails_closed_when_claimed_capture_never_materializes(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "missing.png")
            packet = {
                "tree_markdown": "x" * 100,
                "elements": [],
                "screenshot_file_path": missing,
            }
            with mock.patch.object(macos_cua, "call_driver", return_value=packet):
                result = macos_cua.snapshot(
                    10,
                    20,
                    include_screenshot=True,
                    screenshot_out_file=missing,
                    retries=0,
                    delay=0,
                )

        self.assertEqual(result["error"], "screenshot artifact did not materialize")
        self.assertEqual(result["capture_path"], missing)

    def test_resolution_reset_preserves_operator_json(self):
        original_cache = macos_cua.CACHE_DIR
        try:
            with tempfile.TemporaryDirectory() as directory:
                macos_cua.CACHE_DIR = directory
                resolution = Path(directory) / "Calculator.json"
                operator = Path(directory) / "operator-state.json"
                resolution.write_text('{"pid": 1, "window_id": 2, "ts": 3}')
                operator.write_text('{"pid": 4, "window_id": 5, "status": "active"}')

                removed = macos_cua.clear_resolution_cache()

                self.assertEqual(removed, [str(resolution)])
                self.assertFalse(resolution.exists())
                self.assertTrue(operator.exists())
        finally:
            macos_cua.CACHE_DIR = original_cache

    def test_static_child_text_is_attached_to_cell_and_row(self):
        state = {
            "tree_markdown": (
                '- [0] AXWindow "Downloads"\n'
                "    - [1] AXRow\n"
                "        - [2] AXCell [actions=[open]]\n"
                '            - AXStaticText = "Documents"\n'
            ),
            "elements": [
                {"element_index": 0, "role": "AXWindow", "parent_index": None},
                {"element_index": 1, "role": "AXRow", "parent_index": 0},
                {"element_index": 2, "role": "AXCell", "parent_index": 1},
            ],
        }

        macos_cua._enrich_elements(state)

        self.assertEqual(state["elements"][2]["derived_text"], "Documents")
        self.assertEqual(state["elements"][1]["derived_text"], "Documents")
        self.assertEqual(macos_cua.find_clickable_index(state, "Documents"), 1)

    def test_native_static_child_text_resolves_sidebar_row(self):
        elements = [
            {"element_index": 1, "role": "AXOutline", "parent_index": None},
            {"element_index": 2, "role": "AXRow", "parent_index": 1, "label": "", "value": ""},
            {"element_index": 3, "role": "AXCell", "parent_index": 2, "label": "", "value": ""},
            {
                "element_index": 4,
                "role": "AXStaticText",
                "parent_index": 3,
                "label": "",
                "value": "Downloads",
            },
        ]
        macos_cua._attach_static_child_text(elements)
        state = {"elements": elements, "tree_markdown": ""}
        self.assertEqual(elements[2]["value"], "Downloads")
        self.assertEqual(elements[1]["value"], "Downloads")
        self.assertEqual(macos_cua.find_clickable_index(state, "Downloads"), 2)

    def test_native_text_field_child_resolves_open_sheet_row(self):
        elements = [
            {"element_index": 1, "role": "AXOutline", "parent_index": None},
            {"element_index": 2, "role": "AXRow", "parent_index": 1, "label": "", "value": ""},
            {"element_index": 3, "role": "AXCell", "parent_index": 2, "label": "", "value": ""},
            {
                "element_index": 4,
                "role": "AXTextField",
                "parent_index": 3,
                "label": "",
                "value": "wa-probe-acu15.txt",
            },
        ]
        macos_cua._attach_static_child_text(elements)
        state = {"elements": elements, "tree_markdown": ""}
        self.assertEqual(elements[1]["value"], "wa-probe-acu15.txt")
        self.assertEqual(macos_cua.find_clickable_index(state, "wa-probe-acu15.txt"), 2)

    def test_choose_walk_roots_prefers_sheet_over_windows(self):
        windows = ["chrome", "chat"]
        self.assertEqual(macos_cua.choose_walk_roots(windows, ["sheet"]), ["sheet"])
        self.assertEqual(macos_cua.choose_walk_roots(windows, []), ["chrome"])
        self.assertEqual(macos_cua.choose_walk_roots([], []), [])
        self.assertEqual(
            macos_cua.choose_walk_roots(windows, [], menus=["ctx"]),
            ["ctx", "chrome"],
        )
        with self.assertRaises(TypeError):
            macos_cua.choose_walk_roots(windows, [], menus=["ctx"], menubar="bar")

    def test_glide_uses_popover_frame_when_window_missing(self):
        snap = {
            "elements": [
                {
                    "element_index": 1,
                    "role": "AXPopover",
                    "frame": {"x": 10, "y": 20, "w": 200, "h": 100},
                },
                {
                    "element_index": 2,
                    "role": "AXStaticText",
                    "label": "Message yourself",
                    "frame": {"x": 20, "y": 30, "w": 80, "h": 20},
                },
            ]
        }
        frame = macos_cua._glide_container_frame(snap, (40, 40))
        self.assertEqual(frame["w"], 200)
        self.assertEqual(frame["h"], 100)

    def test_glide_omits_stale_screen_point_without_quartz(self):
        snap = {
            "elements": [
                {
                    "element_index": 1,
                    "role": "AXWindow",
                    "frame": {"x": 0, "y": 0, "w": 200, "h": 100},
                },
                {
                    "element_index": 2,
                    "role": "AXButton",
                    "label": "7",
                    "frame": {"x": 20, "y": 20, "w": 20, "h": 20},
                },
            ]
        }
        with (
            mock.patch.object(
                macos_cua, "_live_window_frame", side_effect=AssertionError("hot path")
            ),
            mock.patch.object(
                macos_cua,
                "operator_update",
                return_value={"ok": True, "state": {"cursor_update_id": "u1"}},
            ) as update,
            mock.patch.object(
                macos_cua, "_wait_for_operator_cursor", return_value={"ok": True}
            ),
        ):
            result = macos_cua.glide_operator_to_element(
                "Calculator", 11, 22, snap, 2
            )
        kwargs = update.call_args.kwargs
        self.assertTrue(result["ok"])
        self.assertAlmostEqual(kwargs["cursor_x"], 0.15)
        self.assertAlmostEqual(kwargs["cursor_y"], 0.3)
        self.assertIsNone(kwargs["cursor_screen_x"])
        self.assertIsNone(kwargs["cursor_screen_y"])
        self.assertAlmostEqual(result["coords"]["x"], 30)
        self.assertAlmostEqual(result["coords"]["y"], 30)

    def test_display_packet_reports_asleep_configured_without_failing(self):
        active = [
            {
                "id": 1,
                "name": "Built-in",
                "main": True,
                "x": 0,
                "y": 0,
                "width": 1440,
                "height": 900,
                "scale": 2,
            }
        ]
        with (
            mock.patch.object(displays, "list_displays", return_value=active),
            mock.patch.object(displays, "_cg_display_count", return_value=2),
        ):
            packet = displays.display_packet()
            self.assertEqual(packet["display_count_active"], 1)
            self.assertEqual(packet["display_count_configured"], 2)
            self.assertEqual(len(packet["displays"]), 1)
            self.assertNotIn("target_window_display", packet)
            with mock.patch.dict(os.environ, {"MACOS_CUA_DISPLAY": "DELL"}):
                pinned = displays.display_packet()
            self.assertEqual(pinned.get("pin_error"), "pinned display is not active")

    def test_window_local_cursor_clears_stale_screen_point(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            operator_ui.STATE = root / "state.json"
            operator_ui.PID_FILE = root / "operator.pid"
            operator_ui.update(
                start=False,
                active=True,
                app="Calculator",
                cursor_x=0.2,
                cursor_y=0.3,
                cursor_screen_x=10,
                cursor_screen_y=20,
            )
            cleared = operator_ui.update(
                start=False,
                active=True,
                app="Calculator",
                cursor_x=0.4,
                cursor_y=0.5,
            )
            self.assertNotIn("cursor_screen_x", cleared["state"])
            self.assertNotIn("cursor_screen_y", cleared["state"])
            self.assertEqual(cleared["state"]["cursor_x"], 0.4)

    def test_typed_text_is_proven_fails_closed_on_empty_ax(self):
        self.assertTrue(macos_cua.typed_text_is_proven("hello", "hello there"))
        self.assertTrue(macos_cua.typed_text_is_proven("hello", "", "hello"))
        self.assertFalse(macos_cua.typed_text_is_proven("hello", "", ""))
        self.assertFalse(macos_cua.typed_text_is_proven("hello", None, None))

    def test_compact_state_hides_closed_menu_descendants(self):
        elements = [
            {
                "element_index": 0,
                "role": "AXWindow",
                "label": "Calculator",
                "frame": {"x": 1, "y": 1, "w": 200, "h": 300},
            },
            {
                "element_index": 1,
                "role": "AXButton",
                "label": "Equals",
                "frame": {"x": 10, "y": 10, "w": 40, "h": 40},
            },
            {"element_index": 2, "role": "AXMenuItem", "label": "Hidden Recent Item"},
            {
                "element_index": 3,
                "role": "AXMenuItem",
                "label": "Visible Menu Item",
                "frame": {"x": 40, "y": 40, "w": 120, "h": 22},
            },
        ]

        compact = macos_cua._state_elements(elements)
        text = macos_cua._state_text("Calculator", compact)

        self.assertEqual([e["element_index"] for e in compact], [0, 1, 3])
        self.assertIn("[1] AXButton", text)
        self.assertIn("Visible Menu Item", text)
        self.assertNotIn("Hidden Recent Item", text)

    def test_compact_state_keeps_frameless_items_under_open_menu(self):
        elements = [
            {
                "element_index": 1,
                "role": "AXMenu",
                "label": "File",
                "frame": {"x": 10, "y": 10, "w": 160, "h": 80},
            },
            {
                "element_index": 2,
                "role": "AXMenuItem",
                "label": "New",
                "parent_index": 1,
            },
            {"element_index": 3, "role": "AXMenuItem", "label": "Recent Closed"},
        ]
        compact = macos_cua._state_elements(elements)
        self.assertEqual([e["element_index"] for e in compact], [1, 2])

    def test_compact_state_query_is_progressive(self):
        elements = [
            {
                "element_index": 0,
                "role": "AXWindow",
                "label": "Calculator",
                "frame": {"x": 1, "y": 1, "w": 200, "h": 300},
            },
            {
                "element_index": 1,
                "role": "AXButton",
                "label": "Seven",
                "frame": {"x": 10, "y": 10, "w": 40, "h": 40},
            },
            {
                "element_index": 2,
                "role": "AXButton",
                "label": "Equals",
                "frame": {"x": 60, "y": 10, "w": 40, "h": 40},
            },
        ]

        text = macos_cua._state_text("Calculator", elements, query="equals")

        self.assertIn("[0] AXWindow", text)
        self.assertIn("[2] AXButton", text)
        self.assertNotIn("[1] AXButton", text)


class PrimitiveContractTests(unittest.TestCase):
    def test_type_text_prefers_focused_native_selection(self):
        with (
            mock.patch.object(
                macos_cua,
                "_native_type_selected_text",
                return_value={"ok": True, "path": "native_ax_selected_text"},
            ) as native,
            mock.patch.object(macos_cua, "call_driver") as driver,
        ):
            result = macos_cua.type_text(10, 20, None, "BETA")

        self.assertEqual(result["path"], "native_ax_selected_text")
        native.assert_called_once_with(10, "BETA", window_id=20, element_index=None)
        driver.assert_not_called()

    def test_native_ax_click_retries_stale_element_with_fresh_label(self):
        stale = {"elements": [{"element_index": 3, "label": "Clear"}]}
        fresh = {"elements": [{"element_index": 8, "label": "Clear"}]}
        with (
            mock.patch.object(
                macos_cua,
                "_native_ax_press",
                side_effect=[
                    {"error": "AXUIElementPerformAction(AXPress) returned -25204"},
                    {"ok": True, "path": "native_ax"},
                ],
            ) as press,
            mock.patch.object(
                macos_cua, "_native_ax_snapshot", return_value=fresh
            ) as snapshot,
            mock.patch.object(
                macos_cua, "find_clickable_index", return_value=8
            ),
        ):
            result, used_snapshot, used_index = (
                macos_cua._native_ax_press_label_with_retry(
                    10, 20, "Clear", stale, 3, max_elements=120
                )
            )

        self.assertTrue(result["ok"])
        self.assertIs(used_snapshot, fresh)
        self.assertEqual(used_index, 8)
        self.assertEqual(press.call_count, 2)
        snapshot.assert_called_once_with(10, max_elements=120, window_id=20)

    def test_native_ax_click_remaps_dynamic_label_by_exact_frame(self):
        stale = {
            "elements": [{
                "element_index": 3, "role": "AXButton", "label": "Clear",
                "frame": {"x": 10, "y": 20, "w": 30, "h": 40},
            }]
        }
        fresh = {
            "elements": [{
                "element_index": 8, "role": "AXButton", "label": "All Clear",
                "frame": {"x": 10, "y": 20, "w": 30, "h": 40},
            }]
        }
        with (
            mock.patch.object(
                macos_cua,
                "_native_ax_press",
                side_effect=[
                    {"error": "AXUIElementPerformAction(AXPress) returned -25204"},
                    {"ok": True, "path": "native_ax"},
                ],
            ),
            mock.patch.object(macos_cua, "_native_ax_snapshot", return_value=fresh),
        ):
            result, _, used_index = macos_cua._native_ax_press_label_with_retry(
                10, 20, "Clear", stale, 3
            )

        self.assertTrue(result["ok"])
        self.assertEqual(used_index, 8)

    def test_native_ax_press_uses_pressable_descendant_on_unsupported_row(self):
        services = SimpleNamespace(
            AXUIElementPerformAction=lambda element, _action: element.code
        )
        snapshot = {
            "elements": [
                {
                    "element_index": 47,
                    "role": "AXRow",
                    "parent_index": 0,
                    "_native_element": SimpleNamespace(code=-25206),
                    "_native_services": services,
                },
                {
                    "element_index": 49,
                    "role": "AXStaticText",
                    "parent_index": 47,
                    "actions": ["AXPress"],
                    "_native_element": SimpleNamespace(code=0),
                    "_native_services": services,
                },
            ]
        }

        result = macos_cua._native_ax_press(snapshot, 47)

        self.assertTrue(result["ok"])
        self.assertTrue(result["pressed_descendant"])
        self.assertEqual(result["element"], 49)
        self.assertEqual(result["requested_element"], 47)

    def test_native_ax_click_retries_unsupported_identity_after_fresh_tree(self):
        stale = {"elements": [{"element_index": 3, "label": "Save"}]}
        fresh = {"elements": [{"element_index": 8, "label": "Save"}]}
        with (
            mock.patch.object(
                macos_cua,
                "_native_ax_press",
                side_effect=[
                    {"error": "AXUIElementPerformAction(AXPress) returned -25206"},
                    {"ok": True, "path": "native_ax"},
                ],
            ) as press,
            mock.patch.object(macos_cua, "_native_ax_snapshot", return_value=fresh),
            mock.patch.object(macos_cua, "find_clickable_index", return_value=8),
        ):
            result, used_snapshot, used_index = (
                macos_cua._native_ax_press_label_with_retry(
                    10, 20, "Save", stale, 3, max_elements=120
                )
            )

        self.assertTrue(result["ok"])
        self.assertIs(used_snapshot, fresh)
        self.assertEqual(used_index, 8)
        self.assertEqual(press.call_count, 2)

    def test_click_retries_one_transient_ax_failure_after_fresh_state(self):
        with (
            mock.patch.object(
                macos_cua,
                "click",
                side_effect=[
                    {"ok": False, "raw": "AX action failed: -25204"},
                    {"ok": True},
                ],
            ) as click,
            mock.patch.object(macos_cua, "snapshot") as snapshot,
        ):
            result = macos_cua.click_with_retry(10, 20, 3)

        self.assertTrue(result["ok"])
        self.assertEqual(click.call_count, 2)
        snapshot.assert_called_once_with(10, 20, max_elements=120)

    def test_click_uses_native_ax_press_when_available(self):
        with (
            mock.patch.object(
                macos_cua,
                "_native_ax_snapshot",
                return_value={"source": "native_ax", "elements": []},
            ) as native_snap,
            mock.patch.object(
                macos_cua,
                "_native_ax_press",
                return_value={"ok": True, "path": "native_ax", "action": "press"},
            ) as native_press,
            mock.patch.object(macos_cua, "snapshot") as snapshot,
            mock.patch.object(macos_cua, "call_driver") as call_driver,
        ):
            result = macos_cua.click(10, 20, 3)

        self.assertTrue(result["ok"])
        self.assertTrue(result["accepted"])
        self.assertEqual(result["path"], "native_ax")
        native_snap.assert_called_once_with(10, max_elements=120, window_id=20)
        native_press.assert_called_once()
        snapshot.assert_not_called()
        call_driver.assert_not_called()

    def test_index_click_glides_visible_cursor_before_pressing(self):
        order = []
        native_snap = {
            "source": "native_ax",
            "tree_markdown": "[3] AXButton 7",
            "elements": [{"element_index": 3, "role": "AXButton", "label": "7"}],
        }
        with (
            mock.patch.object(
                macos_cua, "_native_ax_snapshot", return_value=native_snap
            ),
            mock.patch.object(
                macos_cua,
                "glide_operator_to_element",
                side_effect=lambda *a, **k: order.append("glide")
                or {"ok": True, "move": {"ok": True}, "cursor_normalized": {"x": 0.5, "y": 0.5}},
            ) as glide,
            mock.patch.object(
                macos_cua,
                "_native_ax_press",
                side_effect=lambda *a, **k: order.append("press")
                or {"ok": True, "path": "native_ax"},
            ),
        ):
            result = macos_cua.click(10, 20, 3, app_name="Calculator")

        self.assertEqual(order, ["glide", "press"])
        self.assertEqual(result["method"], "agent-cursor-glide+native-axpress")
        self.assertEqual(result["move"], {"ok": True})
        self.assertEqual(glide.call_args.args[0], "Calculator")

    def test_index_click_refuses_to_press_when_cursor_is_not_visible(self):
        with (
            mock.patch.object(
                macos_cua,
                "_native_ax_snapshot",
                return_value={
                    "source": "native_ax",
                    "tree_markdown": "[3] AXButton 7",
                    "elements": [{"element_index": 3, "role": "AXButton"}],
                },
            ),
            mock.patch.object(
                macos_cua,
                "glide_operator_to_element",
                return_value={
                    "ok": False,
                    "error": "visible operator cursor did not reach the target",
                },
            ),
            mock.patch.object(macos_cua, "_native_ax_press") as press,
            mock.patch.object(macos_cua, "call_driver") as call_driver,
        ):
            result = macos_cua.click(10, 20, 3, app_name="Calculator")

        self.assertFalse(result["accepted"])
        press.assert_not_called()
        call_driver.assert_not_called()

    def test_click_sends_fresh_snapshot_id(self):
        before = {
            "snapshot_id": "snap-click-1",
            "elements": [{"element_index": 3, "role": "AXButton", "label": "7"}],
        }
        with (
            mock.patch.object(
                macos_cua,
                "_native_ax_snapshot",
                return_value={"error": "native unavailable", "elements": []},
            ),
            mock.patch.object(
                macos_cua,
                "_native_ax_press",
                return_value={"error": "native AX element 3 is unavailable"},
            ),
            mock.patch.object(macos_cua, "snapshot", return_value=before) as snapshot,
            mock.patch.object(
                macos_cua, "call_driver", return_value={"ok": True, "accepted": True}
            ) as call_driver,
        ):
            result = macos_cua.click(10, 20, 3)

        self.assertTrue(result["ok"])
        snapshot.assert_called_once_with(10, 20, max_elements=120, retries=1, delay=0.1)
        self.assertEqual(call_driver.call_args.args[0], "click")
        self.assertEqual(call_driver.call_args.args[1]["snapshot_id"], "snap-click-1")
        self.assertEqual(call_driver.call_args.args[1]["element_index"], 3)

    def test_set_value_on_text_requires_matching_ax_readback(self):
        before = {
            "snapshot_id": "snapshot-1",
            "elements": [
                {"element_index": 1, "role": "AXTextArea", "value": "before"}
            ],
            "tree_markdown": "x" * 100,
        }
        after = {
            "elements": [
                {"element_index": 1, "role": "AXTextArea", "value": "after"}
            ],
            "tree_markdown": "x" * 100,
        }
        with (
            mock.patch.object(macos_cua, "snapshot", side_effect=[before, after]),
            mock.patch.object(
                macos_cua,
                "_native_ax_snapshot",
                return_value={"error": "native unavailable", "elements": []},
            ),
            mock.patch.object(
                macos_cua, "call_driver", return_value={"ok": True}
            ) as call_driver,
        ):
            result = macos_cua.set_value(10, 20, 1, "after")

        self.assertTrue(result["verified"])
        self.assertEqual(result["path"], "driver+ax-value-readback")
        self.assertEqual(
            call_driver.call_args.args[1]["snapshot_id"], "snapshot-1"
        )

    def test_set_value_prefers_verified_native_ax(self):
        with (
            mock.patch.object(
                macos_cua,
                "_native_ax_snapshot",
                return_value={
                    "elements": [{"element_index": 4, "role": "AXTextArea"}],
                    "tree_markdown": "[4] AXTextArea",
                },
            ),
            mock.patch.object(
                macos_cua,
                "_native_ax_set_value",
                return_value={"ok": True, "verified": True},
            ) as native_set,
            mock.patch.object(macos_cua, "call_driver") as driver,
        ):
            result = macos_cua.set_value(10, 20, 4, "after")

        self.assertTrue(result["verified"])
        native_set.assert_called_once()
        driver.assert_not_called()

    def test_ax_sequence_accepts_cocoa_style_iterables(self):
        class CocoaArray:
            def __iter__(self):
                return iter(("one", "two"))

        self.assertEqual(macos_cua._ax_sequence(CocoaArray()), ["one", "two"])
        self.assertEqual(macos_cua._ax_sequence("not children"), [])

    def test_driver_version_parser_and_minimum_are_fail_closed(self):
        self.assertEqual(macos_cua._parse_driver_version("cua-driver 0.8.3"), (0, 8, 3))
        self.assertEqual(macos_cua._parse_driver_version("release 12.4.19-beta"), (12, 4, 19))
        self.assertIsNone(macos_cua._parse_driver_version("unknown"))
        self.assertEqual(macos_cua.MIN_CUA_DRIVER_VERSION, (0, 8, 3))

    def test_driver_output_paths_are_made_absolute_and_parent_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            current = os.getcwd()
            os.chdir(directory)
            try:
                output = macos_cua._absolute_output_path("proof/state.png")
            finally:
                os.chdir(current)

            self.assertEqual(
                Path(output).resolve(), Path(directory, "proof", "state.png").resolve()
            )
            self.assertTrue(Path(directory, "proof").is_dir())

    def test_point_click_uses_driver_count_field(self):
        calls = []
        original_call_driver = macos_cua.call_driver
        try:
            macos_cua.call_driver = lambda tool, params: (
                calls.append((tool, params)) or {"ok": True}
            )

            macos_cua.click_point(10, 20, 30, 40, click_count=2)

            click_calls = [c for c in calls if c[0] == "click"]
            self.assertEqual(len(click_calls), 1)
            self.assertEqual(click_calls[0][1]["count"], 2)
            self.assertNotIn("click_count", click_calls[0][1])
        finally:
            macos_cua.call_driver = original_call_driver

    def test_point_click_renders_and_acknowledges_operator_cursor_first(self):
        move = {"ok": True, "cursor_normalized": {"x": 0.25, "y": 0.5}}
        with (
            mock.patch.object(
                macos_cua, "_move_operator_cursor_to_point", return_value=move
            ) as render,
            mock.patch.object(
                macos_cua, "call_driver", return_value={"ok": True}
            ) as click,
        ):
            result = macos_cua.click_point(
                10, 20, 30, 40, app_name="Example App"
            )

        render.assert_called_once_with("Example App", 10, 20, 30.0, 40.0)
        self.assertEqual(
            [call for call in click.call_args_list if call.args[0] == "click"],
            [mock.call(
                "click",
                {
                    "pid": 10,
                    "window_id": 20,
                    "x": 30.0,
                    "y": 40.0,
                    "button": "left",
                    "count": 1,
                    "delivery_mode": "background",
                },
            )],
        )
        self.assertEqual(result["move"], move)

    def test_foreground_point_posts_verified_screen_point_to_target_pid(self):
        move = {
            "ok": True,
            "cursor_normalized": {"x": 0.25, "y": 0.5},
            "screen_point": {"x": 410.0, "y": 320.0},
        }
        native_text_pointer = SimpleNamespace(
            accessible_text_click=mock.Mock(return_value=None)
        )
        native_input = SimpleNamespace(
            post_mouse_click=mock.Mock(
                return_value={"ok": True, "path": "native_pid_mouse"}
            )
        )
        with (
            mock.patch.object(
                macos_cua,
                "bring_resolved_window_to_front",
                return_value={"ok": True},
            ),
            mock.patch.object(
                macos_cua, "_move_operator_cursor_to_point", return_value=move
            ),
            mock.patch.object(
                macos_cua,
                "_native_text_pointer",
                return_value=native_text_pointer,
            ),
            mock.patch.object(macos_cua, "_native_input", return_value=native_input),
            mock.patch.object(macos_cua, "call_driver") as driver,
        ):
            result = macos_cua.click_point(
                10,
                20,
                30,
                40,
                delivery_mode="foreground",
                app_name="Example App",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["path"], "verified-screen-point+native-pid-mouse"
        )
        native_text_pointer.accessible_text_click.assert_called_once()
        native_input.post_mouse_click.assert_called_once_with(
            10,
            {"x": 410.0, "y": 320.0},
            button="left",
            count=1,
        )
        driver.assert_not_called()

    def test_point_click_fails_closed_when_operator_cursor_is_not_visible(self):
        with (
            mock.patch.object(
                macos_cua,
                "_move_operator_cursor_to_point",
                return_value={"ok": False, "error": "cursor unavailable"},
            ),
            mock.patch.object(macos_cua, "call_driver") as click,
        ):
            result = macos_cua.click_point(
                10, 20, 30, 40, app_name="Example App"
            )

        self.assertFalse(result["ok"])
        self.assertIn("visible operator cursor", result["error"])
        click.assert_not_called()

    def test_point_target_rejects_stale_operator_screenshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            screenshot = root / "proof.png"
            screenshot.write_bytes(b"png")
            os.utime(screenshot, (10, 10))
            (root / "operator-state.json").write_text(
                json.dumps(
                    {
                        "app": "Fixture",
                        "pid": 10,
                        "window_id": 20,
                        "snapshot_id": "old-1",
                        "raw_screenshot_path": str(screenshot),
                    }
                )
            )
            with (
                mock.patch.object(macos_cua, "CACHE_DIR", str(root)),
                mock.patch.object(macos_cua.time, "time", return_value=100),
                mock.patch.object(macos_cua, "operator_update") as publish,
            ):
                result = macos_cua._move_operator_cursor_to_point(
                    "Fixture", 10, 20, 30, 40
                )

        self.assertFalse(result["ok"])
        self.assertIn("stale", result["error"])
        publish.assert_not_called()

    def test_point_click_keeps_raw_png_pixels_and_forwards_foreground_debug(self):
        calls = []
        original_call_driver = macos_cua.call_driver
        try:
            macos_cua.call_driver = lambda tool, params: (
                calls.append((tool, params)) or {"ok": True}
            )

            with mock.patch.object(
                macos_cua,
                "bring_resolved_window_to_front",
                return_value={"ok": True},
            ) as foreground:
                macos_cua.click_point(
                    10,
                    20,
                    70,
                    14,
                    delivery_mode="foreground",
                    debug_image_out="/tmp/click.png",
                )

            foreground.assert_called_once_with(10, 20)
            click_calls = [c for c in calls if c[0] == "click"]
            self.assertEqual(len(click_calls), 1)
            self.assertEqual(click_calls[0][1]["x"], 70.0)
            self.assertEqual(click_calls[0][1]["y"], 14.0)
            self.assertEqual(click_calls[0][1]["delivery_mode"], "foreground")
            self.assertEqual(click_calls[0][1]["debug_image_out"], "/tmp/click.png")
        finally:
            macos_cua.call_driver = original_call_driver

    def test_double_click_reuses_window_local_click_mapping(self):
        with mock.patch.object(
            macos_cua,
            "click_point",
            return_value={"effect": "unverifiable"},
        ) as click:
            result = macos_cua.double_click(
                10,
                20,
                x=30,
                y=40,
                delivery_mode="background",
            )

        self.assertEqual(result["effect"], "unverifiable")
        click.assert_called_once_with(
            10,
            20,
            30,
            40,
            click_count=2,
            delivery_mode="background",
        )

    def test_double_click_uses_center_of_visible_element_intersection(self):
        state = {
            "elements": [
                {
                    "element_index": 1,
                    "role": "AXWindow",
                    "frame": {"x": 100, "y": 200, "w": 400, "h": 300},
                },
                {
                    "element_index": 2,
                    "role": "AXTextArea",
                    "frame": {"x": 120, "y": 220, "w": 200, "h": 1000},
                },
            ]
        }
        with mock.patch.object(
            macos_cua, "click_point", return_value={"ok": True}
        ) as click:
            result = macos_cua.double_click(
                10,
                20,
                element_index=2,
                delivery_mode="foreground",
                snapshot_data=state,
            )

        self.assertEqual(result, {"ok": True})
        click.assert_called_once_with(
            10,
            20,
            120.0,
            160.0,
            click_count=2,
            delivery_mode="foreground",
            verified_screen_point={"x": 220.0, "y": 360.0},
        )

    def test_double_click_uses_two_native_presses_for_accessible_button(self):
        state = {
            "elements": [
                {
                    "element_index": 1,
                    "role": "AXWindow",
                    "frame": {"x": 100, "y": 200, "w": 400, "h": 300},
                },
                {
                    "element_index": 2,
                    "role": "AXButton",
                    "frame": {"x": 120, "y": 220, "w": 40, "h": 40},
                },
            ]
        }
        with (
            mock.patch.object(
                macos_cua, "click", side_effect=[{"ok": True}, {"ok": True}]
            ) as click,
            mock.patch.object(macos_cua.time, "sleep") as sleep,
        ):
            result = macos_cua.double_click(
                10,
                20,
                element_index=2,
                snapshot_data=state,
                app_name="Calculator",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], "native-ax-double-press")
        self.assertEqual(click.call_count, 2)
        click.assert_has_calls(
            [
                mock.call(10, 20, 2, app_name="Calculator"),
                mock.call(10, 20, 2, app_name=None),
            ]
        )
        self.assertEqual(sleep.call_count, 2)

    def test_secondary_action_uses_click_action_field(self):
        calls = []
        original_call_driver = macos_cua.call_driver
        try:
            macos_cua.call_driver = lambda tool, params: (
                calls.append((tool, params)) or {"ok": True}
            )

            macos_cua.perform_action(10, 20, 30, "open")

            self.assertEqual(calls[0][0], "click")
            self.assertEqual(calls[0][1]["action"], "open")
            self.assertEqual(calls[0][1]["element_index"], 30)
        finally:
            macos_cua.call_driver = original_call_driver

    def test_secondary_action_normalizes_advertised_showmenu(self):
        calls = []
        original_call_driver = macos_cua.call_driver
        try:
            macos_cua.call_driver = lambda tool, params: (
                calls.append((tool, params)) or {"ok": True}
            )

            macos_cua.perform_action(10, 20, 30, "Show Menu")

            self.assertEqual(calls[0][1]["action"], "show_menu")
        finally:
            macos_cua.call_driver = original_call_driver

    def test_secondary_action_accepts_native_ax_action_name(self):
        services = SimpleNamespace(
            AXUIElementPerformAction=lambda element, action: 0,
        )
        state = {
            "elements": [{"element_index": 30, "actions": ["AXShowMenu"]}]
        }
        with mock.patch.object(
            macos_cua,
            "_resolve_native_ax_element",
            return_value=((object(), services), None),
        ):
            result = macos_cua.perform_action(
                10, 20, 30, "showmenu", snapshot_data=state
            )

        self.assertEqual(result["path"], "native_ax")

    def test_perform_action_fails_closed_when_ax_hangs(self):
        services = SimpleNamespace(
            AXUIElementSetMessagingTimeout=mock.Mock(),
            AXUIElementPerformAction=lambda element, action: time.sleep(5) or 0,
        )
        state = {"elements": [{"element_index": 30, "actions": ["AXPress"]}]}
        started = time.monotonic()
        with mock.patch.object(
            macos_cua,
            "_resolve_native_ax_element",
            return_value=((object(), services), None),
        ):
            result = macos_cua.perform_action(
                10, 20, 30, "press", snapshot_data=state
            )
        elapsed = time.monotonic() - started
        self.assertEqual(result.get("error_code"), "ax_timeout")
        self.assertFalse(result.get("ok"))
        self.assertLess(elapsed, 2.5)

    def test_selection_range_supports_context_and_cursor_modes(self):
        value = "one target two target three"
        self.assertEqual(
            macos_cua._selection_range(value, "target", prefix="one ", suffix=" two"),
            (4, 6),
        )
        self.assertEqual(
            macos_cua._selection_range(
                value,
                "target",
                prefix="one ",
                suffix=" two",
                selection_type="cursor_after",
            ),
            (10, 0),
        )

    def test_drag_forwards_foreground_delivery(self):
        calls = []
        original_call_driver = macos_cua.call_driver
        try:
            macos_cua.call_driver = lambda tool, params: (
                calls.append((tool, params)) or {"ok": True}
            )

            moves = [
                {
                    "ok": True,
                    "cursor_normalized": {"x": 0.1, "y": 0.2},
                    "publish": {"state": {"cursor_update_id": "source"}},
                    "sync": {"ok": True},
                },
                {
                    "ok": True,
                    "cursor_normalized": {"x": 0.3, "y": 0.4},
                    "publish": {"state": {"cursor_update_id": "destination"}},
                    "sync": None,
                },
            ]
            with (
                mock.patch.object(
                    macos_cua,
                    "bring_resolved_window_to_front",
                    return_value={"ok": True},
                ) as foreground,
                mock.patch.object(
                    macos_cua,
                    "app_state",
                    return_value={
                        "ok": True,
                        "screenshot": {"width": 100, "height": 100},
                        "capture_geometry": {
                            "expected": {"x": 0, "y": 0, "width": 100, "height": 100}
                        },
                    },
                ),
                mock.patch.object(
                    macos_cua, "_move_operator_cursor_to_point", side_effect=moves
                ) as move,
                mock.patch.object(
                    macos_cua,
                    "_wait_for_operator_cursor",
                    return_value={"ok": True},
                ),
            ):
                result = macos_cua.drag(
                    10,
                    20,
                    1,
                    2,
                    3,
                    4,
                    delivery_mode="foreground",
                    duration_ms=900,
                    steps=36,
                )

            foreground.assert_called_once_with(10, 20)
            self.assertEqual(calls[0][0], "drag")
            self.assertEqual(calls[0][1]["delivery_mode"], "foreground")
            self.assertEqual(calls[0][1]["duration_ms"], 900)
            self.assertEqual(calls[0][1]["steps"], 36)
            self.assertEqual(result["effect"], "unverified")
            self.assertTrue(result["system_cursor_used"])
            self.assertTrue(result["move"]["destination"]["sync"]["ok"])
            self.assertEqual(move.call_count, 2)
        finally:
            macos_cua.call_driver = original_call_driver


class PlanEfficiencyTests(unittest.TestCase):
    @staticmethod
    def _snapshot(label, text):
        return {
            "tree_markdown": f'AXWindow "Fixture"\nAXButton "{label}"\n{text}',
            "elements": [
                {
                    "element_index": 1,
                    "role": "AXButton",
                    "label": label,
                    "actions": ["press"],
                    "frame": {"x": 0, "y": 0, "w": 40, "h": 20},
                }
            ],
            "element_count": 1,
        }

    def test_mutating_plan_requires_assertion_before_dispatch(self):
        with mock.patch.object(macos_cua, "operator_update") as operator:
            result = macos_cua.run_actions(
                10,
                20,
                {"actions": [{"action": "click", "label": "Go"}]},
                app_name="Fixture",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "assertion_required")
        self.assertEqual(result["unasserted_steps"], [1])
        operator.assert_not_called()

    def test_selected_element_is_reused_for_immediate_type(self):
        state = {
            "tree_markdown": "[3] AXTextArea Fixture",
            "elements": [{"element_index": 3, "role": "AXTextArea"}],
        }
        with (
            mock.patch.object(macos_cua, "_plan_snapshot", return_value=state),
            mock.patch.object(
                macos_cua, "select_text_action", return_value={"ok": True}
            ),
            mock.patch.object(
                macos_cua, "type_text", return_value={"ok": True}
            ) as type_text,
            mock.patch.object(macos_cua, "operator_update"),
        ):
            result = macos_cua.run_actions(
                10,
                20,
                {
                    "allow_unverified": True,
                    "capture": "never",
                    "settle_ms": 0,
                    "actions": [
                        {"action": "select_text", "element": 3, "text": "BETA"},
                        {"action": "type", "text": "GAMMA"},
                    ],
                },
                app_name="Fixture",
            )

        self.assertTrue(result["accepted"])
        self.assertEqual(type_text.call_args.args[2], 3)

    def test_new_document_shortcut_rebinds_to_new_ax_window(self):
        state = {"tree_markdown": "[1] AXWindow Untitled", "elements": []}
        with (
            mock.patch.object(macos_cua, "press_key", return_value={"ok": True}),
            mock.patch.object(macos_cua, "_new_ax_window_id", return_value=99),
            mock.patch.object(macos_cua, "_write_cache") as cache,
            mock.patch.object(macos_cua, "_plan_snapshot", return_value=state),
            mock.patch.object(macos_cua, "operator_update"),
        ):
            result = macos_cua.run_actions(
                10,
                20,
                {
                    "allow_unverified": True,
                    "capture": "never",
                    "settle_ms": 0,
                    "actions": [{"action": "key", "keys": "cmd+n"}],
                },
                app_name="TextEdit",
            )

        self.assertEqual(result["window_id"], 99)
        cache.assert_called_once_with("TextEdit", 10, 99)

    def test_malformed_plan_fails_before_operator_or_snapshot(self):
        with (
            mock.patch.object(macos_cua, "operator_update") as operator,
            mock.patch.object(macos_cua, "snapshot") as snapshot,
        ):
            result = macos_cua.run_actions(
                10,
                20,
                {"actions": [{"action": "key"}]},
                app_name="Fixture",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "invalid_plan")
        self.assertEqual(result["errors"][0]["code"], "required_field_missing")
        operator.assert_not_called()
        snapshot.assert_not_called()

    def test_pointer_coordinate_plan_observes_immediately_before_dispatch(self):
        state = self._snapshot("Go", "Ready")
        with (
            mock.patch.object(macos_cua, "snapshot", return_value=state),
            mock.patch.object(
                macos_cua, "app_state", return_value={"ok": True}
            ) as app_state,
            mock.patch.object(
                macos_cua, "click_point", return_value={"ok": True}
            ) as click_point,
            mock.patch.object(macos_cua, "operator_update", return_value={"ok": True}),
        ):
            result = macos_cua.run_actions(
                10,
                20,
                {
                    "pointer": True,
                    "allow_unverified": True,
                    "capture": "never",
                    "settle_ms": 0,
                    "actions": [{"action": "click", "x": 30, "y": 40}],
                },
                app_name="Fixture",
                foreground_prepared=True,
            )

        self.assertTrue(result["accepted"])
        app_state.assert_called_once_with(
            "Fixture",
            10,
            20,
            max_elements=120,
            include_screenshot=True,
            foreground_prepared=True,
        )
        click_point.assert_called_once_with(
            10,
            20,
            30,
            40,
            button="left",
            click_count=1,
            delivery_mode="background",
            debug_image_out=None,
            preserve_pointer=False,
            app_name="Fixture",
        )

    def test_failed_plan_stays_compact_unless_full_output_is_requested(self):
        state = self._snapshot("Go", "Ready")
        with (
            mock.patch.object(macos_cua, "snapshot", return_value=state),
            mock.patch.object(
                macos_cua, "press_key", return_value={"ok": False, "error": "missed"}
            ),
            mock.patch.object(macos_cua, "operator_update", return_value={"ok": True}),
        ):
            result = macos_cua.run_actions(
                10,
                20,
                {
                    "capture": "never",
                    "actions": [{"action": "key", "keys": "return"}],
                    "expect": {"text": "Ready"},
                },
                app_name="Fixture",
            )

        self.assertFalse(result["ok"])
        self.assertNotIn("elements", result["final"])
        self.assertEqual(result["steps"][0]["error"], "missed")

    def test_empty_expectation_does_not_exempt_mutating_plan(self):
        for empty in ({}, [], ""):
            with self.subTest(empty=empty), mock.patch.object(
                macos_cua, "operator_update"
            ) as operator:
                result = macos_cua.run_actions(
                    10,
                    20,
                    {
                        "actions": [{"action": "click", "label": "Go"}],
                        "expect": empty,
                    },
                    app_name="Fixture",
                )
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "assertion_required")
            operator.assert_not_called()

    def test_allow_unverified_dispatches_but_never_reports_success(self):
        state = self._snapshot("Go", "Ready")
        with (
            mock.patch.object(
                macos_cua, "_plan_snapshot", return_value=state
            ) as plan_snapshot,
            mock.patch.object(macos_cua, "app_state") as app_state,
            mock.patch.object(
                macos_cua,
                "click_label_pointer",
                return_value={"ok": True, "method": "native_ax"},
            ) as click,
            mock.patch.object(
                macos_cua, "_cleanup_driver_cursors", return_value={"ended": []}
            ),
            mock.patch.object(macos_cua, "operator_update", return_value={"ok": True}),
        ):
            result = macos_cua.run_actions(
                10,
                20,
                {
                    "pointer": True,
                    "allow_unverified": True,
                    "settle_ms": 0,
                    "actions": [{"action": "click", "label": "Go"}],
                },
                app_name="Fixture",
            )

        click.assert_called_once()
        self.assertFalse(result["ok"])
        self.assertTrue(result["accepted"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["code"], "unverified_run")
        self.assertFalse(result["metrics"]["capture_attempted"])
        self.assertFalse(result["metrics"]["final_snapshot_skipped"])
        plan_snapshot.assert_called_once_with(10, 20, max_elements=120)
        app_state.assert_not_called()

    def test_key_only_unverified_plan_skips_useless_final_snapshot(self):
        with (
            mock.patch.object(macos_cua, "_plan_snapshot") as plan_snapshot,
            mock.patch.object(macos_cua, "app_state") as app_state,
            mock.patch.object(
                macos_cua,
                "press_key",
                return_value={"accepted": True, "effect": "unverifiable"},
            ),
            mock.patch.object(macos_cua, "operator_update", return_value={"ok": True}),
        ):
            result = macos_cua.run_actions(
                10,
                20,
                {
                    "allow_unverified": True,
                    "actions": [{"action": "key", "keys": "space"}],
                },
                app_name="Fixture",
            )

        self.assertTrue(result["accepted"])
        self.assertFalse(result["metrics"]["capture_attempted"])
        self.assertTrue(result["metrics"]["final_snapshot_skipped"])
        self.assertEqual(result["final"]["element_count"], 0)
        plan_snapshot.assert_not_called()
        app_state.assert_not_called()

    def test_rejected_unverified_dispatch_still_captures_failure(self):
        state = self._snapshot("Ready", "Ready")
        captured_state = {
            "ok": True,
            "screenshot": {"path": "/tmp/failure.png", "raw_path": "/tmp/failure.png"},
            "capture_geometry": {"verified": True},
            "capture_recovery": None,
        }
        with (
            mock.patch.object(macos_cua, "_plan_snapshot", return_value=state),
            mock.patch.object(
                macos_cua, "press_key", return_value={"ok": False, "error": "missed"}
            ),
            mock.patch.object(
                macos_cua, "app_state", return_value=captured_state
            ) as app_state,
            mock.patch.object(macos_cua, "operator_update", return_value={"ok": True}),
        ):
            result = macos_cua.run_actions(
                10,
                20,
                {
                    "allow_unverified": True,
                    "actions": [{"action": "key", "keys": "space"}],
                },
                app_name="Fixture",
            )

        self.assertFalse(result["accepted"])
        self.assertTrue(result["metrics"]["capture_attempted"])
        self.assertFalse(result["metrics"]["final_snapshot_skipped"])
        app_state.assert_called_once()

    def test_key_dispatches_with_unverifiable_effect_pass_when_assertions_observe_outcome(self):
        save_sheet = self._snapshot("Cancel", "Save As")
        page = self._snapshot("Example Domain", "Example Domain")
        unverified_dispatch = {
            "ok": False,
            "accepted": True,
            "verified": False,
            "effect": "unverifiable",
        }
        with (
            mock.patch.object(
                macos_cua, "snapshot", side_effect=[save_sheet, page]
            ) as snapshot,
            mock.patch.object(
                macos_cua,
                "press_key",
                side_effect=[
                    {**unverified_dispatch, "path": "pid_targeted"},
                    {**unverified_dispatch, "path": "system_events_pid"},
                ],
            ) as press_key,
            mock.patch.object(macos_cua, "operator_update", return_value={"ok": True}),
        ):
            result = macos_cua.run_actions(
                32734,
                125239,
                {
                    "capture": "never",
                    "output": "full",
                    "settle_ms": 0,
                    "actions": [
                        {
                            "action": "key",
                            "keys": "cmd+s",
                            "expect": [{"text": "Save As"}, {"text": "Cancel"}],
                        },
                        {
                            "action": "key",
                            "keys": "escape",
                            "delivery_mode": "system_events",
                            "expect": [
                                {"text": "Example Domain"},
                                {"not_text": "Save As"},
                            ],
                        },
                    ],
                    "expect": [
                        {"text": "Example Domain"},
                        {"not_text": "Save As"},
                    ],
                },
                app_name="Google Chrome",
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["accepted"])
        self.assertTrue(result["verified"])
        self.assertTrue(all(step["accepted"] for step in result["steps"]))
        self.assertTrue(all(step["verification"]["ok"] for step in result["steps"]))
        self.assertTrue(all(step["result"]["ok"] is False for step in result["steps"]))
        self.assertEqual(snapshot.call_count, 2)
        self.assertEqual(
            press_key.call_args_list,
            [
                mock.call(32734, 125239, "cmd+s", "background"),
                mock.call(32734, 125239, "escape", "system_events"),
            ],
        )

    def test_capture_always_serializes_verified_geometry_receipt(self):
        state = self._snapshot("Ready", "Ready")
        geometry = {
            "verified": True,
            "identity": {"status": "resolved", "method": "exact-quartz-window-id"},
            "driver": {"max_image_dimension": 1568, "config_error": None},
        }
        captured_state = {
            "ok": True,
            "screenshot": {"path": "/tmp/fresh.png", "raw_path": "/tmp/fresh.png"},
            "capture_geometry": geometry,
            "capture_recovery": None,
        }
        with (
            mock.patch.object(macos_cua, "snapshot", return_value=state) as snapshot,
            mock.patch.object(
                macos_cua,
                "app_state",
                return_value=captured_state,
            ) as app_state,
            mock.patch.object(
                macos_cua,
                "_driver_max_image_dimension",
            ) as driver_config,
            mock.patch.object(macos_cua, "operator_update", return_value={"ok": True}),
        ):
            result = macos_cua.run_actions(
                10,
                20,
                {
                    "capture": "always",
                    "actions": [],
                    "expect": {"text": "Ready"},
                },
                app_name="Fixture",
            )

        serialized = json.loads(json.dumps(result))
        self.assertTrue(serialized["ok"])
        self.assertTrue(serialized["accepted"])
        self.assertTrue(serialized["verified"])
        self.assertTrue(serialized["final"]["capture_geometry"]["verified"])
        self.assertEqual(
            serialized["final"]["capture_geometry"]["driver"][
                "max_image_dimension"
            ],
            1568,
        )
        self.assertIsNone(serialized["final"]["capture_recovery"])
        snapshot.assert_called_once_with(10, 20, max_elements=120)
        app_state.assert_called_once_with(
            "Fixture",
            10,
            20,
            max_elements=120,
            include_screenshot=True,
            foreground_prepared=False,
        )
        driver_config.assert_not_called()

    def test_capture_failure_retains_geometry_diagnostic(self):
        state = self._snapshot("Ready", "Ready")
        geometry = {
            "verified": False,
            "identity": {"status": "resolved", "method": "exact-quartz-window-id"},
            "driver": {"max_image_dimension": 1568, "config_error": None},
            "scale": {"x": 0.5, "y": 0.4},
        }
        recovery = {
            "attempted": True,
            "reason": "capture_geometry_mismatch",
            "captured": True,
        }
        captured_state = {
            "ok": False,
            "error": "capture_geometry_mismatch",
            "screenshot": {"path": "/tmp/bad.png", "raw_path": "/tmp/bad.png"},
            "capture_geometry": geometry,
            "capture_recovery": recovery,
        }
        with (
            mock.patch.object(macos_cua, "snapshot", return_value=state),
            mock.patch.object(macos_cua, "app_state", return_value=captured_state),
            mock.patch.object(macos_cua, "operator_update", return_value={"ok": True}),
        ):
            result = macos_cua.run_actions(
                10,
                20,
                {
                    "capture": "always",
                    "actions": [],
                    "expect": {"text": "Ready"},
                },
                app_name="Fixture",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["final"]["capture_error"], "capture_geometry_mismatch")
        self.assertEqual(result["final"]["capture_geometry"], geometry)
        self.assertEqual(result["final"]["capture_recovery"], recovery)

    def test_failed_step_assertion_does_not_reclassify_accepted_dispatch(self):
        page = self._snapshot("Example Domain", "Example Domain")
        captured_state = {
            "ok": True,
            "screenshot": {"path": "/tmp/assertion-failure.png"},
            "capture_geometry": {"verified": True},
            "capture_recovery": None,
        }
        with (
            mock.patch.object(
                macos_cua,
                "press_key",
                return_value={
                    "ok": False,
                    "accepted": True,
                    "verified": False,
                    "effect": "unverifiable",
                },
            ),
            mock.patch.object(
                macos_cua,
                "wait_for_expectations",
                return_value=(False, [{"ok": False}], page),
            ),
            mock.patch.object(
                macos_cua, "app_state", return_value=captured_state
            ) as app_state,
            mock.patch.object(macos_cua, "operator_update", return_value={"ok": True}),
        ):
            result = macos_cua.run_actions(
                32734,
                125239,
                {
                    "allow_unverified": True,
                    "settle_ms": 0,
                    "actions": [
                        {"action": "key", "keys": "cmd+f"},
                        {"action": "key", "keys": "cmd+s", "expect": {"text": "Save As"}}
                    ],
                    "expect": {"text": "Example Domain"},
                },
                app_name="Google Chrome",
            )

        self.assertTrue(result["accepted"])
        self.assertFalse(result["verified"])
        self.assertFalse(result["ok"])
        self.assertTrue(all(step["accepted"] for step in result["steps"]))
        self.assertTrue(result["metrics"]["capture_attempted"])
        app_state.assert_called_once()

    def test_rejected_key_dispatch_stays_unaccepted_when_final_assertion_passes(self):
        page = self._snapshot("Example Domain", "Example Domain")
        with (
            mock.patch.object(macos_cua, "snapshot", return_value=page),
            mock.patch.object(
                macos_cua,
                "press_key",
                return_value={
                    "ok": False,
                    "accepted": False,
                    "verified": False,
                    "effect": "unverifiable",
                },
            ),
            mock.patch.object(macos_cua, "operator_update", return_value={"ok": True}),
        ):
            result = macos_cua.run_actions(
                32734,
                125239,
                {
                    "capture": "never",
                    "settle_ms": 0,
                    "actions": [{"action": "key", "keys": "escape"}],
                    "expect": {"text": "Example Domain"},
                },
                app_name="Google Chrome",
            )

        self.assertFalse(result["accepted"])
        self.assertTrue(result["verified"])
        self.assertFalse(result["ok"])

    def test_asserted_plan_reuses_fresh_state_and_compacts_success(self):
        before = self._snapshot("Go", "Ready")
        done = self._snapshot("Next", "Done")
        finished = self._snapshot("Next", "Finished")
        with (
            mock.patch.object(
                macos_cua, "snapshot", side_effect=[before, done, finished]
            ) as snapshot,
            mock.patch.object(
                macos_cua,
                "click_label_pointer",
                side_effect=[
                    {"ok": True, "method": "ax", "move": {"sync": {"duration_ms": 4}}},
                    {"ok": True, "method": "ax", "move": {"sync": {"duration_ms": 3}}},
                ],
            ) as click,
            mock.patch.object(
                macos_cua, "_cleanup_driver_cursors", return_value={"ended": []}
            ) as cleanup,
            mock.patch.object(macos_cua, "operator_update", return_value={"ok": True}),
        ):
            result = macos_cua.run_actions(
                10,
                20,
                {
                    "pointer": True,
                    "settle_ms": 0,
                    "capture": "never",
                    "actions": [
                        {"action": "click", "label": "Go", "expect": {"text": "Done"}},
                        {"action": "click", "label": "Next", "expect": {"text": "Finished"}},
                    ],
                    "expect": {"text": "Finished"},
                },
                app_name="Fixture",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(snapshot.call_count, 3)
        self.assertEqual(result["metrics"]["state_reuses"], 1)
        self.assertNotIn("elements", result["final"])
        self.assertEqual(result["steps"][1]["state_reused"], True)
        self.assertEqual(result["steps"][0]["cursor_sync_ms"], 4)
        cleanup.assert_called_once_with()
        self.assertTrue(all(call.kwargs["prepare_cursor"] is False for call in click.call_args_list))

    def test_asserted_plan_reuses_seed_snapshot_without_initial_ax(self):
        before = self._snapshot("7", "Ready")
        done = self._snapshot("Equals", "64")
        done["elements"].append(
            {"element_index": 9, "role": "AXStaticText", "label": "", "value": "64"}
        )
        with (
            mock.patch.object(
                macos_cua,
                "_native_ax_snapshot",
                return_value={"error": "test", "elements": []},
            ),
            mock.patch.object(macos_cua, "snapshot", side_effect=[done]) as snapshot,
            mock.patch.object(
                macos_cua,
                "click_label_pointer",
                return_value={
                    "ok": True,
                    "method": "ax",
                    "move": {"sync": {"duration_ms": 2}},
                },
            ),
            mock.patch.object(
                macos_cua, "_cleanup_driver_cursors", return_value={"ended": []}
            ),
            mock.patch.object(macos_cua, "operator_update", return_value={"ok": True}),
        ):
            result = macos_cua.run_actions(
                10,
                20,
                {
                    "pointer": True,
                    "settle_ms": 0,
                    "capture": "never",
                    "seed_snapshot": before,
                    "actions": [
                        {"action": "click", "label": "7"},
                        {
                            "action": "click",
                            "label": "Equals",
                            "expect": {"text": "64", "role": "AXStaticText"},
                        },
                    ],
                    "expect": {"text": "64", "role": "AXStaticText"},
                },
                app_name="Fixture",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(snapshot.call_count, 1)
        self.assertGreaterEqual(result["metrics"]["state_reuses"], 1)
        self.assertEqual(result["steps"][0]["state_reused"], True)

    def test_clear_all_clear_alias_reuses_seed_without_extra_ax(self):
        before = self._snapshot("Clear", "0")
        after = self._snapshot("8", "8")
        after["elements"].append(
            {"element_index": 9, "role": "AXStaticText", "label": "", "value": "8"}
        )
        with (
            mock.patch.object(
                macos_cua,
                "_native_ax_snapshot",
                return_value={"error": "test", "elements": []},
            ),
            mock.patch.object(macos_cua, "snapshot", side_effect=[after]) as snapshot,
            mock.patch.object(
                macos_cua,
                "click_label_pointer",
                return_value={
                    "ok": True,
                    "method": "ax",
                    "move": {"sync": {"duration_ms": 2}},
                },
            ) as click,
            mock.patch.object(
                macos_cua, "_cleanup_driver_cursors", return_value={"ended": []}
            ),
            mock.patch.object(macos_cua, "operator_update", return_value={"ok": True}),
        ):
            result = macos_cua.run_actions(
                10,
                20,
                {
                    "pointer": True,
                    "settle_ms": 0,
                    "capture": "never",
                    "seed_snapshot": before,
                    "actions": [
                        {
                            "action": "click",
                            "label": "All Clear",
                            "expect": {"text": "8", "role": "AXStaticText"},
                        },
                    ],
                    "expect": {"text": "8", "role": "AXStaticText"},
                },
                app_name="Calculator",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["steps"][0]["state_reused"], True)
        self.assertEqual(click.call_args.args[2], "All Clear")
        self.assertEqual(snapshot.call_count, 1)

    def test_asserted_plan_reuses_state_between_clicks_without_expect(self):
        before = self._snapshot("7", "Ready")
        before["elements"].append(
            {"element_index": 3, "role": "AXButton", "label": "Add", "value": ""}
        )
        done = self._snapshot("Add", "15")
        with (
            mock.patch.object(
                macos_cua,
                "_native_ax_snapshot",
                return_value={"error": "test", "elements": []},
            ),
            mock.patch.object(
                macos_cua, "snapshot", side_effect=[before, done]
            ) as snapshot,
            mock.patch.object(
                macos_cua,
                "click_label_pointer",
                side_effect=[
                    {"ok": True, "method": "ax", "move": {"sync": {"duration_ms": 2}}},
                    {"ok": True, "method": "ax", "move": {"sync": {"duration_ms": 2}}},
                ],
            ),
            mock.patch.object(
                macos_cua, "_cleanup_driver_cursors", return_value={"ended": []}
            ),
            mock.patch.object(macos_cua, "operator_update", return_value={"ok": True}),
        ):
            result = macos_cua.run_actions(
                10,
                20,
                {
                    "pointer": True,
                    "settle_ms": 0,
                    "capture": "never",
                    "actions": [
                        {"action": "click", "label": "7"},
                        {"action": "click", "label": "Add", "expect": {"text": "15"}},
                    ],
                    "expect": {"text": "15"},
                },
                app_name="Fixture",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(snapshot.call_count, 2)
        self.assertEqual(result["metrics"]["state_reuses"], 1)
        self.assertEqual(result["steps"][1]["state_reused"], True)

    def test_pointer_label_click_prefers_native_ax_without_driver_snapshot(self):
        native = self._snapshot("Go", "Ready")
        native["source"] = "native_ax"
        native["elements"].insert(
            0,
            {
                "element_index": 0,
                "role": "AXWindow",
                "label": "Fixture",
                "frame": {"x": 0, "y": 0, "w": 200, "h": 200},
            },
        )
        native["elements"][1]["_native_element"] = object()
        native["elements"][1]["_native_services"] = object()
        with (
            mock.patch.object(macos_cua, "_native_ax_snapshot", return_value=native),
            mock.patch.object(macos_cua, "snapshot") as driver_snapshot,
            mock.patch.object(macos_cua, "_native_ax_press", return_value={"ok": True}),
            mock.patch.object(macos_cua, "operator_update", return_value={"ok": True}),
            mock.patch.object(
                macos_cua,
                "_wait_for_operator_cursor",
                return_value={"ok": True, "duration_ms": 1},
            ),
            mock.patch.object(
                macos_cua, "_cleanup_driver_cursors", return_value={"ended": []}
            ),
        ):
            result = macos_cua.click_label_pointer(
                10, 20, "Go", app_name="Fixture"
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["method"], "agent-cursor-glide+native-axpress-fallback")
        driver_snapshot.assert_not_called()

    def test_scroll_forwards_foreground_delivery(self):
        calls = []
        original_call_driver = macos_cua.call_driver
        try:
            macos_cua.call_driver = lambda tool, params: (
                calls.append((tool, params)) or {"ok": True}
            )

            macos_cua.scroll(
                10,
                20,
                "down",
                1,
                by="page",
                element_index=30,
                delivery_mode="foreground",
            )

            self.assertEqual(calls[0][0], "scroll")
            self.assertEqual(calls[0][1]["delivery_mode"], "foreground")
            self.assertEqual(calls[0][1]["element_index"], 30)
        finally:
            macos_cua.call_driver = original_call_driver

    def test_page_scroll_prefers_exact_window_native_ax(self):
        native = {"elements": [{"element_index": 30, "role": "AXOutline"}]}
        native_input = SimpleNamespace(
            scroll_page=mock.Mock(return_value={"ok": True, "path": "native_ax_scroll"})
        )
        with (
            mock.patch.object(macos_cua, "_native_ax_snapshot", return_value=native) as snapshot,
            mock.patch.object(macos_cua, "_native_input", return_value=native_input),
            mock.patch.object(macos_cua, "call_driver") as driver,
        ):
            result = macos_cua.scroll(
                10, 20, "down", 1, by="page", element_index=30
            )

        self.assertEqual(result["path"], "native_ax_scroll")
        snapshot.assert_called_once_with(10, max_elements=120, window_id=20)
        native_input.scroll_page.assert_called_once_with(native, 30, "down")
        driver.assert_not_called()

    def test_page_scroll_recovers_capture_failure_with_pid_key(self):
        native_input = SimpleNamespace(
            scroll_page=mock.Mock(return_value={"error": "unavailable"}),
            page_key_scroll=mock.Mock(
                return_value={"ok": True, "path": "native_pid_page_key"}
            ),
        )
        with (
            mock.patch.object(macos_cua, "_native_ax_snapshot", return_value={}),
            mock.patch.object(macos_cua, "_native_input", return_value=native_input),
            mock.patch.object(
                macos_cua,
                "call_driver",
                return_value={"code": "px_capture_unavailable"},
            ),
        ):
            result = macos_cua.scroll(
                10, 20, "down", 2, by="page", x=30, y=40
            )

        self.assertEqual(result["path"], "native_pid_page_key")
        native_input.page_key_scroll.assert_called_once_with(
            macos_cua._post_key_event, 10, "down", 2
        )

    def test_page_scroll_rejects_recovered_global_input(self):
        native_input = SimpleNamespace(
            page_key_scroll=mock.Mock(
                return_value={
                    "ok": True,
                    "accepted": True,
                    "path": "native_pid_page_key",
                }
            ),
        )
        with (
            mock.patch.object(macos_cua, "_native_input", return_value=native_input),
            mock.patch.object(
                macos_cua,
                "call_driver",
                return_value={
                    "effect": "unverifiable",
                    "route": "global_input",
                    "session_recovered": True,
                },
            ),
        ):
            result = macos_cua.scroll(10, 20, "down", 1, by="page", x=30, y=40)

        self.assertEqual(result["path"], "native_pid_page_key")
        native_input.page_key_scroll.assert_called_once()

    def test_native_page_scroll_uses_scrollable_ancestor(self):
        target, parent = object(), object()
        services = SimpleNamespace(
            kAXParentAttribute="AXParent",
            AXUIElementCopyAttributeValue=mock.Mock(return_value=(0, parent)),
            AXUIElementCopyActionNames=mock.Mock(
                return_value=(0, ["AXScrollDownByPage"])
            ),
            AXUIElementPerformAction=mock.Mock(return_value=0),
        )
        snapshot = {
            "elements": [
                {
                    "element_index": 30,
                    "actions": [],
                    "_native_element": target,
                    "_native_services": services,
                }
            ]
        }

        result = macos_cua._native_input().scroll_page(snapshot, 30, "down")

        self.assertEqual(result["path"], "native_ax_scroll")
        services.AXUIElementPerformAction.assert_called_once_with(
            parent, "AXScrollDownByPage"
        )

    def test_focused_type_omits_element_index(self):
        calls = []
        original_call_driver = macos_cua.call_driver
        try:
            macos_cua.call_driver = lambda tool, params: (
                calls.append((tool, params)) or {"ok": True}
            )

            macos_cua.type_text(10, 20, None, "hello", delivery_mode="foreground")

            self.assertEqual(calls[0][0], "type_text")
            self.assertEqual(calls[0][1]["text"], "hello")
            self.assertNotIn("element_index", calls[0][1])
            self.assertEqual(calls[0][1]["delivery_mode"], "foreground")
        finally:
            macos_cua.call_driver = original_call_driver


class ListButtonsTests(unittest.TestCase):
    def test_vision_inventory_prints_labeled_framed_targets(self):
        snapshot = {
            "source": "driver_vision",
            "tree_markdown": "[7] VisionText Members",
            "elements": [
                {
                    "element_index": 7,
                    "role": "VisionText",
                    "label": "Members",
                    "value": "",
                    "frame": {"x": 10, "y": 20, "w": 80, "h": 30},
                },
                {
                    "element_index": 8,
                    "role": "VisionText",
                    "label": "No frame",
                },
            ],
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            ok = macos_cua.emit_list_buttons(snapshot)

        self.assertTrue(ok)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            [json.loads(line) for line in stdout.getvalue().splitlines()],
            [{"index": 7, "label": "Members", "value": ""}],
        )
    def test_native_snapshot_retries_after_activation_until_window_content_exists(self):
        menu_only = {
            "source": "native_ax",
            "tree_markdown": "[1] AXApplication App",
            "elements": [
                {"element_index": 1, "role": "AXApplication", "label": "App"}
            ],
        }
        content = {
            "source": "native_ax",
            "tree_markdown": "[1] AXWindow App\n[2] AXButton Upcoming",
            "elements": [
                {"element_index": 1, "role": "AXWindow", "label": "App"},
                {"element_index": 2, "role": "AXButton", "label": "Upcoming"},
            ],
        }
        with (
            mock.patch.object(
                macos_cua,
                "_activate_running_identity",
                return_value={"ok": True},
            ) as activate,
            mock.patch.object(
                macos_cua,
                "_native_ax_snapshot",
                side_effect=[menu_only, content],
            ) as native_state,
            mock.patch.object(
                macos_cua,
                "_reopen_running_identity",
                return_value={"ok": True, "method": "bundle-reopen"},
            ) as reopen,
            mock.patch.object(macos_cua.time, "sleep") as sleep,
        ):
            result = macos_cua._native_ax_snapshot_after_activation(42, 80)

        self.assertIsNone(macos_cua.snapshot_content_error(result))
        activate.assert_called_once_with({"pid": 42})
        reopen.assert_called_once_with(42)
        self.assertEqual(native_state.call_count, 2)
        sleep.assert_called_once_with(0.2)

    def test_snapshot_error_is_reported_as_failure(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            ok = macos_cua.emit_list_buttons({"error": "AX snapshot unavailable"})

        self.assertFalse(ok)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("AX snapshot unavailable", stderr.getvalue())

    def test_empty_ax_tree_is_reported_as_failure(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            ok = macos_cua.emit_list_buttons(
                {"elements": [], "tree_markdown": ""}
            )

        self.assertFalse(ok)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("empty AX tree", stderr.getvalue())

    def test_menu_only_snapshot_is_reported_as_failure_with_roles(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        snapshot = {
            "tree_markdown": "AXApplication > AXMenuBar",
            "elements": [
                {"element_index": 1, "role": "AXApplication", "label": "Example"},
                {"element_index": 2, "role": "AXMenuBar", "label": ""},
                {"element_index": 3, "role": "AXMenuBarItem", "label": "File"},
            ],
        }
        with redirect_stdout(stdout), redirect_stderr(stderr):
            ok = macos_cua.emit_list_buttons(snapshot)

        self.assertFalse(ok)
        self.assertEqual(stdout.getvalue(), "")
        error = json.loads(stderr.getvalue())
        self.assertEqual(error["error"], "snapshot has no target-window AX content")
        self.assertEqual(error["element_count"], 3)
        self.assertIn("AXMenuBarItem", error["roles"])

    def test_valid_snapshot_prints_only_labeled_interactive_controls(self):
        snapshot = {
            "elements": [
                {
                    "element_index": 4,
                    "role": "AXButton",
                    "label": "Upcoming",
                    "value": "Selected",
                },
                {
                    "element_index": 5,
                    "role": "AXStaticText",
                    "label": "Community Meetup",
                },
                {"element_index": 6, "role": "AXButton", "label": ""},
            ]
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            ok = macos_cua.emit_list_buttons(snapshot)

        self.assertTrue(ok)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(
            [json.loads(line) for line in stdout.getvalue().splitlines()],
            [{"index": 4, "label": "Upcoming", "value": "Selected"}],
        )


class _FakeDriverSocket:
    def __init__(self, chunks=(), send_error=None, recv_error=None):
        self._chunks = list(chunks)
        self.send_error = send_error
        self.recv_error = recv_error
        self.sent = []
        self.closed = False
        self.timeout = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, data):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(data)

    def recv(self, _n):
        if self.recv_error is not None:
            raise self.recv_error
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self):
        self.closed = True


class DriverSocketTransportTests(unittest.TestCase):
    def setUp(self):
        macos_cua.reset_driver_socket()
        macos_cua.reset_driver_call_stats()
        macos_cua.telemetry_reset()

    def tearDown(self):
        macos_cua.reset_driver_socket()

    def test_socket_success_returns_structured_content_only(self):
        envelope = {
            "ok": True,
            "result": {
                "content": [{"text": "x", "type": "text"}],
                "structuredContent": {"x": 1731, "y": 1398},
            },
        }
        fake = _FakeDriverSocket(chunks=[json.dumps(envelope).encode() + b"\n"])
        with mock.patch.object(macos_cua, "_connect_driver_socket", return_value=fake):
            with mock.patch.object(macos_cua.subprocess, "run") as run:
                result = macos_cua.call_driver("get_cursor_position", {})
        self.assertEqual(result, {"x": 1731, "y": 1398})
        run.assert_not_called()

    def test_socket_success_without_structured_content_returns_error(self):
        envelope = {
            "ok": True,
            "result": {"content": [{"text": "x", "type": "text"}]},
        }
        fake = _FakeDriverSocket(chunks=[json.dumps(envelope).encode() + b"\n"])
        with mock.patch.object(macos_cua, "_connect_driver_socket", return_value=fake):
            with mock.patch.object(macos_cua.subprocess, "run") as run:
                result = macos_cua.call_driver("get_cursor_position", {})
        self.assertEqual(
            result["error"],
            "cua-driver socket response had no structuredContent",
        )
        self.assertIn("raw", result)
        run.assert_not_called()

    def test_socket_error_envelope_matches_error_convention(self):
        fake = _FakeDriverSocket(
            chunks=[
                json.dumps({"ok": False, "error": "boom", "exit_code": 1}).encode()
                + b"\n"
            ]
        )
        with mock.patch.object(macos_cua, "_connect_driver_socket", return_value=fake):
            with mock.patch.object(macos_cua.subprocess, "run") as run:
                result = macos_cua.call_driver("press_key", {"key": "a"})
        self.assertEqual(result, {"error": "boom"})
        run.assert_not_called()

    def test_session_ended_revives_named_session_once(self):
        ended = {
            "refusal": {
                "code": "session_ended",
                "message": "this session has ended; call start_session explicitly to reuse its label",
            },
            "status": "refused",
        }
        fake = _FakeDriverSocket(
            chunks=[
                _envelope_line(ended),
                _envelope_line({"ok": True}),
                _envelope_line({"ok": True, "accepted": True}),
            ]
        )
        with mock.patch.object(macos_cua, "_connect_driver_socket", return_value=fake):
            with mock.patch.object(macos_cua.subprocess, "run") as run:
                result = macos_cua.call_driver(
                    "press_key", {"pid": 10, "window_id": 20, "key": "escape"}
                )
        self.assertEqual(result.get("ok"), True)
        self.assertTrue(result.get("session_recovered"))
        payloads = [json.loads(item) for item in fake.sent]
        self.assertEqual(
            [item["name"] for item in payloads],
            ["press_key", "start_session", "press_key"],
        )
        self.assertEqual(payloads[2]["args"].get("session"), macos_cua.CUA_SESSION)
        run.assert_not_called()

    def test_reconnect_waits_for_restarted_daemon_socket(self):
        fresh = _FakeDriverSocket(chunks=[_envelope_line({"ok": True})])
        with _allow_daemon_restart():
            with mock.patch.object(
                macos_cua,
                "_connect_driver_socket",
                side_effect=[
                    FileNotFoundError("missing"),
                    FileNotFoundError("missing"),
                    ConnectionRefusedError("daemon still starting"),
                    fresh,
                ],
            ) as connect:
                with mock.patch.object(
                    macos_cua.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
                ) as run:
                    result = macos_cua.call_driver("get_window_state", {"pid": 1})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(connect.call_count, 4)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][:3], ["launchctl", "kickstart", "-k"])
        _assert_no_tool_call_spawn(run)

    def test_exhausted_recovery_returns_error_without_tool_call(self):
        with _allow_daemon_restart():
            with mock.patch.object(
                macos_cua,
                "_connect_driver_socket",
                side_effect=FileNotFoundError("missing"),
            ):
                with mock.patch.object(
                    macos_cua.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
                ) as run:
                    result = macos_cua.call_driver("get_window_state")
        self.assertIn("error", result)
        self.assertIn("missing", result["error"])
        self.assertEqual(run.call_count, 1)
        _assert_no_tool_call_spawn(run)

    def test_socket_timeout_message(self):
        fake = _FakeDriverSocket(recv_error=TimeoutError("timed out"))
        with mock.patch.object(macos_cua, "_connect_driver_socket", return_value=fake):
            with mock.patch.object(macos_cua.subprocess, "run") as run:
                result = macos_cua.call_driver(
                    "press_key", {"key": "a"}, timeout=5, _recover_timeout=False
                )
        self.assertEqual(
            result,
            {"error": "cua-driver socket call 'press_key' timed out after 5s"},
        )
        run.assert_not_called()

    def test_unparseable_socket_line_returns_error(self):
        fake = _FakeDriverSocket(chunks=[b"not-json\n"])
        with mock.patch.object(macos_cua, "_connect_driver_socket", return_value=fake):
            with mock.patch.object(macos_cua.subprocess, "run") as run:
                result = macos_cua.call_driver("get_window_state")
        self.assertEqual(result["error"], "cua-driver socket returned invalid JSON")
        run.assert_not_called()

    def test_truncated_socket_line_returns_error_without_tool_call(self):
        fake = _FakeDriverSocket(chunks=[b'{"ok":true,"result":{'])
        with _allow_daemon_restart():
            with mock.patch.object(
                macos_cua, "_connect_driver_socket", return_value=fake
            ):
                with mock.patch.object(
                    macos_cua.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
                ) as run:
                    result = macos_cua.call_driver("get_window_state")
        self.assertIn("error", result)
        _assert_no_tool_call_spawn(run)

    def test_socket_response_reassembled_from_multiple_recv_chunks(self):
        fake = _FakeDriverSocket(
            chunks=[
                b'{"ok":true,"result":{"content":[{"text":"x","type":"text"}],',
                b'"structuredContent":{"ok":true,"n":1}}}\n',
            ]
        )
        with mock.patch.object(macos_cua, "_connect_driver_socket", return_value=fake):
            with mock.patch.object(macos_cua.subprocess, "run") as run:
                result = macos_cua.call_driver("get_window_state")
        self.assertEqual(result, {"ok": True, "n": 1})
        run.assert_not_called()

    def test_stale_cached_socket_reconnects_once(self):
        stale = _FakeDriverSocket(send_error=OSError("broken pipe"))
        fresh = _FakeDriverSocket(chunks=[_envelope_line({"ok": True})])
        macos_cua._DRIVER_SOCKET_STATE["sock"] = stale
        with mock.patch.object(
            macos_cua, "_connect_driver_socket", return_value=fresh
        ) as connect:
            with mock.patch.object(macos_cua.subprocess, "run") as run:
                result = macos_cua.call_driver("press_key", {"key": "Return"})
        self.assertEqual(result, {"ok": True})
        self.assertTrue(stale.closed)
        connect.assert_called_once()
        run.assert_not_called()
        self.assertEqual(
            fresh.sent,
            [
                b'{"method": "call", "name": "press_key", '
                b'"args": {"key": "Return"}}\n'
            ],
        )

    def test_isError_envelope_surfaces_driver_text_as_error(self):
        line = (
            json.dumps(
                {
                    "ok": True,
                    "result": {
                        "content": [
                            {"text": "Missing required integer field: pid", "type": "text"}
                        ],
                        "isError": True,
                    },
                }
            ).encode()
            + b"\n"
        )
        fake = _FakeDriverSocket(chunks=[line])
        with mock.patch.object(macos_cua, "_connect_driver_socket", return_value=fake):
            with mock.patch.object(macos_cua.subprocess, "run"):
                result = macos_cua.call_driver("press_key", {"key": "escape"})
        self.assertEqual(result, {"error": "Missing required integer field: pid"})

    def test_live_daemon_parses_our_argument_envelope(self):
        """The daemon reads args under one specific key; fakes cannot prove which."""
        sock_path = os.path.expanduser(
            "~/Library/Caches/cua-driver/cua-driver.sock"
        )
        if not os.path.exists(sock_path):
            self.skipTest("cua-driver daemon socket not present")
        macos_cua.reset_driver_socket()
        with mock.patch.dict(os.environ, {"MACOS_CUA_DRIVER_SOCKET": sock_path}):
            result = macos_cua.call_driver(
                "get_window_state", {"pid": os.getpid(), "window_id": 0}, timeout=20
            )
        macos_cua.reset_driver_socket()
        error = str(result.get("error") or "")
        self.assertNotIn(
            "Missing required integer field: pid",
            error,
            "daemon did not read pid from our envelope: arguments are not reaching it",
        )

    def test_socket_success_records_one_driver_telemetry_call(self):
        fake = _FakeDriverSocket(chunks=[_envelope_line({"ok": True})])
        with mock.patch.object(macos_cua, "_connect_driver_socket", return_value=fake):
            with mock.patch.object(macos_cua.subprocess, "run") as run:
                macos_cua.call_driver("get_window_state")
        run.assert_not_called()
        self.assertEqual(macos_cua.telemetry_read()["driver_calls"], 1)
        self.assertEqual(macos_cua.driver_call_stats()["calls"], 1)


if __name__ == "__main__":
    unittest.main()
