# Runtime dependencies are supplied by macos-cua.py.
# ruff: noqa: F821
"""Read-only `doctor`: is this CUAService instance worth driving?

One owner for service health, TCC grants and operator identity. Never
launches, activates, clicks or restarts anything; never calls the retired
cua-driver.
"""
from __future__ import annotations


def _doctor_permissions():
    pid, window_id, _, error = resolve_app("Finder", launch_if_missing=False)
    probe = {} if error else (cua_snapshot(pid, window_id, 1) or {})
    perms = {
        "accessibility": probe.get("ax_trusted"),
        "screen_recording": probe.get("screen_capture"),
    }
    ok = all(value is True for value in perms.values())
    return {"ok": ok, **perms, **({"error": error} if error else {})}


def _doctor_operator():
    status = _operator_ui().status()
    signing = (status.get("signing") or {}).get("mode")
    service = status.get("service") or {}
    ok = status.get("ok") is True and signing == "identity"
    return {
        "ok": ok,
        "signing": signing,
        "service_installed": bool(service.get("installed")),
        "service_running": bool(service.get("running")),
        "pid": status.get("pid"),
    }


def doctor():
    """Return {"ok", "failed", "checks"}; ok only when every check is True."""
    checks = {}
    health = cuaservice_health() or {}
    checks["cuaservice"] = {"ok": health.get("ok") is True, **{
        key: health[key] for key in ("error", "recovery") if key in health
    }}
    for name, probe in (("permissions", _doctor_permissions), ("operator", _doctor_operator)):
        if not checks["cuaservice"]["ok"] and name == "permissions":
            checks[name] = {"ok": False, "error": "skipped: CUAService not healthy"}
            continue
        try:
            checks[name] = probe()
        except Exception as exc:  # doctor reports, never raises
            checks[name] = {"ok": False, "error": str(exc)}
    failed = [name for name, check in checks.items() if not check["ok"]]
    return {"ok": not failed, "failed": failed, "checks": checks}
