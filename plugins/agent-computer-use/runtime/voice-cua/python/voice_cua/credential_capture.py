"""Metadata-only bridge to native Island entry; credential values never enter Python."""
from __future__ import annotations

import time

from voice_cua import session_hub
from voice_cua.catalog import _normalize_row, load_catalog, upsert_key
from voice_cua.cua_bridge import get_backend
from voice_cua.island_facade import island_publish

_FIELDS = frozenset({"id", "label", "service", "account", "platform", "env"})


def capture_credential(arguments: dict, *, timeout: float = 125.0) -> dict:
    if set(arguments) - _FIELDS:
        return {"ok": False, "error": "Use the Island's secure field; tool arguments accept metadata only."}
    try:
        # Validate before opening a native form; an omitted account is entered in the form.
        _normalize_row({**arguments, "account": arguments.get("account") or "pending-native-entry"})
    except (ValueError, TypeError):
        return {"ok": False, "error": "Credential id, label and service are required."}
    backend = get_backend()
    request_id = ""
    result = None
    try:
        with session_hub.credential_privacy():
            island_publish("secrets", title="Save credential", detail="Use the secure Island field")
            result = backend.credential("request", arguments)
            if not result.get("ok"):
                return result
            request_id = str(result.get("request_id") or "")
            if not request_id:
                return {"ok": False, "error": "Native credential request did not start."}
            deadline = time.monotonic() + timeout
            while result.get("status") == "pending" and time.monotonic() < deadline:
                time.sleep(0.15)
                result = backend.credential("status", {"request_id": request_id})
            if result.get("status") != "saved" or not result.get("stored"):
                return {"ok": False, "error": str(result.get("error") or "Credential entry timed out.")}
            row = {**arguments, **{key: result[key] for key in ("id", "label", "service", "account")}}
            try:
                upsert_key(load_catalog(), row)
            except (OSError, ValueError):
                # Keychain has the value already. A retry must repair metadata, not ask to overwrite it.
                return {"ok": False, "stored": True, "error": "Saved in Keychain; credential locator could not be recorded.",
                        **{key: row[key] for key in ("id", "label", "service", "account")}}
            island_publish("done", title="Saved to Keychain", detail=row["label"])
            return {"ok": True, "stored": True, **{key: row[key] for key in ("id", "label", "service", "account")}}
    finally:
        if request_id and (not result or result.get("status") not in {"saved", "cancelled", "expired"}):
            backend.credential("cancel", {"request_id": request_id})
