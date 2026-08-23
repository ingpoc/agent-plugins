"""Scan login Keychain for present secrets (metadata only — never values)."""

from __future__ import annotations

import re
import subprocess

SECURITY = "/usr/bin/security"

# Skip obvious system / Wi‑Fi / crypto noise when listing "user secrets"
_SKIP_SERVICE_PREFIXES = (
    "com.apple.",
    "AirPort",
    "Bluetooth",
    "MetadataKeychain",
    "Safari Forms",
    "AirPlay",
    "Apple ",
    "AppleID",
    "WiFiAnalytics",
    "FMFDStoreController",
)


def scan_login_keychain() -> list[dict[str, str]]:
    """Return generic + internet password metadata from login keychain."""
    r = subprocess.run(
        [SECURITY, "dump-keychain"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "dump-keychain failed")

    blocks = re.split(r"\nclass:", r.stdout)
    items: list[dict[str, str]] = []
    for block in blocks:
        head = block.split("\n", 1)[0]
        if "genp" not in head and "inet" not in head:
            continue
        kind = "generic" if "genp" in head else "internet"
        acct = re.search(r'"acct"<blob>="([^"]*)"', block)
        svce = re.search(r'"svce"<blob>="([^"]*)"', block)
        labl = re.search(r'"labl"<blob>="([^"]*)"', block)
        service = svce.group(1) if svce else ""
        account = acct.group(1) if acct else ""
        label = labl.group(1) if labl else ""
        display = label or service or account
        if not display.strip():
            continue
        items.append({
            "kind": kind,
            "label": label,
            "service": service,
            "account": account,
            "display": display,
        })
    return items


def _is_user_secret(row: dict[str, str]) -> bool:
    service = row.get("service") or ""
    label = row.get("label") or ""
    display = row.get("display") or ""
    blob = f"{service} {label} {display}".lower()
    if " safe storage" in blob:
        return False
    if any(service.startswith(p) for p in _SKIP_SERVICE_PREFIXES):
        return False
    return bool(service or label)


def filter_user_secrets(items: list[dict[str, str]]) -> list[dict[str, str]]:
    return [x for x in items if _is_user_secret(x)]
