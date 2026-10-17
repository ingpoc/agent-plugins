#!/usr/bin/env python3
"""One-time re-scope. Default is a dry-run JSON report. --apply writes events.

Refuses the live ~/.context-ledger. Never hard-deletes. Archive sets lifecycle
archived and copies the row into data/archive/<date>.json.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_ledger.contracts import (  # noqa: E402
    APPLICABILITY_SCOPE_RE,
    SCOPE_RE,
    TOOL_SCOPE_IDS,
    decision_surface,
    pure_status,
    volatile_kinds,
)
from context_ledger.store import Store  # noqa: E402

_TEXT_FIELDS = ("summary", "situation", "action", "rationale", "applicability")
_FLEET_TEXT = re.compile(r"\b(?:any|every|another) project\b", re.I)


def _sentences(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    return [part.strip() for part in re.split(r"(?<=[.!;])\s+", text) if part.strip()]


def infer_scope(body: dict) -> tuple[str, str]:
    existing = body.get("scope")
    if isinstance(existing, str) and APPLICABILITY_SCOPE_RE.fullmatch(existing):
        return existing, "scope already set"
    entities = [str(item).casefold() for item in body.get("entities") or []]
    text = decision_surface(body.get("summary"), body.get("rationale"), body.get("applicability"))
    if _FLEET_TEXT.search(text):
        return "fleet", "text holds in another project"
    projects = [item for item in entities if item not in TOOL_SCOPE_IDS and SCOPE_RE.fullmatch(item)]
    tools = [item for item in entities if item in TOOL_SCOPE_IDS]
    if len(projects) == 1 and not tools:
        return f"project:{projects[0]}", "single project entity"
    if len(tools) == 1 and not projects:
        return f"tool:{tools[0]}", "single tool entity"
    if projects:
        return f"project:{projects[0]}", "project entity"
    if tools:
        return f"tool:{tools[0]}", "tool entity"
    return "fleet", "no project or tool entity"


def _blank_required(body: dict) -> str | None:
    for field in _TEXT_FIELDS:
        if field == "rationale" and body.get("kind") == "observation":
            continue
        if not str(body.get(field) or "").strip():
            return field
    return None


def rewrite_body(body: dict) -> tuple[dict | None, str]:
    new = json.loads(json.dumps(body))
    moved: list[str] = []
    for field in _TEXT_FIELDS:
        kept: list[str] = []
        for sentence in _sentences(str(new.get(field) or "")):
            if volatile_kinds(sentence) or pure_status(sentence):
                moved.append(sentence)
            else:
                kept.append(sentence)
        if field in new:
            new[field] = " ".join(kept)
    if not moved:
        return new, "rescope"
    surface = decision_surface(new.get("summary"), new.get("rationale"))
    if not surface.strip() or pure_status(surface) or volatile_kinds(surface):
        return None, "volatile text cannot be split from the rule"
    blank = _blank_required(new)
    if blank:
        return None, f"{blank} empty after removing volatile text"
    evidence = list(new.get("evidence") or [])
    if len(evidence) >= 4:
        return None, "evidence list is full"
    evidence.append(
        {"ref": " ".join(moved)[:512], "revision": None, "sha256": None, "state": "unverified"}
    )
    new["evidence"] = evidence
    return new, "volatile pins moved to evidence"


def classify(row: dict) -> dict:
    body = row["body"]
    old = body.get("scope") or body.get("applicability") or ""
    new_scope, scope_reason = infer_scope(body)
    surface = decision_surface(body.get("summary"), body.get("rationale"))
    kind = "state" if pure_status(surface) else "decision"
    if row["lifecycle"] == "archived":
        return {
            "id": row["id"], "old_scope": old, "new_scope": new_scope, "kind": kind,
            "action": "archive", "reason": "already archived",
        }
    rewritten, reason = rewrite_body(body)
    if rewritten is None:
        return {
            "id": row["id"], "old_scope": old, "new_scope": new_scope, "kind": "state",
            "action": "archive", "reason": reason, "_revision": row["revision"],
        }
    rewritten["scope"] = new_scope
    action = "rewrite" if reason != "rescope" else "rescope"
    if action == "rescope" and body.get("scope") == new_scope:
        scope_reason = "already at new scope"
    report_kind = "state" if action == "archive" else "decision"
    item = {
        "id": row["id"], "old_scope": old, "new_scope": new_scope, "kind": report_kind,
        "action": action, "reason": reason if action == "rewrite" else scope_reason,
    }
    if action != "rescope" or body.get("scope") != new_scope:
        item["_body"] = rewritten
        item["_revision"] = row["revision"]
    return item


def load_rows(store: Store) -> list[dict]:
    conn = store.open()
    rows = []
    for row in conn.execute(
        "select decision_id, revision, lifecycle, projection_json from current_records order by decision_id"
    ):
        proj = json.loads(row["projection_json"])
        rows.append(
            {
                "id": row["decision_id"],
                "revision": row["revision"],
                "lifecycle": row["lifecycle"],
                "body": proj["body"],
            }
        )
    return rows


def plan(store: Store) -> list[dict]:
    return [classify(row) for row in load_rows(store)]


def _public(row: dict) -> dict:
    return {key: row[key] for key in ("id", "old_scope", "new_scope", "kind", "action", "reason")}


def _refuse_live(data: Path) -> None:
    live = (Path.home() / ".context-ledger").resolve()
    if data.resolve() == live:
        raise SystemExit("refusing live ledger ~/.context-ledger")


def apply(store: Store, rows: list[dict], *, on: str, data: Path) -> dict:
    archived_bodies = []
    wrote = 0
    for row in rows:
        if "_body" not in row and row["action"] != "archive":
            continue
        if row["action"] == "archive" and row["reason"] == "already archived":
            continue
        if row["action"] == "archive":
            store.append_event(
                {
                    "request_id": str(uuid.uuid4()),
                    "decision_id": row["id"],
                    "expected_revision": row["_revision"],
                    "occurred_at": None,
                    "event_type": "archived",
                    "payload": {"reason": row["reason"]},
                },
                mcp=False,
            )
            archived_bodies.append(_public(row))
            wrote += 1
            continue
        store.append_event(
            {
                "request_id": str(uuid.uuid4()),
                "decision_id": row["id"],
                "expected_revision": row["_revision"],
                "occurred_at": None,
                "event_type": "corrected",
                "payload": {"body": row["_body"], "reason": row["reason"]},
            },
            mcp=False,
        )
        wrote += 1
    archive_path = None
    if archived_bodies:
        folder = data / "archive"
        folder.mkdir(mode=0o700, exist_ok=True)
        archive_path = folder / f"{on}.json"
        prior = []
        if archive_path.exists():
            prior = json.loads(archive_path.read_text(encoding="utf-8"))
        seen = {item["id"] for item in prior}
        prior.extend(item for item in archived_bodies if item["id"] not in seen)
        archive_path.write_text(json.dumps(prior, indent=1) + "\n", encoding="utf-8")
        archive_path.chmod(0o600)
    return {"wrote": wrote, "archive": str(archive_path) if archive_path else None}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--on", default="2026-10-17")
    args = ap.parse_args(argv)
    data = args.data.resolve()
    _refuse_live(data)
    store = Store(data, actor_channel="owner_cli")
    try:
        rows = plan(store)
        if args.apply:
            apply(store, rows, on=args.on, data=data)
    finally:
        store.close()
    json.dump([_public(row) for row in rows], sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
