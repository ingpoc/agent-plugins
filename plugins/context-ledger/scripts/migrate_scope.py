#!/usr/bin/env python3
"""One-time re-scope. Default is a dry-run JSON report. --apply writes events.

Refuses a --data dir that is, or binds to the same ledger_dir as, a live root
(~/.context-ledger, ~/.agents/plugin-data/context-ledger, $CONTEXT_LEDGER_DATA).
Never hard-deletes. Archive sets lifecycle archived and copies the row, body
included, into data/archive/<date>.json.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_ledger.contracts import (  # noqa: E402
    APPLICABILITY_SCOPE_RE,
    PROJECT_SCOPE_IDS,
    SCOPE_RE,
    TOOL_SCOPE_IDS,
    decision_surface,
    pure_status,
    strip_volatile_tokens,
    volatile_kinds,
)
from context_ledger.store import Store  # noqa: E402

_TEXT_FIELDS = ("summary", "situation", "action", "rationale", "applicability")
# Explicit fleet cues. One allowlisted project entity sets that project unless one of these appears.
_FLEET_TEXT = re.compile(
    r"\b(?:any|every|another) project\b|\bnot about (?:this|the) project\b|\bfleet\b"
    r"|\bevery (?:bot|agent)\b|\ball (?:bots|agents|agent projects)\b|\bcross-project\b"
    r"|\bstanding rule\b|\bnever\b[^.;]*\bon the mini\b",
    re.I,
)
# Entities that name a part of an allowlisted project.
ENTITY_PROJECT_ALIASES = {"context-ledger": "agent-plugins"}
# A task tied to a PR or Project: an imperative step plus a merge, findings, or review tie.
_TASK_VERB = re.compile(
    r"(?:^|[\u2192;:]\s*|\bthen\s+)(?:fix|address|resolve|rerun|re-run|run|merge|land|finish|hold)\b",
    re.I,
)
_TASK_TIE = re.compile(r"\bbefore (?:merge|merging|accepting)\b|\bfindings?\b|\bre-?review\b", re.I)
_RULE_WORDS = re.compile(r"\b(?:must|never|always|only|when|whenever|because|rule)\b", re.I)


def _sentences(text: str) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    return [part.strip() for part in re.split(r"(?<=[.!;])\s+", text) if part.strip()]


def project_allowlist(data: Path | None) -> frozenset[str]:
    """Built-in project ids plus any named in binding.json or project-registry.json."""
    extra: list[str] = []
    if data is not None:
        registry = data / "project-registry.json"
        if registry.is_file():
            try:
                obj = json.loads(registry.read_text(encoding="utf-8"))
                extra.extend(str(item) for item in (obj.get("projects") or []))
            except (OSError, ValueError, TypeError):
                pass
        binding = data / "binding.json"
        if binding.is_file():
            try:
                obj = json.loads(binding.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                obj = {}
            if isinstance(obj.get("project"), str):
                extra.append(obj["project"])
            if isinstance(obj.get("projects"), list):
                extra.extend(str(item) for item in obj["projects"])
    ids = set(PROJECT_SCOPE_IDS)
    for item in extra:
        folded = item.casefold()
        if folded != "fleet" and folded not in TOOL_SCOPE_IDS and SCOPE_RE.fullmatch(folded):
            ids.add(folded)
    return frozenset(ids)


def _kept_scope(existing: str, projects: frozenset[str]) -> str | None:
    if existing == "fleet":
        return existing
    if existing == "project:fleet":
        return None
    if existing.startswith("project:"):
        return existing if existing.split(":", 1)[1] in projects else None
    if existing.startswith("tool:"):
        return existing if existing.split(":", 1)[1] in TOOL_SCOPE_IDS else None
    return None


def task_instruction(summary: str, pr_tied: bool) -> bool:
    """True when the decision is a one-off task step tied to a PR or Project, not a lasting rule."""
    summary = summary or ""
    if _RULE_WORDS.search(summary) or not _TASK_VERB.search(summary):
        return False
    return pr_tied or _TASK_TIE.search(summary) is not None


def infer_scope(body: dict, projects: frozenset[str] | None = None) -> tuple[str, str]:
    allowed = projects if projects is not None else PROJECT_SCOPE_IDS
    existing = body.get("scope")
    if isinstance(existing, str) and APPLICABILITY_SCOPE_RE.fullmatch(existing):
        kept = _kept_scope(existing, allowed)
        if kept is not None:
            return kept, "scope already set"
    entities = [str(item).casefold() for item in body.get("entities") or []]
    text = decision_surface(body.get("summary"), body.get("rationale"), body.get("applicability"))
    if "fleet" in entities or _FLEET_TEXT.search(text):
        return "fleet", "fleet cue or fleet entity"
    project_ids = sorted(
        {ENTITY_PROJECT_ALIASES.get(item, item) for item in entities}
        & set(allowed)
    )
    if len(project_ids) == 1:
        return f"project:{project_ids[0]}", "one allowlisted project"
    if len(project_ids) > 1:
        return "fleet", "several projects, wider scope"
    tools = [item for item in entities if item in TOOL_SCOPE_IDS]
    if len(tools) == 1:
        return f"tool:{tools[0]}", "one known tool"
    if tools:
        return "fleet", "several tools, wider scope"
    return "fleet", "no allowlisted project or known tool"


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
    pr_tied = False
    for field in _TEXT_FIELDS:
        raw = str(new.get(field) or "")
        cleaned, tokens = strip_volatile_tokens(raw)
        if field == "summary" and "pr-or-project-id" in volatile_kinds(raw):
            pr_tied = True
        moved.extend(tokens)
        kept: list[str] = []
        for sentence in _sentences(cleaned):
            if pure_status(sentence):
                moved.append(sentence)
            else:
                kept.append(sentence)
        if field in new:
            new[field] = " ".join(kept).strip()
    if task_instruction(new.get("summary") or "", pr_tied) and not _RULE_WORDS.search(new.get("rationale") or ""):
        return None, "task tied to a PR or Project, not a lasting rule"
    if not moved:
        return new, "rescope"
    moved = list(dict.fromkeys(item.strip() for item in moved if item.strip()))
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


def classify(row: dict, projects: frozenset[str] | None = None) -> dict:
    body = row["body"]
    old = body.get("scope") or body.get("applicability") or ""
    new_scope, scope_reason = infer_scope(body, projects)
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
            "_archived_body": body,
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
    projects = project_allowlist(store.plugin_data)
    return [classify(row, projects) for row in load_rows(store)]


def _public(row: dict) -> dict:
    return {key: row[key] for key in ("id", "old_scope", "new_scope", "kind", "action", "reason")}


def _live_roots() -> list[Path]:
    roots = [Path.home() / ".context-ledger", Path.home() / ".agents" / "plugin-data" / "context-ledger"]
    env = os.environ.get("CONTEXT_LEDGER_DATA")
    if env:
        roots.append(Path(env))
    return roots


def _bound_ledger_dir(data: Path) -> Path | None:
    try:
        obj = json.loads((data / "binding.json").read_text(encoding="utf-8"))
        ledger = Path(obj["ledger_dir"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return ledger.resolve() if ledger.is_absolute() else None


def _live_ledger_dirs(root: Path) -> set[Path]:
    dirs = {(root / "ledger").resolve()}
    bound = _bound_ledger_dir(root)
    if bound is not None:
        dirs.add(bound)
    return dirs


def _refuse_live(data: Path, live_roots: list[Path] | None = None) -> None:
    roots = [Path(root).resolve() for root in (live_roots if live_roots is not None else _live_roots())]
    target = data.resolve()
    target_ledger = _bound_ledger_dir(target)
    for root in roots:
        if target == root:
            raise SystemExit(f"refusing live ledger root {root}")
        if target_ledger is not None and target_ledger in _live_ledger_dirs(root):
            raise SystemExit(f"refusing: --data binds to the live ledger_dir of {root}")


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
            archived_bodies.append({**_public(row), "body": row.get("_archived_body")})
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
