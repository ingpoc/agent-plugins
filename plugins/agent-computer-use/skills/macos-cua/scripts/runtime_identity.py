# Runtime dependencies are injected by the compatibility facade.
# ruff: noqa: F401, F821
"""Which apps are live: the instance registry and its identity lookups.

A short-lived CLI process holds a stale ``NSWorkspace`` snapshot: an app
launched seconds ago is missing from it, and a killed pid can linger. The live
CUAService registry is therefore the authority for "is it running, and which
pid", and NSWorkspace only contributes what it still knows.
"""
from __future__ import annotations

import re


def _is_app_extension(app):
    """True for app extensions and XPC services (``.appex``/``.xpc`` bundles).

    WhatsApp's ``ServiceExtension.appex`` runs on its own with
    ``localizedName`` "WhatsApp". Treating it as the app made a cold WhatsApp
    resolve to the extension, and activating it failed with
    ``LSCopyApplicationURLsForBundleIdentifier() failed``. Extensions are never
    a UI target; the regular ``.app`` is.
    """
    try:
        url = app.bundleURL()
        path = str(url.path() if url is not None else "")
    except Exception:
        path = ""
    path = path.rstrip("/").lower()
    return path.endswith(".appex") or path.endswith(".xpc")


def _running_app_identity(app_name):
    """Resolve a running app by bundle/name before inspecting window titles.

    Window titles can contain unrelated app names (for example a Notification
    Center widget titled "Codex"). This process's NSWorkspace snapshot answers
    fast and decides the ranking; the live registry is the fallback for the app
    it missed because that app launched a moment ago.
    """
    try:
        from AppKit import NSWorkspace
    except ImportError:
        return None
    needle = app_name.strip().lower()
    pid_match = re.fullmatch(r"pid:(\d+)", needle)
    requested_pid = int(pid_match.group(1)) if pid_match else None
    ranked = []
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        pid = int(app.processIdentifier())
        name = str(app.localizedName() or "")
        bundle_id = str(app.bundleIdentifier() or "")
        if requested_pid is None and _is_app_extension(app):
            continue
        if requested_pid is not None:
            if pid != requested_pid:
                continue
            return {
                "pid": pid,
                "name": name,
                "bundle_id": bundle_id,
                "active": bool(app.isActive()),
            }
        name_lower = name.lower()
        bundle_lower = bundle_id.lower()
        bundle_leaf = bundle_lower.rsplit(".", 1)[-1]
        if needle == bundle_lower:
            score = 5
        elif needle == name_lower:
            score = 4
        elif needle == bundle_leaf:
            score = 3
        elif needle and needle in name_lower:
            score = 2
        elif needle and needle in bundle_lower:
            score = 1
        else:
            continue
        ranked.append(
            (
                score,
                {
                    "pid": pid,
                    "name": name,
                    "bundle_id": bundle_id,
                    "active": bool(app.isActive()),
                },
            )
        )
    # Equal-name multi-instance apps are deterministic: active first, then the
    # newest/highest PID. Exact callers should still prefer pid:<number>.
    if ranked:
        return max(
            ranked,
            key=lambda item: (item[0], bool(item[1]["active"]), item[1]["pid"]),
        )[1]
    # An app launched seconds ago can be missing from this process's NSWorkspace
    # snapshot. The live registry still knows its pid and bundle, so a fresh
    # launch stays targetable instead of failing as "no running app with pid".
    for instance in _live_instances():
        if requested_pid is not None:
            if instance["pid"] == requested_pid:
                return dict(instance)
            continue
        if needle in {
            instance["name"].lower(),
            instance["bundle_id"].lower(),
            instance["bundle_id"].lower().rsplit(".", 1)[-1],
        }:
            return dict(instance)
    return None


def _live_instances():
    """Every running app, deduped by PID, from the live registry first.

    ``NSWorkspace.runningApplications`` is a per-process snapshot that goes
    stale inside a short-lived CLI process: an app launched seconds ago is
    missing from it, and a killed pid can linger in it. CUAService enumerates
    the live registry, so it is the authority for "is it running, and which
    pid"; NSWorkspace only contributes what it still knows (accessory apps,
    or everything at all when CUAService is unavailable).
    """
    found: dict[int, dict] = {}

    def record(pid, name, bundle_id, active=False):
        pid = int(pid or 0)
        if pid and _pid_alive(pid):
            found.setdefault(
                pid,
                {
                    "pid": pid,
                    "name": str(name or ""),
                    "bundle_id": str(bundle_id or ""),
                    "active": bool(active),
                },
            )

    try:
        listed = list_apps()
    except Exception:
        listed = {}
    if isinstance(listed, dict):
        for entry in listed.get("apps") or []:
            record(
                entry.get("pid"),
                entry.get("name"),
                entry.get("bundle_id"),
                entry.get("active"),
            )
    try:
        from AppKit import NSWorkspace
    except ImportError:
        return list(found.values())
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if _is_app_extension(app):
            continue
        record(
            app.processIdentifier(),
            app.localizedName(),
            app.bundleIdentifier(),
            app.isActive(),
        )
    return list(found.values())


def _live_app_instances(app_name):
    """Every live instance of an app, deduped by PID.

    ``_running_app_identity`` returns a single ranked match, so a second copy
    (or a just-launched one) stays invisible. Anything that has to be certain
    no instance is left behind -- reaping a fixture, or deciding between
    activating and forcing a cold launch -- needs the complete set.
    """
    instances = _live_instances()
    needle = str(app_name or "").strip().lower()
    if not needle:
        return instances
    # Exact identity only: this set decides what gets terminated, and a
    # substring match ("Code" vs "Codex") would kill an unrelated app.
    return [
        instance
        for instance in instances
        if needle
        in {
            instance["name"].lower(),
            instance["bundle_id"].lower(),
            instance["bundle_id"].lower().rsplit(".", 1)[-1],
        }
    ]
