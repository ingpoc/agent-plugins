"""Resolve runtime secrets from Keychain via .secret JSON — never from chat."""

from __future__ import annotations

import os
from typing import Any

from voice_cua.catalog import (
    catalog_path,
    find_by_role,
    find_key,
    init_secrets_from_bundle,
    keychain_exists,
    keychain_get,
    keychain_put,
    labels_for_row,
    load_catalog,
)
from voice_cua.inject import clear_clipboard

OPENAI_RUNTIME_ROLE = "openai-runtime"


def _openai_row() -> dict[str, Any]:
    init_secrets_from_bundle()
    cat = load_catalog()
    row = find_by_role(cat, OPENAI_RUNTIME_ROLE)
    if row:
        return row
    row = find_key(cat, "openai-api")
    if row:
        return row
    raise RuntimeError(
        f"no openai-runtime secret in {catalog_path()}/ — run: secrets_cli init"
    )


def _first_keychain_label(row: dict[str, Any]) -> str | None:
    for label in labels_for_row(row):
        if keychain_exists(label):
            return label
    return None


def resolve_openai_api_key() -> str:
    """Env override first, else login Keychain via labels from .secret JSON."""
    env = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if env:
        return env
    row = _openai_row()
    label = _first_keychain_label(row)
    if not label:
        primary = str(row["label"])
        raise RuntimeError(
            f"{primary!r} is in Passwords but not yet in login Keychain "
            "(security CLI cannot read Passwords-app vault directly).\n"
            "Mirror once (no paste into chat):\n"
            f"  1) In Passwords, open {primary!r} → Copy Password\n"
            f"  2) secrets_cli mirror {row['id']}\n"
            "Or let the voice/CUA agent open Passwords and copy for you."
        )
    return keychain_get(label)


def mirror_secret_from_clipboard(key_id: str | None = None) -> dict[str, Any]:
    """Read clipboard (after user/CUA copied from Passwords) → login Keychain."""
    import subprocess

    init_secrets_from_bundle()
    cat = load_catalog()
    if key_id:
        row = find_key(cat, key_id)
    else:
        row = _openai_row()
    if not row:
        raise RuntimeError(f"unknown secret id {key_id!r}")

    raw = subprocess.check_output(["pbpaste"], text=True)
    secret = (raw or "").strip()
    if not secret:
        raise RuntimeError("clipboard empty — copy the password from Passwords first")
    if len(secret) < 16:
        raise RuntimeError("clipboard does not look like an API key")
    label = str(row["label"])
    try:
        keychain_put(
            service=str(row["service"]),
            account=str(row["account"]),
            label=label,
            value=secret,
        )
    finally:
        secret = ""
        clear_clipboard()
    return {
        "ok": True,
        "id": row["id"],
        "label": label,
        "mirrored": True,
    }


def mirror_openai_from_clipboard() -> dict[str, Any]:
    """Backward-compatible wrapper for openai-api mirror."""
    return mirror_secret_from_clipboard("openai-api")


def openai_configured() -> bool:
    if (os.environ.get("OPENAI_API_KEY") or "").strip():
        return True
    try:
        row = _openai_row()
    except RuntimeError:
        return False
    return _first_keychain_label(row) is not None


def openai_status() -> dict[str, Any]:
    try:
        row = _openai_row()
    except RuntimeError as exc:
        return {
            "source": "env" if (os.environ.get("OPENAI_API_KEY") or "").strip() else "keychain",
            "configured": bool((os.environ.get("OPENAI_API_KEY") or "").strip()),
            "catalog_id": None,
            "label": None,
            "resolved_label": None,
            "secrets_dir": str(catalog_path()),
            "in_keychain": False,
            "error": str(exc),
        }
    resolved = _first_keychain_label(row)
    primary = str(row["label"])
    return {
        "source": "env" if (os.environ.get("OPENAI_API_KEY") or "").strip() else "keychain",
        "catalog_id": row["id"],
        "label": primary,
        "resolved_label": resolved if openai_configured() else None,
        "secrets_dir": str(catalog_path()),
        "configured": openai_configured(),
        "in_keychain": resolved is not None,
        "aliases": row.get("aliases") or [],
        "passwords_app_note": (
            "Passwords app items need a one-time mirror into login Keychain "
            "for security CLI / agents"
        ),
    }
