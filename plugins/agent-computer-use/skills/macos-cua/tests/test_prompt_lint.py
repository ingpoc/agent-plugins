#!/usr/bin/env python3
"""Guru's rule: every macOS approval happens once, in onboarding (or the same Settings Grant row).
Fails if any prompting API appears outside the PermissionBroker choke point / cached readers."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
PLUGIN = SKILL.parents[1]
SOURCES = SKILL / "service" / "Sources" / "CUAService"
BROKER = "PermissionBroker.swift"

# Prompting APIs: only PermissionBroker.swift may call them.
SWIFT_REQUEST = [
    r"AXIsProcessTrustedWithOptions", r"kAXTrustedCheckOptionPrompt", r"\bCGRequest\w+\(",
    r"\.requestAccess\(", r"requestFullAccessToEvents", r"requestWriteOnlyAccessToEvents",
    r"\.requestAuthorization\b", r"SMAppService\.mainApp\.register\(",
    r"AEDeterminePermissionToAutomateTarget\([^)]*,\s*true\)", r"UNUserNotificationCenter",
]


def swift_files():
    yield from SOURCES.glob("*.swift")
    yield from (SKILL / "service" / "Resources").glob("*.swift")


class PromptLint(unittest.TestCase):
    def test_request_apis_only_in_broker(self):
        bad = []
        for f in swift_files():
            if f.name == BROKER:
                continue
            for i, line in enumerate(f.read_text().splitlines(), 1):
                code = line.split("//", 1)[0]
                for pat in SWIFT_REQUEST:
                    if re.search(pat, code):
                        bad.append(f"{f.name}:{i}: {line.strip()}")
        self.assertEqual(bad, [], "prompting API outside PermissionBroker")

    def test_broker_has_the_requests(self):
        src = (SOURCES / BROKER).read_text()
        for needle in ["AXIsProcessTrustedWithOptions", "CGRequestScreenCaptureAccess", "CGRequestPostEventAccess",
                       "AVCaptureDevice.requestAccess", "requestFullAccessToEvents", "requestAuthorization",
                       "SMAppService.mainApp.register", "KeychainGrant.readAll", "typeWildCard, true)"]:
            self.assertIn(needle, src)
        self.assertIn("allowed(origin)", src)

    def test_automation_status_never_asks(self):
        for f in swift_files():
            code = "\n".join(l.split("//", 1)[0] for l in f.read_text().splitlines())
            for m in re.finditer(r"AEDeterminePermissionToAutomateTarget\(([^)]*)\)", code):
                last = m.group(1).split(",")[-1].strip()
                if f.name != BROKER:
                    self.assertEqual(last, "false", f"{f.name}: {m.group(0)}")

    def test_secret_data_only_in_cached_reader(self):
        hits = []
        for f in swift_files():
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if re.search(r"kSecReturnData as String:\s*true", line.split("//", 1)[0]):
                    hits.append((f.name, i))
        self.assertEqual([h[0] for h in hits], ["IslandSettings.swift"], hits)
        src = (SOURCES / "IslandSettings.swift").read_text()
        reader = src[src.index("static func readData(source:"):]
        self.assertIn("kSecReturnData", reader[:600], "data read must live in KeychainTokenStore.readData")
        self.assertIn("cache.withLock", src[src.index("struct KeychainTokenStore"):src.index("static func readData(source:")])

    def test_python_runtime_reads_only_approved(self):
        py = PLUGIN / "runtime" / "voice-cua" / "python" / "voice_cua"
        # Raw keychain_get: definition + approved wrapper, and two explicit user-run tools
        # (secrets_cli, keychain_access: the agent fetching a user's own secret on request).
        allowed = {"catalog.py", "secrets_cli.py", "keychain_access.py"}
        bad = [f.name for f in py.glob("*.py") if f.name not in allowed and re.search(r"\bkeychain_get\(", f.read_text())]
        self.assertEqual(bad, [])
        auth = (py / "auth.py").read_text()
        self.assertIn('keychain_get_approved(label, "openai")', auth)
        self.assertIn('keychain_get_approved(label, "xai")', auth)
        hub = (py / "island_hub.py").read_text()
        self.assertLess(hub.index("keychain_approved("), hub.index('"find-generic-password"'))
        post = (PLUGIN / "bin" / "island-post").read_text()
        self.assertLess(post.index("keychain-approved.json"), post.index('"find-generic-password"'))

    def test_mic_preflight_never_prompts(self):
        audio = (PLUGIN / "runtime" / "voice-cua" / "python" / "voice_cua" / "audio_io.py").read_text()
        self.assertNotIn("--preflight-mic", audio.split("def _mic_authorized")[1].split("def ")[0])
        self.assertNotIn("requestAccess", (SKILL / "service" / "Resources" / "mic_preflight.swift").read_text())


if __name__ == "__main__":
    unittest.main()
