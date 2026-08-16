import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check-cua-coexistence.py"
SPEC = importlib.util.spec_from_file_location("cua_coexistence", SCRIPT)
coexistence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(coexistence)


def managed_command(pid: int = 99) -> str:
    return (
        f"/Applications/Comet.app/Contents/MacOS/Comet "
        f"--user-data-dir={coexistence.USER_DATA_DIR} --remote-debugging-port=0"
    )


def logged_in_comet_command() -> str:
    return "/Applications/Comet.app/Contents/MacOS/Comet"


def responses(sessions, *, browser_pid=99):
    def request(payload):
        if payload["type"] == "broker_status":
            return {
                "success": True,
                "broker": {
                    "browser_pid": browser_pid,
                    "runtime_verified": True,
                    "user_data_dir": str(coexistence.USER_DATA_DIR),
                },
            }
        if payload["type"] == "sessions":
            return {"success": True, "sessions": sessions}
        raise AssertionError(payload)

    return request


class CuaCoexistenceTests(unittest.TestCase):
    def test_disjoint_native_app_does_not_query_comet_control(self):
        queried = []
        result = coexistence.evaluate(
            7,
            "native-app",
            process_command_fn=lambda _pid: "/System/Applications/Calculator.app/Calculator",
            bridge_fn=lambda payload: queried.append(payload),
        )
        self.assertTrue(result["safe"])
        self.assertFalse(result["managed_browser"])
        self.assertEqual(result["reason"], "disjoint-native-app")
        self.assertEqual(queried, [])

    def test_managed_browser_rejects_generic_native_control(self):
        with self.assertRaises(coexistence.BoundaryError) as raised:
            coexistence.evaluate(
                99,
                "native-app",
                process_command_fn=lambda _pid: managed_command(),
                bridge_fn=responses([]),
            )
        self.assertEqual(raised.exception.code, "COMET_CONTROL_MANAGED_BROWSER")

    def test_logged_in_comet_profile_is_managed(self):
        result = coexistence.evaluate(
            99,
            "comet-admin",
            process_command_fn=lambda _pid: logged_in_comet_command(),
            bridge_fn=responses([]),
        )
        self.assertTrue(result["managed_browser"])
        self.assertEqual(result["reason"], "empty-runtime-admin-handoff")

    def test_comet_admin_requires_empty_runtime(self):
        with self.assertRaises(coexistence.BoundaryError) as raised:
            coexistence.evaluate(
                99,
                "comet-admin",
                process_command_fn=lambda _pid: managed_command(),
                bridge_fn=responses([{"session_id": "agent-a", "busy": False}]),
            )
        self.assertEqual(raised.exception.code, "COMET_CONTROL_RUNTIME_BUSY")

        result = coexistence.evaluate(
            99,
            "comet-admin",
            process_command_fn=lambda _pid: managed_command(),
            bridge_fn=responses([]),
        )
        self.assertTrue(result["safe"])
        self.assertEqual(result["reason"], "empty-runtime-admin-handoff")

    def test_atomic_admin_acquire_returns_claim_without_inventory_race(self):
        calls = []

        def request(payload):
            calls.append(payload["type"])
            if payload["type"] == "broker_status":
                return responses([])(payload)
            if payload["type"] == "cua_runtime_claim":
                return {
                    "success": True,
                    "claim_token": "claim-secret",
                    "claim": {"claim_id": "claim-1", "expires_at": 999},
                    "active_sessions": 0,
                }
            raise AssertionError(payload)

        result = coexistence.evaluate(
            99,
            "comet-admin",
            acquire=True,
            process_command_fn=lambda _pid: managed_command(),
            bridge_fn=request,
        )
        self.assertTrue(result["safe"])
        self.assertEqual(result["claim_token"], "claim-secret")
        self.assertEqual(calls, ["broker_status", "cua_runtime_claim"])

    def test_native_dialog_never_authorizes_from_public_session_state(self):
        for session_id, sessions in (
            (None, [{"session_id": "agent-a", "busy": False}]),
            ("agent-b", [{"session_id": "agent-a", "busy": False}]),
            ("agent-a", [{"session_id": "agent-a", "busy": True}]),
            ("agent-a", [{"session_id": "agent-a", "busy": False}]),
        ):
            calls = []

            def request(payload):
                calls.append(payload["type"])
                return responses(sessions)(payload)

            with self.subTest(session_id=session_id):
                with self.assertRaises(coexistence.BoundaryError) as raised:
                    coexistence.evaluate(
                        99,
                        "native-dialog",
                        session_id,
                        process_command_fn=lambda _pid: managed_command(),
                        bridge_fn=request,
                    )
                self.assertEqual(raised.exception.code, "COMET_CONTROL_HANDOFF_REQUIRED")
                self.assertEqual(calls, ["broker_status"])

    def test_native_dialog_adopts_only_the_authenticated_claim(self):
        def request(payload):
            if payload["type"] == "broker_status":
                return responses([])(payload)
            if payload["type"] == "cua_runtime_validate":
                self.assertEqual(payload["claimToken"], "handoff-secret")
                return {
                    "success": True,
                    "claim": {
                        "claim_id": "claim-2",
                        "intent": "native-dialog",
                        "session_id": "agent-a",
                        "expires_at": 999,
                    },
                }
            raise AssertionError(payload)

        result = coexistence.evaluate(
            99,
            "native-dialog",
            "agent-a",
            claim_token="handoff-secret",
            process_command_fn=lambda _pid: managed_command(),
            bridge_fn=request,
        )
        self.assertTrue(result["safe"])
        self.assertEqual(result["reason"], "authenticated-native-dialog-claim")

    def test_other_browser_requires_explicit_shell_intent(self):
        personal = "/Applications/Chromium.app/Contents/MacOS/Chromium"
        with self.assertRaises(coexistence.BoundaryError) as raised:
            coexistence.evaluate(
                77,
                "native-app",
                process_command_fn=lambda _pid: personal,
                bridge_fn=lambda payload: self.fail(payload),
            )
        self.assertEqual(raised.exception.code, "BROWSER_PAGE_BOUNDARY")
        result = coexistence.evaluate(
            77,
            "comet-admin",
            process_command_fn=lambda _pid: personal,
            bridge_fn=lambda payload: self.fail(payload),
        )
        self.assertTrue(result["safe"])
        self.assertEqual(result["reason"], "unmanaged-browser-shell")

    def test_release_uses_the_short_lived_claim_capability(self):
        seen = []

        def request(payload):
            seen.append(payload)
            return {"success": True, "released": True, "claim_id": "claim-1"}

        result = coexistence.release_claim("claim-secret", bridge_fn=request)
        self.assertTrue(result["released"])
        self.assertEqual(seen[0]["type"], "cua_runtime_release")
        self.assertEqual(seen[0]["claimToken"], "claim-secret")

    def test_attested_browser_pid_must_match_target(self):
        with self.assertRaises(coexistence.BoundaryError) as raised:
            coexistence.evaluate(
                99,
                "comet-admin",
                process_command_fn=lambda _pid: managed_command(),
                bridge_fn=responses([], browser_pid=100),
            )
        self.assertEqual(raised.exception.code, "COMET_CONTROL_RUNTIME_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
