from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugin"))

from comet_control import tools  # noqa: E402


SECRET = "tool-private-lease-token"


def decoded(result: Any) -> dict[str, Any]:
    if isinstance(result, str):
        payload = json.loads(result)
    elif isinstance(result, dict):
        payload = result
    else:
        raise AssertionError(f"unexpected tool result: {type(result)!r}")
    if not isinstance(payload, dict):
        raise AssertionError("tool result was not an object")
    return payload


class ToolLeaseOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        tools._LEASE_TOKENS.clear()
        self.requests: list[dict[str, Any]] = []

    def bridge(self, request: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
        self.requests.append({**request, "_timeout": timeout_seconds})
        if request["action"] == "session_preflight":
            return {
                "success": True,
                "session_id": request["sessionId"],
                "lease_token": SECRET,
                "nested": {"leaseToken": SECRET},
                "window_id": 1,
                "tab_id": 2,
            }
        if request["action"] == "run":
            return {
                "success": True,
                "results": [{"message": f"private={SECRET}", "leaseToken": SECRET}],
            }
        if request["action"] == "session_closeout":
            return {"success": True, "already_closed": False}
        return {"success": True}

    def invoke(self, args: dict[str, Any], task_id: str | None = "task-a") -> dict[str, Any]:
        with (
            patch.object(tools, "_diagnostics", return_value={"ready": True}),
            patch.object(tools, "_extension_health", return_value={"ready": True}),
            patch.object(tools, "_run_extension_bridge", side_effect=self.bridge),
        ):
            return decoded(tools._handle_comet_control_browser(args, task_id=task_id))

    def test_preflight_caches_token_without_returning_it(self) -> None:
        result = self.invoke(
            {"action": "preflight", "url": "https://example.com/"},
            task_id="agent-a",
        )
        serialized = json.dumps(result)
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn("lease_token", serialized)
        self.assertNotIn("leaseToken", serialized)
        self.assertEqual(tools._LEASE_TOKENS["agent-a"], SECRET)

    def test_retryable_preflight_failure_retains_private_cleanup_capability(self) -> None:
        failure = {
            "success": False,
            "error_code": "LEASE_CLEANUP_INCOMPLETE",
            "retryable": True,
            "lease_token": SECRET,
            "error": "Owned target still exists",
        }
        with (
            patch.object(tools, "_diagnostics", return_value={"ready": True}),
            patch.object(tools, "_extension_health", return_value={"ready": True}),
            patch.object(tools, "_run_extension_bridge", return_value=failure),
        ):
            result = decoded(
                tools._handle_comet_control_browser(
                    {"action": "preflight", "url": "https://example.com/"},
                    task_id="agent-failed",
                )
            )
        serialized = json.dumps(result)
        self.assertFalse(result["success"])
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn("lease_token", serialized)
        self.assertEqual(tools._LEASE_TOKENS["agent-failed"], SECRET)

    def test_run_uses_process_owned_token_and_redacts_every_output_shape(self) -> None:
        self.invoke({"action": "preflight", "url": "https://example.com/"})
        result = self.invoke(
            {"action": "run", "actions": [{"type": "page_context"}]}
        )
        request = self.requests[-1]
        self.assertEqual(request["leaseToken"], SECRET)
        serialized = json.dumps(result)
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn("leaseToken", serialized)

    def test_closeout_uses_and_forgets_process_owned_token(self) -> None:
        self.invoke({"action": "preflight", "url": "https://example.com/"})
        result = self.invoke({"action": "closeout"})
        self.assertTrue(result["success"])
        self.assertNotIn("task-a", tools._LEASE_TOKENS)
        self.assertEqual(self.requests[-1]["leaseToken"], SECRET)

    def test_lease_actions_fail_closed_without_agent_identity(self) -> None:
        for action in ("preflight", "run", "closeout"):
            with self.subTest(action=action):
                result = self.invoke({"action": action}, task_id=None)
                self.assertFalse(result["success"])
                self.assertIn("stable task_id", result["error"])
        self.assertEqual(self.requests, [])

    def test_schema_does_not_offer_private_token_input(self) -> None:
        properties = tools.COMET_CONTROL_BROWSER_SCHEMA["parameters"]["properties"]
        self.assertNotIn("lease_token", properties)

    def test_health_requires_broker_attestation_before_extension_status(self) -> None:
        responses = [
            {"success": True, "broker": {"runtime_verified": True, "user_data_dir": "/dedicated"}},
            {"success": True, "active_agent_sessions": 2, "cua_claim": None},
        ]
        with patch.object(tools, "_socket_reachable", return_value=True), patch.object(
            tools, "_run_extension_bridge", side_effect=responses
        ) as bridge:
            health = tools._extension_health(timeout_seconds=3)
        self.assertTrue(health["ready"])
        self.assertEqual(health["runtime"]["user_data_dir"], "/dedicated")
        self.assertEqual(health["active_agent_sessions"], 2)
        self.assertNotIn("active_tab", health)
        self.assertEqual(
            [call.args[0]["action"] for call in bridge.call_args_list],
            ["broker_status", "status"],
        )

    def test_missing_socket_fails_fast_with_dedicated_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            tools, "_SOCKET_PATH", Path(directory) / "missing.sock"
        ):
            result = tools._run_extension_bridge(
                {"action": "status"}, timeout_seconds=3
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "SOCKET_DOWN")
        self.assertIn("launch-wip-comet.sh", result["error"])

    def test_install_info_has_no_personal_profile_route(self) -> None:
        serialized = json.dumps(tools._install_info())
        self.assertNotIn("robo-trader-testing", serialized)
        self.assertIn("launch-wip-comet.sh", serialized)


if __name__ == "__main__":
    unittest.main()
