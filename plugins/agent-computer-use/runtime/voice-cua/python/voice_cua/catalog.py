"""Secrets catalog — one metadata JSON per key under ~/.config/voice-cua/.secret/

Values live in macOS Keychain only. Labels/roles are never hardcoded in Python.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

DEFAULT_SECRETS_DIR = Path("~/.config/voice-cua/.secret").expanduser()
LEGACY_CATALOG = Path("~/.config/voice-cua/catalog.json").expanduser()
BUNDLED_SECRETS_DIR = Path(__file__).resolve().parents[2] / "config" / ".secret"
SECURITY = "/usr/bin/security"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
_FORBIDDEN_FIELDS = frozenset({"value", "secret", "password", "api_key", "token"})
_SKIP_SECRET_FILES = frozenset({"labels.json"})


def secrets_dir() -> Path:
    override = os.environ.get("VOICE_CUA_SECRETS_DIR") or os.environ.get("VOICE_CUA_CATALOG")
    if override:
        p = Path(override).expanduser()
        if p.suffix == ".json":
            return p.parent / ".secret"
        return p
    return DEFAULT_SECRETS_DIR


def catalog_path() -> Path:
    """Directory holding per-secret JSON files (metadata only)."""
    return secrets_dir()


def secret_file(key_id: str) -> Path:
    if not _ID_RE.match(key_id):
        raise ValueError("invalid secret id")
    return secrets_dir() / f"{key_id}.json"


def _validate_row(row: dict[str, Any]) -> None:
    for bad in _FORBIDDEN_FIELDS:
        if bad in row:
            raise ValueError(f"secret metadata must not contain field '{bad}'")


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    key_id = str(row.get("id") or "")
    if not _ID_RE.match(key_id):
        raise ValueError("id must be lowercase alnum/._- (2-64 chars)")
    for req in ("label", "service", "account"):
        if not str(row.get(req) or "").strip():
            raise ValueError(f"missing {req}")
    aliases = row.get("aliases")
    if aliases is None:
        aliases = []
    elif not isinstance(aliases, list):
        raise ValueError("aliases must be a list of strings")
    inject = row.get("inject")
    if inject is None:
        inject = []
    elif not isinstance(inject, list):
        raise ValueError("inject must be a list")
    clean: dict[str, Any] = {
        "id": key_id,
        "platform": str(row.get("platform") or "generic"),
        "env": str(row.get("env") or "local"),
        "label": str(row["label"]).strip(),
        "service": str(row["service"]).strip(),
        "account": str(row["account"]).strip(),
        "aliases": [str(a).strip() for a in aliases if str(a).strip()],
        "inject": inject,
    }
    role = str(row.get("role") or "").strip()
    if role:
        clean["role"] = role
    notes = str(row.get("notes") or "").strip()
    if notes:
        clean["notes"] = notes
    _validate_row(clean)
    return clean


def _write_row(row: dict[str, Any]) -> Path:
    clean = _normalize_row(row)
    d = secrets_dir()
    d.mkdir(parents=True, mode=0o700, exist_ok=True)
    p = secret_file(clean["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(clean, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)
    os.chmod(p, 0o600)
    return p


def _read_row(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: secret file must be an object")
    if "id" not in data:
        data["id"] = path.stem
    return _normalize_row(data)


def _migrate_legacy_catalog() -> None:
    if not LEGACY_CATALOG.exists():
        return
    d = secrets_dir()
    d.mkdir(parents=True, mode=0o700, exist_ok=True)
    existing = list(d.glob("*.json"))
    if existing:
        backup = LEGACY_CATALOG.with_suffix(".json.migrated")
        if not backup.exists():
            shutil.move(str(LEGACY_CATALOG), str(backup))
        return
    try:
        legacy = json.loads(LEGACY_CATALOG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    keys = legacy.get("keys") if isinstance(legacy, dict) else None
    if not isinstance(keys, list) or not keys:
        return
    for row in keys:
        if isinstance(row, dict) and row.get("id"):
            _write_row(row)
    backup = LEGACY_CATALOG.with_suffix(".json.migrated")
    if not backup.exists():
        shutil.move(str(LEGACY_CATALOG), str(backup))


def load_catalog(path: Path | None = None) -> dict[str, Any]:
    d = path or secrets_dir()
    if d.is_file():
        # Legacy override: single catalog.json path
        data = json.loads(d.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("catalog must be an object")
        keys = data.get("keys") or []
        for row in keys:
            if isinstance(row, dict):
                _validate_row(row)
        return {"version": 1, "keys": keys}

    _migrate_legacy_catalog()
    keys: list[dict[str, Any]] = []
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            if p.name.endswith(".tmp") or p.name in _SKIP_SECRET_FILES:
                continue
            try:
                keys.append(_read_row(p))
            except (ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"{p}: {exc}") from exc
    return {"version": 1, "keys": keys}


def save_catalog(data: dict[str, Any], path: Path | None = None) -> Path:
    d = path or secrets_dir()
    if d.is_file():
        tmp = d.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(d)
        os.chmod(d, 0o600)
        return d
    for row in data.get("keys") or []:
        if isinstance(row, dict) and row.get("id"):
            _write_row(row)
    return d


def save_key(row: dict[str, Any]) -> Path:
    return _write_row(row)


def delete_key_file(key_id: str) -> None:
    p = secret_file(key_id)
    if p.exists():
        p.unlink()


def find_key(catalog: dict[str, Any], key_id: str) -> dict[str, Any] | None:
    for row in catalog.get("keys") or []:
        if isinstance(row, dict) and row.get("id") == key_id:
            return row
    return None


def find_by_role(catalog: dict[str, Any], role: str) -> dict[str, Any] | None:
    needle = role.strip()
    if not needle:
        return None
    for row in catalog.get("keys") or []:
        if isinstance(row, dict) and str(row.get("role") or "").strip() == needle:
            return row
    return None


def labels_for_row(row: dict[str, Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for candidate in [str(row.get("label") or ""), *(row.get("aliases") or [])]:
        c = candidate.strip()
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def upsert_key(catalog: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    clean = _normalize_row(row)
    save_key(clean)
    keys = [k for k in catalog.get("keys") or [] if not (isinstance(k, dict) and k.get("id") == clean["id"])]
    keys.append(clean)
    catalog["keys"] = keys
    return clean


def init_secrets_from_bundle(*, force: bool = False) -> list[str]:
    """Copy bundled config/.secret/*.json into user secrets dir (skip existing unless force)."""
    src = BUNDLED_SECRETS_DIR
    if not src.is_dir():
        return []
    d = secrets_dir()
    d.mkdir(parents=True, mode=0o700, exist_ok=True)
    installed: list[str] = []
    for p in sorted(src.glob("*.json")):
        if p.name in _SKIP_SECRET_FILES:
            continue
        dest = d / p.name
        if dest.exists() and not force:
            continue
        row = _read_row(p)
        _write_row(row)
        installed.append(row["id"])
    return installed


def keychain_exists(label: str) -> bool:
    r = subprocess.run(
        [SECURITY, "find-generic-password", "-l", label],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def keychain_get(label: str) -> str:
    r = subprocess.run(
        [SECURITY, "find-generic-password", "-l", label, "-w"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Keychain miss for label={label!r}")
    return r.stdout.rstrip("\n")


def keychain_put(
    *,
    service: str,
    account: str,
    label: str,
    value: str | None = None,
) -> None:
    """Store/update. If value is None, security prompts interactively (-w)."""
    cmd = [
        SECURITY,
        "add-generic-password",
        "-U",
        "-s", service,
        "-a", account,
        "-l", label,
        "-w",
    ]
    if value is None:
        subprocess.run(cmd, check=True)
        return
    with tempfile.NamedTemporaryFile("w", delete=False) as tf:
        tf.write(value)
        tf.flush()
        path = tf.name
    try:
        os.chmod(path, 0o600)
        secret = Path(path).read_text(encoding="utf-8")
        subprocess.run(cmd + [secret], check=True, capture_output=True)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def keychain_delete(label: str) -> None:
    subprocess.run(
        [SECURITY, "delete-generic-password", "-l", label],
        capture_output=True,
        check=False,
    )
