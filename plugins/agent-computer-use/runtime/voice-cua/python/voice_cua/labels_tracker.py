"""Single JSON registry of all secret labels — available vs missing."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from voice_cua.catalog import catalog_path, keychain_exists, labels_for_row, load_catalog
from voice_cua.keychain_scan import filter_user_secrets, scan_login_keychain

SecretStatus = Literal["available", "missing"]

LEGACY_LABELS_FILE = Path("~/.config/voice-cua/labels.json").expanduser()
PROJECT_SECRET_DIR = Path(__file__).resolve().parents[2] / "config" / ".secret"
DEFAULT_LABELS_FILE = PROJECT_SECRET_DIR / "labels.json"


def labels_tracker_path() -> Path:
    override = os.environ.get("VOICE_CUA_LABELS_FILE")
    return Path(override).expanduser() if override else DEFAULT_LABELS_FILE


def _migrate_legacy_labels_file() -> None:
    legacy = LEGACY_LABELS_FILE
    dest = labels_tracker_path()
    if legacy.exists() and not dest.exists():
        dest.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        dest.write_bytes(legacy.read_bytes())
        os.chmod(dest, 0o600)
        backup = legacy.with_suffix(".json.migrated")
        if not backup.exists():
            legacy.rename(backup)


def _catalog_keys_matched(row: dict[str, Any]) -> list[str]:
    return [lab for lab in labels_for_row(row) if keychain_exists(lab)]


def _entry_key(*, label: str = "", service: str = "", account: str = "") -> str:
    if label.strip():
        return label.strip()
    svc = service.strip()
    acct = account.strip()
    if svc and acct:
        return f"{svc} ({acct})"
    return svc or acct or "unknown"


def build_labels_registry() -> dict[str, Any]:
    """Merge .secret catalog + login Keychain scan into one label map."""
    catalog_rows = [r for r in load_catalog().get("keys") or [] if isinstance(r, dict)]
    keychain_items = filter_user_secrets(scan_login_keychain())

    labels: dict[str, dict[str, Any]] = {}
    catalog_label_keys: set[str] = set()

    for row in catalog_rows:
        primary = str(row.get("label") or "").strip()
        if not primary:
            continue
        matched = _catalog_keys_matched(row)
        available = bool(matched)
        status: SecretStatus = "available" if available else "missing"
        key = primary
        catalog_label_keys.add(primary)
        for alias in row.get("aliases") or []:
            catalog_label_keys.add(str(alias).strip())
        labels[key] = {
            "key": key,
            "label": primary,
            "aliases": [str(a) for a in (row.get("aliases") or []) if str(a).strip()],
            "available": available,
            "status": status,
            "sources": ["catalog"],
            "catalog_id": row.get("id"),
            "platform": row.get("platform"),
            "env": row.get("env"),
            "service": row.get("service"),
            "account": row.get("account"),
            "role": row.get("role"),
            "matched_in_keychain": matched,
        }

    for item in keychain_items:
        primary_label = str(item.get("label") or "").strip()
        service = str(item.get("service") or "").strip()
        account = str(item.get("account") or "").strip()
        if primary_label and primary_label in catalog_label_keys:
            continue
        if primary_label:
            skip = False
            for entry in labels.values():
                if primary_label in [entry.get("label"), *(entry.get("aliases") or [])]:
                    skip = True
                    break
            if skip:
                continue
        key = _entry_key(label=primary_label, service=service, account=account)
        if key in labels:
            labels[key]["available"] = True
            labels[key]["status"] = "available"
            if "keychain" not in labels[key].get("sources", []):
                labels[key].setdefault("sources", []).append("keychain")
            continue
        labels[key] = {
            "key": key,
            "label": primary_label,
            "display": item.get("display") or key,
            "available": True,
            "status": "available",
            "sources": ["keychain"],
            "kind": item.get("kind"),
            "service": service,
            "account": account,
        }

    available = sorted(k for k, v in labels.items() if v.get("available"))
    missing = sorted(k for k, v in labels.items() if not v.get("available"))

    return {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "path": str(labels_tracker_path()),
        "secrets_dir": str(catalog_path()),
        "summary": {
            "total": len(labels),
            "available_count": len(available),
            "missing_count": len(missing),
        },
        "available": available,
        "missing": missing,
        "labels": dict(sorted(labels.items(), key=lambda kv: kv[0].lower())),
    }


def save_labels_tracker(data: dict[str, Any] | None = None) -> Path:
    _migrate_legacy_labels_file()
    payload = data if data is not None else build_labels_registry()
    p = labels_tracker_path()
    p.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(p)
    os.chmod(p, 0o600)
    return p


def refresh_labels_tracker() -> dict[str, Any]:
    data = build_labels_registry()
    save_labels_tracker(data)
    return data


def load_labels_tracker(*, refresh: bool = False) -> dict[str, Any]:
    _migrate_legacy_labels_file()
    if refresh or not labels_tracker_path().exists():
        return refresh_labels_tracker()
    try:
        data = json.loads(labels_tracker_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and "labels" in data:
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return refresh_labels_tracker()
