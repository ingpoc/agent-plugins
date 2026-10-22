"""Provider-neutral harness for session-loop tests.

The loop speaks one internal event dialect; each provider translates it at the socket
edge. Loop tests (tools, reconnect, island, usage) run against a pass-through provider so
they exercise the loop itself; GPT-Live translation is covered in test_providers and
test_live_transcripts.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from voice_cua.providers import VoiceProvider


class PassthroughProvider(VoiceProvider):
    name = "test-loop"

    def url(self) -> str:
        return "wss://loop.test.invalid"

    def usage_labels(self, response: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model}


def passthrough(model: str = "test-loop", settings: dict[str, str] | None = None) -> VoiceProvider:
    return PassthroughProvider(model)


_PATCH = patch("voice_cua.realtime_session.make_provider", passthrough)


def install() -> None:
    _PATCH.start()


def uninstall() -> None:
    _PATCH.stop()
