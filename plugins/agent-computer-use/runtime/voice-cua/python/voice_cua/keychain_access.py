"""Resolve and fetch login Keychain items — metadata in tools, values only for inject/clipboard."""

from __future__ import annotations

import subprocess
from typing import Any

from voice_cua.catalog import find_key, keychain_exists, keychain_get, labels_for_row, load_catalog
from voice_cua.keychain_scan import filter_user_secrets, scan_login_keychain

SECURITY = "/usr/bin/security"


def _item_key(item: dict[str, str]) -> str:
    if item.get("label"):
        return f"label:{item['label']}"
    return f"{item.get('kind', 'generic')}:{item.get('service', '')}:{item.get('account', '')}"


def list_keychain(
    *,
    query: str = "",
    user_only: bool = True,
    limit: int = 80,
) -> list[dict[str, str]]:
    items = scan_login_keychain()
    if user_only:
        items = filter_user_secrets(items)
    q = query.lower().strip()
    if q:
        items = [
            x
            for x in items
            if q
            in " ".join(
                str(x.get(k) or "") for k in ("label", "service", "account", "display", "kind")
            ).lower()
        ]
    items.sort(key=lambda x: (x.get("label") or x.get("service") or x.get("account") or "").lower())
    return items[:limit]


def search_keychain(query: str, *, limit: int = 20) -> list[dict[str, str]]:
    return list_keychain(query=query, user_only=True, limit=limit)


def _catalog_overlay() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in load_catalog().get("keys") or []:
        if not isinstance(row, dict):
            continue
        for lab in labels_for_row(row):
            out[lab] = row
        svc = str(row.get("service") or "")
        if svc:
            out[f"service:{svc}"] = row
    return out


def enrich_items(items: list[dict[str, str]]) -> list[dict[str, Any]]:
    overlay = _catalog_overlay()
    enriched: list[dict[str, Any]] = []
    for item in items:
        row = overlay.get(item.get("label") or "") or overlay.get(f"service:{item.get('service', '')}")
        entry: dict[str, Any] = {
            **item,
            "available": True,
            "status": "available",
            "in_keychain": True,
        }
        if row:
            entry["catalog_id"] = row.get("id")
            entry["platform"] = row.get("platform")
        enriched.append(entry)
    return enriched


def resolve_ref(
    *,
    id: str = "",
    label: str = "",
    service: str = "",
    account: str = "",
    query: str = "",
) -> tuple[dict[str, str] | None, str | None]:
    """Return (keychain item metadata, error). Never includes secret value."""
    key_id = id.strip()
    if key_id:
        row = find_key(load_catalog(), key_id)
        if row:
            primary = str(row["label"])
            if keychain_exists(primary):
                return {
                    "kind": "generic",
                    "label": primary,
                    "service": str(row.get("service") or ""),
                    "account": str(row.get("account") or ""),
                    "display": primary,
                    "catalog_id": key_id,
                }, None
            for alias in labels_for_row(row):
                if alias != primary and keychain_exists(alias):
                    return {
                        "kind": "generic",
                        "label": alias,
                        "service": str(row.get("service") or ""),
                        "account": str(row.get("account") or ""),
                        "display": alias,
                        "catalog_id": key_id,
                    }, None
            return None, f"catalog id {key_id} registered but not in login Keychain (mirror from Passwords?)"

    lab = label.strip()
    if lab:
        if keychain_exists(lab):
            matches = [x for x in scan_login_keychain() if x.get("label") == lab]
            base = matches[0] if matches else {"kind": "generic", "label": lab, "service": "", "account": "", "display": lab}
            return dict(base), None
        return None, f"Keychain miss for label {lab!r}"

    svc = service.strip()
    acct = account.strip()
    if svc:
        item, err = _match_service_account(svc, acct)
        if item:
            return item, None
        return None, err or f"Keychain miss for service={svc!r}"

    q = query.strip()
    if q:
        hits = search_keychain(q, limit=10)
        if not hits:
            return None, f"no Keychain item matches {q!r}"
        if len(hits) > 1:
            names = [h.get("label") or h.get("service") or h.get("account") for h in hits[:5]]
            return None, f"ambiguous query {q!r} — be specific: {names}"
        return hits[0], None

    return None, "need label, service, catalog id, or query"


def _match_service_account(service: str, account: str) -> tuple[dict[str, str] | None, str | None]:
    for kind, flag in (("generic", "find-generic-password"), ("internet", "find-internet-password")):
        cmd = [SECURITY, flag, "-s", service]
        if account:
            cmd.extend(["-a", account])
        cmd.append("-w")
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode == 0:
            scanned = [x for x in scan_login_keychain() if x.get("service") == service and (not account or x.get("account") == account)]
            if scanned:
                return scanned[0], None
            return {
                "kind": kind,
                "label": "",
                "service": service,
                "account": account,
                "display": service if not account else f"{service} ({account})",
            }, None
    if not account:
        scanned = [x for x in filter_user_secrets(scan_login_keychain()) if x.get("service") == service]
        if len(scanned) == 1:
            return scanned[0], None
        if len(scanned) > 1:
            return None, f"multiple Keychain rows for service {service!r} — pass account"
    return None, None


def fetch_value(item: dict[str, str]) -> str:
    """Read secret value from login Keychain. Caller must clear clipboard / never log."""
    label = str(item.get("label") or "").strip()
    if label:
        return keychain_get(label)
    service = str(item.get("service") or "").strip()
    account = str(item.get("account") or "").strip()
    if not service:
        raise RuntimeError("cannot fetch Keychain item without label or service")
    kind = item.get("kind", "generic")
    flag = "find-internet-password" if kind == "internet" else "find-generic-password"
    cmd = [SECURITY, flag, "-s", service, "-w"]
    if account:
        cmd = [SECURITY, flag, "-s", service, "-a", account, "-w"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Keychain fetch failed for {item.get('display') or service!r}")
    return r.stdout.rstrip("\n")


def describe_ref(item: dict[str, str]) -> dict[str, Any]:
    display = item.get("label") or item.get("display") or item.get("service") or "keychain-item"
    out: dict[str, Any] = {
        "ok": True,
        "label": item.get("label") or "",
        "service": item.get("service") or "",
        "account": item.get("account") or "",
        "display": display,
        "kind": item.get("kind") or "generic",
        "available": True,
        "status": "available",
        "in_keychain": True,
    }
    if item.get("catalog_id"):
        out["id"] = item["catalog_id"]
    return out
