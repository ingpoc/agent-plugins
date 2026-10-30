"""Samantha's bounded workflow references and repair-before-retry session gate."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

WORKFLOW_ROOT = Path.home() / ".agents/skills/samantha-workflow"
DOCUMENT_MAX = 24_000
INDEX_MAX = 16_000
_SLUG = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
_ROUTE = re.compile(r"^- (.+) → \[([^\[\]]+)\]\(references/([a-z0-9-]+)\.md\)$")
_MUTATIONS = frozenset({"cua_act", "comet_begin", "comet_act", "secrets_put", "secrets_inject", "secrets_provide", "system_volume"})
_STATE_LOCK = threading.RLock()
_active: str | None = None
_repair: str | None = None


class WorkflowError(ValueError):
    pass


def _slug(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 64 or not _SLUG.fullmatch(value):
        raise WorkflowError("invalid workflow slug")
    return value


def _path(name: str) -> Path:
    root = WORKFLOW_ROOT
    for part in (root, *root.parents):
        if part.is_symlink():
            raise WorkflowError("workflow path must not contain symlinks")
    if not root.is_dir():
        raise WorkflowError("samantha-workflow skill is not installed")
    path = root / name
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink():
            raise WorkflowError("workflow path must not contain symlinks")
    return path


def _read(name: str, limit: int) -> str:
    path = _path(name)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise WorkflowError("workflow document exceeds size limit")
    return data.decode("utf-8")


def _write(name: str, text: str) -> None:
    path = _path(name)
    if not path.parent.is_dir():
        raise WorkflowError("workflow references directory is missing")
    fd, temp = tempfile.mkstemp(prefix=".workflow-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _recover() -> None:
    journal = _path(".transaction.json")
    if not journal.exists():
        return
    data = json.loads(_read(".transaction.json", 2 * (DOCUMENT_MAX + INDEX_MAX) + 256))
    if not isinstance(data, dict):
        raise WorkflowError("invalid workflow transaction")
    slug = _slug(data["slug"])
    previous, index = data["document"], data["index"]
    if (slug == "authoring" or not isinstance(index, str) or len(index.encode()) > INDEX_MAX
            or (previous is not None and (not isinstance(previous, str) or len(previous.encode()) > DOCUMENT_MAX))):
        raise WorkflowError("invalid workflow transaction")
    if previous is None:
        _path(f"references/{slug}.md").unlink(missing_ok=True)
    else:
        _write(f"references/{slug}.md", previous)
    _write("SKILL.md", index)
    journal.unlink()


@contextmanager
def _locked():
    with _STATE_LOCK:
        fd = os.open(_path(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _recover()
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _revision(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _error(exc: Exception) -> dict[str, Any]:
    message = str(exc) if isinstance(exc, WorkflowError) else "workflow storage unavailable"
    return {"ok": False, "error": message, "error_type": "workflow_storage"}


def index_context() -> str:
    """Small trigger index for session instructions; references remain lazy."""
    result = read_workflow()
    return str(result.get("content") or "") if result.get("ok") else ""


def read_workflow(arguments: dict[str, Any] | str | None = None) -> dict[str, Any]:
    global _active
    try:
        slug = arguments.get("slug") if isinstance(arguments, dict) else arguments
        with _locked():
            index = _read("SKILL.md", INDEX_MAX)
            if not slug:
                return {"ok": True, "content": index}
            slug = _slug(slug)
            content = _read(f"references/{slug}.md", DOCUMENT_MAX)
            if slug != "authoring":
                _active = slug
            return {"ok": True, "slug": slug, "content": content, "revision": _revision(content),
                    "repair_required": _repair == slug}
    except (OSError, ValueError, KeyError) as exc:
        return _error(exc)


def save_workflow(arguments: dict[str, Any]) -> dict[str, Any]:
    global _repair
    try:
        if not isinstance(arguments, dict):
            raise WorkflowError("workflow arguments must be an object")
        slug = _slug(arguments.get("slug"))
        if slug == "authoring":
            raise WorkflowError("authoring reference is managed by the skill owner")
        title, trigger, content = (arguments.get(k) for k in ("title", "trigger", "content"))
        for value, limit in ((title, 80), (trigger, 240)):
            if not isinstance(value, str) or not value.strip() or len(value) > limit or any(c in value for c in "\r\n[]()"):
                raise WorkflowError("title and trigger must be short single-line text")
        if not isinstance(content, str) or not content.strip() or len(content.encode()) > DOCUMENT_MAX:
            raise WorkflowError("workflow content is empty or exceeds size limit")
        with _locked():
            old_index = _read("SKILL.md", INDEX_MAX)
            path = _path(f"references/{slug}.md")
            previous = _read(f"references/{slug}.md", DOCUMENT_MAX) if path.exists() else None
            revision = _revision(previous) if previous is not None else ""
            if arguments.get("expected_revision", "") != revision:
                return {"ok": False, "error": "workflow changed; read and reconcile before saving", "error_type": "workflow_revision_conflict"}
            if _repair == slug and previous == content:
                raise WorkflowError("change the faulty workflow step before continuing")
            route = f"- {trigger.strip()} → [{title.strip()}](references/{slug}.md)"
            lines = [line for line in old_index.splitlines() if not ((match := _ROUTE.fullmatch(line)) and match[3] == slug)]
            new_index = "\n".join(lines).rstrip() + "\n" + route + "\n"
            if len(new_index.encode()) > INDEX_MAX:
                raise WorkflowError("workflow trigger index exceeds size limit")
            journal = {"slug": slug, "document": previous, "index": old_index}
            _write(".transaction.json", json.dumps(journal, ensure_ascii=False))
            try:
                _write(f"references/{slug}.md", content)
                _write("SKILL.md", new_index)
                _path(".transaction.json").unlink()
            except OSError:
                _recover()
                raise
            if _repair == slug:
                _repair = None
            return {"ok": True, "slug": slug, "revision": _revision(content), "trigger": trigger.strip(),
                    "reference": f"references/{slug}.md", "updated": previous != content}
    except (OSError, ValueError, KeyError) as exc:
        return _error(exc)


def delete_workflow(arguments: dict[str, Any]) -> dict[str, Any]:
    global _active, _repair
    try:
        if not isinstance(arguments, dict):
            raise WorkflowError("workflow arguments must be an object")
        slug = _slug(arguments.get("slug"))
        if slug == "authoring":
            raise WorkflowError("authoring reference is managed by the skill owner")
        expected_revision = arguments.get("expected_revision")
        if not isinstance(expected_revision, str) or not expected_revision:
            raise WorkflowError("current workflow revision is required")
        with _locked():
            old_index = _read("SKILL.md", INDEX_MAX)
            path = _path(f"references/{slug}.md")
            if not path.is_file():
                return {"ok": False, "error": "workflow not found", "error_type": "workflow_not_found"}
            previous = _read(f"references/{slug}.md", DOCUMENT_MAX)
            if expected_revision != _revision(previous):
                return {"ok": False, "error": "workflow changed; read and reconcile before deleting", "error_type": "workflow_revision_conflict"}
            lines = [line for line in old_index.splitlines() if not ((match := _ROUTE.fullmatch(line)) and match[3] == slug)]
            if len(lines) == len(old_index.splitlines()):
                raise WorkflowError("workflow trigger route is missing; repair the index before deleting")
            new_index = "\n".join(lines).rstrip() + "\n"
            journal = {"slug": slug, "document": previous, "index": old_index}
            _write(".transaction.json", json.dumps(journal, ensure_ascii=False))
            try:
                path.unlink()
                _write("SKILL.md", new_index)
                _path(".transaction.json").unlink()
            except OSError:
                _recover()
                raise
            if _active == slug:
                _active = None
            if _repair == slug:
                _repair = None
            return {"ok": True, "slug": slug, "deleted": True}
    except (OSError, ValueError, KeyError) as exc:
        return _error(exc)


def reset_session() -> None:
    global _active, _repair
    with _STATE_LOCK:
        _active = _repair = None


def begin_engagement() -> None:
    """A new user task selects its own workflow; unresolved repairs remain binding."""
    global _active
    with _STATE_LOCK:
        if _repair is None:
            _active = None


def note_failure(name: str, result: dict[str, Any]) -> None:
    """A tool miss belongs to the loaded workflow; approvals are not step failures."""
    global _repair
    with _STATE_LOCK:
        if (_active and name in _MUTATIONS and not result.get("ok")
                and not result.get("needs_confirmation") and result.get("error") != "user denied"):
            _repair = _repair or _active


def mutation_guard(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any] | None:
    with _STATE_LOCK:
        if name == "system_volume" and (arguments or {}).get("direction", "read") == "read":
            return None
        if _repair and name in _MUTATIONS:
            return {"ok": False, "error_type": "workflow_repair_required", "workflow": _repair,
                    "error": "Repair the failed step in this workflow reference before another mutation; read-only inspection and cleanup remain available."}
    return None
