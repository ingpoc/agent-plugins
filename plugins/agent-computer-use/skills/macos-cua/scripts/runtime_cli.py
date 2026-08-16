# Runtime dependencies are injected by the compatibility facade.
# ruff: noqa: F401, F821
"""Session lifecycle, cursor configuration, and CLI dispatch.

Loaded behind the stable macos-cua compatibility facade.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

def ensure_session(session: str | None = None) -> dict:
    """Revive an explicit legacy cua-driver cursor session."""
    sid = session or CUA_SESSION
    return call_driver("start_session", {"session": sid}, timeout=10)


def configure_cursor_icon(
    session: str | None = None, *, icon_path: str | None = None
) -> dict:
    """Configure an explicit legacy driver cursor; primary clicks do not call this."""
    sid = session or CUA_SESSION
    source_path = icon_path or CURSOR_ICON
    path = cursor_raster_path(source_path)
    use_arrow = os.environ.get("MACOS_CUA_CURSOR_ARROW", "0") == "1"
    cursor_label = os.environ.get(
        "MACOS_CUA_CURSOR_LABEL", f"macos-cua · {_operator_ui().detect_harness()}"
    )
    ensure_session(sid)
    style = {"raw": "skipped-svg-arrow-default"}
    if not use_arrow and os.path.isfile(path):
        style = call_driver(
            "set_agent_cursor_style",
            {"session": sid, "cursor_id": sid, "image_path": path},
            timeout=10,
        )
    motion = call_driver(
        "set_agent_cursor_motion",
        {
            "session": sid,
            "cursor_id": sid,
            **({} if use_arrow else {"cursor_icon": path}),
            "cursor_size": int(os.environ.get("MACOS_CUA_CURSOR_SIZE", "64")),
            "cursor_opacity": 1.0,
            "cursor_color": os.environ.get("MACOS_CUA_CURSOR_COLOR", "#00FFFF"),
            "cursor_label": cursor_label,
            "glide_duration_ms": int(os.environ.get("MACOS_CUA_GLIDE_MS", "300")),
            "dwell_after_click_ms": int(os.environ.get("MACOS_CUA_DWELL_MS", "300")),
            "idle_hide_ms": 0,
        },
        timeout=10,
    )
    ok = "error" not in motion and (use_arrow or "error" not in style)
    return {
        "ok": ok,
        "icon": path,
        "source_icon": source_path,
        "using_generic_arrow": use_arrow,
        "style": style,
        "motion": motion,
    }


def cursor(action, **kwargs):
    global _CURSOR_CONFIGURED
    session = kwargs.get("session", CUA_SESSION)
    ensure_session(session)
    if action == "status":
        sess = call_driver("get_agent_cursor_state", {"session": session})
        return sess
    if action == "configure":
        configured = configure_cursor_icon(session, icon_path=kwargs.get("icon"))
        _CURSOR_CONFIGURED = True
        return configured
    if action == "show":
        if (
            _CURSOR_CONFIGURED
            and os.environ.get("MACOS_CUA_FORCE_CURSOR_CONFIG") != "1"
        ):
            configured = {"ok": True, "skipped": "already_configured"}
        else:
            configured = configure_cursor_icon(session, icon_path=kwargs.get("icon"))
            _CURSOR_CONFIGURED = True
        enabled = call_driver(
            "set_agent_cursor_enabled",
            {"enabled": True, "session": session},
        )
        return {"configured": configured, "enabled": enabled}
    if action == "hide":
        return call_driver(
            "set_agent_cursor_enabled", {"enabled": False, "session": session}
        )
    if action == "move":
        return call_driver(
            "move_cursor", {"x": kwargs["x"], "y": kwargs["y"], "session": session}
        )
    raise ValueError(f"unknown cursor action: {action}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_snap(result, max_lines=40):
    tree = result.get("tree_markdown", "")
    count = result.get("element_count", 0)
    print(f"Elements: {count}")
    for line in tree.splitlines()[:max_lines]:
        print(line)
    if count > max_lines:
        print(f"... ({count - max_lines} more elements)")


def _emit_json(result, *, require_ok=False, require_accepted=False):
    return _cli_parser().emit_json(
        result,
        accepted=_accepted,
        require_ok=require_ok,
        require_accepted=require_accepted,
    )


def _main():
    parser = _cli_parser().build_parser(
        cua_session=CUA_SESSION,
        key_codes=KEY_CODES,
    )
    args = parser.parse_args()

    if hasattr(args, "app"):
        visual = _acquire_visual_focus(
            f"macos-cua:{args.command}:{str(args.app)[:80]}"
        )
        if not visual.get("ok"):
            _emit_json({"ok": False, **visual}, require_ok=True)
    elif args.command == "click-desktop":
        visual = _acquire_visual_focus("macos-cua:click-desktop")
        if not visual.get("ok"):
            _emit_json({"ok": False, **visual}, require_ok=True)

    if args.command == "status":
        print(json.dumps(driver_status(), indent=2, default=str))
        return
    if args.command == "reset":
        removed = clear_resolution_cache()
        print(json.dumps({"ok": True, "cleared": removed}))
        return
    if args.command == "apps":
        payload = list_apps()
        if isinstance(payload, dict) and (
            getattr(args, "query", None) or getattr(args, "running", False)
        ):
            payload = _cli_parser().filter_apps(
                payload,
                query=getattr(args, "query", None),
                running=bool(getattr(args, "running", False)),
            )
        _emit_json(payload)
        return
    if args.command == "operator":
        controller = _operator_ui()
        if args.action == "start":
            result = controller.ensure()
        elif args.action == "stop":
            result = controller.stop()
        elif args.action == "install-service":
            result = controller.install_service()
        elif args.action == "uninstall-service":
            result = controller.uninstall_service()
        elif args.action == "signing-status":
            result = controller.signing_status()
        elif args.action == "show-pip":
            result = controller.set_pip_visible(True)
        elif args.action == "hide-pip":
            result = controller.set_pip_visible(False)
        else:
            result = controller.status()
        _emit_json(result, require_ok=True)
        return
    if args.command == "displays":
        disp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "displays.py"
        )
        spec = importlib.util.spec_from_file_location("displays", disp_path)
        disp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(disp)
        print(json.dumps(disp.display_packet(), indent=2))
        return
    if args.command == "ensure-display":
        pid, _wid, _name, err = resolve_app(
            args.app,
            launch_if_missing=False,
            activate_if_inactive=False,
        )
        if err:
            _emit_json({"ok": False, "error": err}, require_ok=True)
        _enforce_browser_coexistence(pid, args)
        disp_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "displays.py"
        )
        spec = importlib.util.spec_from_file_location("displays", disp_path)
        disp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(disp)
        clear_resolution_cache()
        res = disp.ensure_on_test_display(
            args.app,
            args.display,
            width=args.width,
            height=args.height,
            margin=args.margin,
        )
        print(json.dumps(res, indent=2, default=str))
        sys.exit(0 if res.get("ok") else 1)
    if args.command == "cursor":
        kw = {"session": args.session}
        if args.icon:
            kw["icon"] = args.icon
        if args.action == "move":
            if args.x is None or args.y is None:
                print(json.dumps({"error": "move requires --x and --y"}))
                sys.exit(1)
            kw.update(x=args.x, y=args.y)
        print(json.dumps(cursor(args.action, **kw), indent=2, default=str))
        return

    if args.command == "click-desktop":
        _emit_json(
            click_at_desktop(
                args.x,
                args.y,
                button=args.button,
                click_count=args.count,
                preserve_pointer=args.preserve_pointer,
            ),
            require_accepted=True,
        )
        return

    if args.command == "focus":
        identity = _running_app_identity(args.app)
        if identity:
            _enforce_browser_coexistence(identity["pid"], args)
        pid, wid, name, err = resolve_app(
            args.app,
            activate_if_inactive=False,
        )
        if err:
            print(json.dumps({"error": err}))
            sys.exit(1)
        if not identity or identity["pid"] != pid:
            _enforce_browser_coexistence(pid, args)
        foreground = bring_resolved_window_to_front(pid, wid)
        if foreground.get("error"):
            _emit_json({"ok": False, "error": foreground["error"]}, require_ok=True)
        print(json.dumps({"ok": True, "pid": pid, "window_id": wid, "name": name}))
        return

    # All other commands need a resolved window.
    identity = _running_app_identity(args.app)
    authorized_pid = None
    if identity:
        _enforce_browser_coexistence(identity["pid"], args)
        authorized_pid = identity["pid"]

    # Background AX/targeted delivery is the default. Individual coordinate,
    # key, and capture commands opt into foregrounding explicitly.
    activate_if_inactive = False
    pid, wid, name, err = resolve_app(
        args.app,
        activate_if_inactive=False,
    )
    if err:
        # Last-resort: try focus then resolve.
        launch_or_activate(args.app)
        time.sleep(1)
        pid, wid, name, err = resolve_app(
            args.app,
            launch_if_missing=False,
            activate_if_inactive=False,
        )
    if err:
        print(json.dumps({"error": f"Cannot resolve '{args.app}': {err}"}))
        sys.exit(1)
    if authorized_pid != pid:
        _enforce_browser_coexistence(pid, args)

    skip_frame = os.environ.get("MACOS_CUA_NO_FRAME") == "1"
    if not skip_frame and getattr(args, "frame", None):
        frame_app_window(args.app)
        clear_resolution_cache()
        time.sleep(0.6)

    target_identity = _running_app_identity(f"pid:{pid}")
    foreground_prepared = False
    if (
        activate_if_inactive
        and target_identity
        and target_identity.get("active") is False
    ):
        foreground = bring_resolved_window_to_front(pid, wid)
        if foreground.get("error"):
            print(json.dumps({"error": foreground["error"]}))
            sys.exit(1)
        foreground_prepared = True

    if (
        activate_if_inactive
        and args.command in _cli_parser().AX_FOREGROUND_COMMANDS
        and not foreground_prepared
    ):
        foreground = bring_resolved_window_to_front(pid, wid)
        if foreground.get("error"):
            print(json.dumps({"error": foreground["error"]}))
            sys.exit(1)
        foreground_prepared = True
    if args.command == "click-point":
        try:
            wid = _validated_window_override(pid, wid, args.window_id)
        except ValueError as exc:
            print(json.dumps({"error": str(exc)}))
            sys.exit(1)

    if args.command not in _cli_parser().OBSERVING_COMMANDS:
        operator_update(
            name or args.app,
            pid,
            wid,
            status="acting",
            active=True,
            message=f"{args.command} in progress",
        )

    if args.command == "snap":
        operator_update(
            name or args.app,
            pid,
            wid,
            status="observing",
            active=True,
            message="Accessibility state",
        )
        _print_snap(snapshot(pid, wid, args.max_elements, args.mode))
    elif args.command == "state":
        result = app_state(
            name or args.app,
            pid,
            wid,
            max_elements=args.max_elements,
            query=args.query,
            include_screenshot=not args.no_screenshot,
            screenshot_out_file=args.screenshot,
            prepare_foreground=bool(args.foreground),
            foreground_prepared=foreground_prepared,
        )
        if args.compact and result.get("ok"):
            result = {
                key: result[key]
                for key in (
                    "ok", "app", "pid", "window_id", "snapshot_id", "text",
                    "element_count", "hidden_element_count", "screenshot",
                    "capture_error",
                )
                if key in result
            }
        if result.get("ok"):
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from state_diff import apply as apply_diff
            result.update(apply_diff(result.get("app") or args.app, result.get("pid"), getattr(args, "query", None), result.get("text") or "", enabled=bool(getattr(args, "diff", False))))
        _emit_json(result, require_ok=True)
    elif args.command == "click":
        _emit_json(
            click(pid, wid, args.element, app_name=name or args.app),
            require_accepted=True,
        )
    elif args.command == "click-point":
        _emit_json(
            click_point(
                pid,
                wid,
                args.x,
                args.y,
                button=args.button,
                click_count=args.count,
                delivery_mode="foreground" if args.foreground else "background",
                debug_image_out=args.debug_image,
                logical_frame_recovery=args.window_id is None,
                preserve_pointer=args.preserve_pointer,
                app_name=name or args.app,
            ),
            require_accepted=True,
        )
    elif args.command == "double-click":
        if args.element is None and (args.x is None or args.y is None):
            parser.error("double-click requires --element N or both --x and --y")
        _emit_json(
            double_click(
                pid,
                wid,
                element_index=args.element,
                x=args.x,
                y=args.y,
                delivery_mode="foreground" if args.foreground else "background",
                app_name=name or args.app,
            ),
            require_accepted=True,
        )
    elif args.command == "perform-action":
        fresh = snapshot(pid, wid, max_elements=200)
        element = args.element
        if element is None:
            element = find_clickable_index(fresh, args.label)
        pre = pointer_preflight(
            True, name or args.app, pid, wid, fresh, element,
            f"Moving to {args.label or element}",
        )
        if element is None:
            outcome = {"error": "element not found"}
        elif pre and not pre.get("ok"):
            outcome = pre
        else:
            outcome = merge_pointer_proof(
                perform_action(pid, wid, element, args.action, snapshot_data=fresh),
                pre,
            )
        _emit_json(outcome)
    elif args.command == "drag":
        _emit_json(
            drag(
                pid,
                wid,
                args.from_x,
                args.from_y,
                args.to_x,
                args.to_y,
                delivery_mode="foreground" if args.foreground else "background",
                duration_ms=args.duration_ms,
                steps=args.steps,
                app_name=name or args.app,
            )
        )
    elif args.command == "type-text":
        _emit_json(
            type_text(
                pid,
                wid,
                args.element,
                args.text,
                x=args.x,
                y=args.y,
                delivery_mode="foreground" if args.foreground else "background",
            )
        )
    elif args.command == "key":
        if args.foreground and args.system_events:
            parser.error("key accepts only one of --foreground or --system-events")
        mode = "system_events" if args.system_events else (
            "foreground" if args.foreground else "background"
        )
        print(json.dumps(press_key(pid, wid, args.keys, mode), indent=2, default=str))
    elif args.command == "hold-key":
        _emit_json(
            hold_key(
                pid,
                args.key,
                args.duration,
                window_id=wid,
                foreground=args.foreground,
            ),
            require_ok=True,
        )
    elif args.command == "scroll":
        _emit_json(
            scroll(
                pid,
                wid,
                args.direction,
                args.amount,
                by=args.by,
                element_index=args.element,
                x=args.x,
                y=args.y,
                delivery_mode="foreground" if args.foreground else "background",
            )
        )
    elif args.command == "right-click":
        _emit_json(right_click(pid, wid, args.element, app_name=name or args.app))
    elif args.command == "set-value":
        _emit_json(set_value(pid, wid, args.element, args.value))
    elif args.command == "select-text":
        fresh = snapshot(pid, wid, max_elements=500)
        element = args.element
        if element is None:
            element = find_field_index(fresh, args.label)
        _emit_json(
            select_text_action(
                pid,
                fresh,
                element,
                args.text,
                prefix=args.prefix,
                suffix=args.suffix,
                selection_type=args.selection_type,
            )
            if element is not None
            else {"ok": False, "error": "field not found"},
            require_ok=True,
        )
    elif args.command == "find":
        snap = snapshot(pid, wid, args.max_elements)
        idx = find_clickable_index(snap, args.text)
        print(json.dumps({"found": idx is not None, "element": idx, "text": args.text}))
    elif args.command in ("click-label", "click-label-pointer"):
        result = click_label_pointer(
            pid,
            wid,
            args.label,
            args.max_elements,
            app_name=name or args.app,
        )
        _emit_json(result, require_ok=True)
    elif args.command == "type-label":
        _emit_json(
            type_label_action(
                pid,
                wid,
                args.label,
                args.text,
                args.max_elements,
                app_name=name or args.app,
            ),
            require_accepted=True,
        )
    elif args.command == "list-buttons":
        snap = _native_ax_snapshot(pid, max_elements=args.max_elements, window_id=wid)
        if snapshot_content_error(snap):
            snap = snapshot(pid, wid, args.max_elements)
        if snapshot_content_error(snap):
            snap = _native_ax_snapshot_after_activation(pid, args.max_elements)
        if snapshot_content_error(snap):
            snap = _vision_snapshot_after_activation(pid, wid, args.max_elements)
        if not emit_list_buttons(snap):
            sys.exit(1)
    elif args.command == "run":
        js = args.json
        if js.startswith("@"):
            with open(js[1:]) as f:
                js = f.read()
        plan = json.loads(js)
        _emit_json(
            run_actions(
                pid,
                wid,
                plan,
                app_name=name or args.app,
                foreground_prepared=foreground_prepared,
            ),
            require_ok=True,
        )


def main():
    """Run one CLI command while holding any managed-Chrome claim to exit."""
    failure = None
    traceback = None
    try:
        _main()
    except BaseException as exc:  # release must cover SystemExit and interrupts
        failure = exc
        traceback = exc.__traceback__
    claim_release = _release_browser_coexistence_claim()
    visual_release = _release_visual_focus()
    release_failures = [
        packet
        for packet in (claim_release, visual_release)
        if packet.get("safe", packet.get("ok")) is not True
    ]
    for packet in release_failures:
        print(json.dumps(packet, separators=(",", ":")), file=sys.stderr)
    if release_failures:
        if failure is None:
            return 1
    if failure is not None:
        raise failure.with_traceback(traceback)
    return 0
