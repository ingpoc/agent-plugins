# Runtime dependencies are supplied by macos-cua.py.
# ruff: noqa: F821
"""Read-only `doctor`: is this CUAService instance worth driving?

Reports service health and TCC grants. Never launches, activates, clicks or
restarts anything; never calls the retired cua-driver.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_LEGACY = Path(__file__).resolve().parent / "legacy_operator_migration.py"
_spec = importlib.util.spec_from_file_location("legacy_operator_migration", _LEGACY)
_legacy = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_legacy)
retire_legacy_operator = _legacy.retire_legacy_operator


def _doctor_permissions():
    pid, window_id, _, error = resolve_app("Finder", launch_if_missing=False)
    probe = {} if error else (cua_snapshot(pid, window_id, 1) or {})
    perms = {
        "accessibility": probe.get("ax_trusted"),
        "screen_recording": probe.get("screen_capture"),
    }
    ok = all(value is True for value in perms.values())
    return {"ok": ok, **perms, **({"error": error} if error else {})}


def _doctor_legacy_operator():
    retirement = retire_legacy_operator()
    return {
        "ok": retirement.get("ok") is True,
        "already_retired": retirement.get("already_retired"),
        "unloaded": retirement.get("unloaded"),
        "trashed": retirement.get("trashed"),
        "required": False,
        **({"reason": retirement["reason"]} if retirement.get("reason") else {}),
    }


def doctor():
    """Return {"ok", "failed", "checks"}; ok only when every required check is True."""
    checks = {}
    health = cuaservice_health() or {}
    checks["cuaservice"] = {"ok": health.get("ok") is True, **{
        key: health[key] for key in ("socket", "error", "recovery") if key in health
    }}
    for name, probe in (
        ("permissions", _doctor_permissions),
        ("legacy_operator", _doctor_legacy_operator),
    ):
        if not checks["cuaservice"]["ok"] and name == "permissions":
            checks[name] = {"ok": False, "error": "skipped: CUAService not healthy"}
            continue
        try:
            checks[name] = probe()
        except Exception as exc:  # doctor reports, never raises
            checks[name] = {"ok": False, "error": str(exc)}
    if "legacy_operator" in checks:
        checks["legacy_operator"]["required"] = False
    failed = [
        name
        for name, check in checks.items()
        if check.get("required", True) and not check["ok"]
    ]
    return {"ok": not failed, "failed": failed, "checks": checks}
