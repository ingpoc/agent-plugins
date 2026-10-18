"""Synthetic ledger for the isolation fixture. Temp data dir only. Never ~/.context-ledger.

Every id here is a made-up `dec0deNN` prefix and every summary is written for this
fixture. Nothing in this module or in tests/fixtures/isolation_30.json describes a
record in the owner's ledger.
"""

from __future__ import annotations

import json
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
        mcp=False,
    )


def build(data: Path) -> Path:
    data.mkdir(mode=0o700, exist_ok=True)
    init_ledger(data, None, "agent-plugins", "synthetic")
    registry = {
        "projects": [
            "agent-plugins",
            "open-resource-network",
            "aadhaarchain",
            "brandgpt",
            "hisab-pune",
            "context-ledger",
            "example-service",
        ]
    }
    (data / "project-registry.json").write_text(json.dumps(registry) + "\n", encoding="utf-8")
    store = Store(data, actor_channel="agent")
    try:
        rows = [
            ("dec0de01", "Cursor CLI review keychain prompt blocks a headless run",
             "The keychain prompt blocks a headless Cursor CLI review", ["cursor-cli"]),
            ("dec0de02", "Cursor CLI review answers with the cursor agent reviewer",
             "That review half is separate from the keychain prompt on Cursor CLI", ["cursor-cli"]),
            ("dec0de03", "Comet browser lease is held by one agent at a time",
             "One agent holds the comet browser lease", ["comet"]),
            ("dec0de04", "Comet browser lease held by another agent waits for release",
             "The comet browser lease stays with the agent that holds it", ["comet", "irctc"]),
            ("dec0de05", "Comet Control owns the browser lease for every agent",
             "The comet browser lease has one owner across agents", ["comet"]),
            ("dec0de06", "check.py merge gate failing on PR stays fail loud",
             "A failing check.py merge gate on a PR must stay loud", ["open-resource-network"]),
            ("dec0de07", "check.py merge gate failing on PR names the broken check",
             "The check.py merge gate on a PR names the broken check", ["open-resource-network"]),
            ("dec0de08", "check.py merge gate failing on PR is a free-plan lesson",
             "A free-plan check.py merge gate on a PR still fails loud", ["open-resource-network"]),
            ("dec0de09", "Codex re-review failed.",
             "sha abcdef1 and pr 15 merged.", ["open-resource-network"]),
            ("dec0de10", "Codex review fails before posting PR comments on this repo",
             "The codex review fails before posting PR comments", ["context-ledger"]),
            ("dec0de11", "Garden watering schedule for the courtyard beds",
             "Water the courtyard beds on a fixed weekday", ["aadhaarchain"]),
            ("dec0de12", "Keep a version pin out of a lasting rule for every project.",
             "The rule stays because a pin is not the rule. The pin was 0.1.26.", ["context-ledger"]),
            ("dec0de13", "Status update.",
             "pr 9 merged.", ["aadhaarchain"]),
        ]
        for prefix, summary, rationale, entities in rows:
            _put(store, prefix, summary, rationale, entities)
    finally:
        store.close()
    return data / "ledger" / "ledger.sqlite3"
