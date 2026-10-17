"""Synthetic ledger for the isolation fixture. Temp data dir only. Never ~/.context-ledger."""

from __future__ import annotations

import uuid
from pathlib import Path

from context_ledger.store import Store, init_ledger


def _id(prefix: str) -> str:
    return f"{prefix}-0000-4000-8000-000000000001"


def _put(store: Store, prefix: str, summary: str, rationale: str, entities: list[str]) -> None:
    store.record(
        {
            "request_id": str(uuid.uuid4()),
            "decision_id": _id(prefix),
            "occurred_at": None,
            "body": {
                "kind": "decision",
                "summary": summary,
                "situation": summary,
                "action": summary,
                "rationale": rationale,
                "constraints": [],
                "applicability": "stored before the scope token existed",
                "entities": entities,
                "evidence": [],
                "sensitivity": "model_safe",
                "approval": {"state": "none", "authority_ref": None},
            },
            "outcome": {"status": "pending", "note": "", "evidence": []},
        },
        mcp=True,
    )


def build(data: Path) -> Path:
    data.mkdir(mode=0o700, exist_ok=True)
    init_ledger(data, None, "agent-plugins", "synthetic")
    store = Store(data, actor_channel="agent")
    try:
        rows = [
            ("d7f6cc8d", "Cursor CLI review keychain prompt blocks a headless run",
             "The keychain prompt blocks a headless Cursor CLI review", ["cursor-cli"]),
            ("a4abc29c", "Cursor CLI review answers with the cursor agent reviewer",
             "That review half is separate from the keychain prompt on Cursor CLI", ["cursor-cli"]),
            ("e5ff9784", "Comet browser lease is held by one agent at a time",
             "One agent holds the comet browser lease", ["comet"]),
            ("2599cee1", "Comet browser lease held by another agent waits for release",
             "The comet browser lease stays with the agent that holds it", ["comet"]),
            ("04f17360", "Comet Control owns the browser lease for every agent",
             "The comet browser lease has one owner across agents", ["comet"]),
            ("7d3941cc", "check.py merge gate failing on PR stays fail loud",
             "A failing check.py merge gate on a PR must stay loud", ["open-resource-network"]),
            ("a8babf06", "check.py merge gate failing on PR names the broken check",
             "The check.py merge gate on a PR names the broken check", ["open-resource-network"]),
            ("b13d5e4f", "check.py merge gate failing on PR is a free-plan lesson",
             "A free-plan check.py merge gate on a PR still fails loud", ["open-resource-network"]),
            ("4e5d0bad", "Codex re-review failed.",
             "sha abcdef1 and pr 15 merged.", ["open-resource-network"]),
            ("6c7fd233", "Codex review fails before posting PR comments on this repo",
             "The codex review fails before posting PR comments", ["context-ledger"]),
            ("b32beeea", "Garden watering schedule for the courtyard beds",
             "Water the courtyard beds on a fixed weekday", ["aadhaarchain"]),
            ("aa11aa11", "Keep the merge gate closed for every project.",
             "The gate stays closed because a pin is not a rule. The pin was 0.1.26.", ["context-ledger"]),
            ("bb22bb22", "Status update.",
             "pr 9 merged.", ["aadhaarchain"]),
        ]
        for prefix, summary, rationale, entities in rows:
            _put(store, prefix, summary, rationale, entities)
    finally:
        store.close()
    return data / "ledger" / "ledger.sqlite3"
