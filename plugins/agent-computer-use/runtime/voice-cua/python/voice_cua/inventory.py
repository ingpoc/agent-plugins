"""Label-indexed secret inventory — catalog metadata + Keychain availability."""

from __future__ import annotations

from typing import Any, Literal

from voice_cua.catalog import (
    catalog_path,
    find_key,
    keychain_exists,
    load_catalog,
)

SecretStatus = Literal["available", "missing"]


def _row_status(label: str) -> SecretStatus:
    return "available" if keychain_exists(label) else "missing"


def describe_entry(row: dict[str, Any]) -> dict[str, Any]:
    """One catalog row with label-centric availability (no secret values)."""
    label = str(row.get("label") or "").strip()
    status: SecretStatus = _row_status(label) if label else "missing"
    return {
        "id": row.get("id"),
        "label": label,
        "platform": row.get("platform"),
        "env": row.get("env"),
        "service": row.get("service"),
        "account": row.get("account"),
        "status": status,
        "available": status == "available",
        "in_keychain": status == "available",
        "inject": row.get("inject") if isinstance(row.get("inject"), list) else [],
    }


def build_inventory(
    *,
    platform: str = "",
    query: str = "",
    available_only: bool = False,
) -> dict[str, Any]:
    """Full label inventory from catalog + live Keychain checks."""
    cat = load_catalog()
    platform_l = platform.lower().strip()
    query_l = query.lower().strip()
    entries: list[dict[str, Any]] = []
    by_label: dict[str, dict[str, Any]] = {}

    for row in cat.get("keys") or []:
        if not isinstance(row, dict):
            continue
        item = describe_entry(row)
        label = item["label"]
        if platform_l and platform_l not in str(item.get("platform") or "").lower():
            continue
        if query_l:
            blob = " ".join(
                str(item.get(k) or "") for k in ("id", "label", "platform", "env", "service", "account")
            ).lower()
            if query_l not in blob:
                continue
        if available_only and not item["available"]:
            continue
        entries.append(item)
        if label:
            by_label[label] = item

    available_labels = [e["label"] for e in entries if e["available"] and e["label"]]
    missing_labels = [e["label"] for e in entries if not e["available"] and e["label"]]

    return {
        "catalog_path": str(catalog_path()),
        "secrets_dir": str(catalog_path()),
        "total": len(entries),
        "available_count": len(available_labels),
        "missing_count": len(missing_labels),
        "available_labels": available_labels,
        "missing_labels": missing_labels,
        "by_label": by_label,
        "keys": entries,
    }


def find_by_label(label: str) -> dict[str, Any] | None:
    """Resolve catalog entry + availability by Keychain label."""
    needle = label.strip()
    if not needle:
        return None
    for row in load_catalog().get("keys") or []:
        if isinstance(row, dict) and str(row.get("label") or "").strip() == needle:
            return describe_entry(row)
    return None


def find_id_by_label(label: str) -> str | None:
    item = find_by_label(label)
    return str(item["id"]) if item and item.get("id") else None


def resolve_label(*, id: str | None = None, label: str | None = None) -> str | None:
    """Catalog id or label → canonical Keychain label."""
    if label and str(label).strip():
        return str(label).strip()
    if id:
        row = find_key(load_catalog(), str(id))
        if row:
            return str(row.get("label") or "").strip() or None
    return None
