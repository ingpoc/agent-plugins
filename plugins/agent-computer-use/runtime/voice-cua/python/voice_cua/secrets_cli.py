#!/usr/bin/env python3
"""CLI for catalog + Keychain (metadata in JSON, values in Keychain)."""

from __future__ import annotations

import argparse
import getpass
import json
import sys

from voice_cua.catalog import (
    catalog_path,
    delete_key_file,
    find_key,
    init_secrets_from_bundle,
    keychain_delete,
    keychain_exists,
    keychain_get,
    keychain_put,
    load_catalog,
    save_key,
    upsert_key,
)
from voice_cua.inject import clear_clipboard, set_clipboard
from voice_cua.inventory import build_inventory, find_by_label
from voice_cua.labels_tracker import labels_tracker_path, load_labels_tracker, refresh_labels_tracker
from voice_cua.keychain_scan import scan_login_keychain


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="voice-cua-secrets")
    parser.add_argument("--secrets-dir", help="Override ~/.config/voice-cua/.secret")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("path", help="Print secrets directory (~/.config/voice-cua/.secret)")

    sub.add_parser("init", help="Install bundled config/.secret/*.json (skip existing)")

    sub.add_parser("labels", help="Print config/.secret/labels.json (refresh if stale)")

    p_list = sub.add_parser("list", help="List catalog entries with label availability")
    p_list.add_argument("--platform")
    p_list.add_argument("-q", "--query", help="Filter Keychain by label/service/account")
    p_list.add_argument("--keychain", action="store_true", help="List login Keychain (default for list)")

    p_status = sub.add_parser(
        "status",
        help="Label inventory: which secrets are available under which Keychain label",
    )
    p_status.add_argument("--platform")
    p_status.add_argument("-q", "--query")
    p_status.add_argument("--available-only", action="store_true")
    p_status.add_argument(
        "--labels-only",
        action="store_true",
        help="Print only available_labels and missing_labels arrays",
    )

    p_label = sub.add_parser("label", help="Look up one Keychain label in the catalog")
    p_label.add_argument("label")

    p_get = sub.add_parser("get", help="Verify Keychain presence (no value print by default)")
    p_get.add_argument("id")
    p_get.add_argument("--print-value", action="store_true", help="Print value to stdout (dangerous)")

    p_put = sub.add_parser("put", help="Upsert .secret JSON + Keychain value")
    p_put.add_argument("id")
    p_put.add_argument("--label")
    p_put.add_argument("--service")
    p_put.add_argument("--account")
    p_put.add_argument("--platform", default="generic")
    p_put.add_argument("--env", default="local")
    p_put.add_argument("--role", help="Runtime role (e.g. openai-runtime)")
    p_put.add_argument("--from-file", help="Read metadata from JSON file (config/.secret/<id>.json)")
    p_put.add_argument("--stdin-value", action="store_true", help="Read secret from stdin")
    p_put.add_argument("--prompt", action="store_true", help="Prompt for secret (default if no stdin)")

    p_mirror = sub.add_parser(
        "mirror",
        help="After Copy Password in Passwords app, store value in login Keychain",
    )
    p_mirror.add_argument("id", nargs="?", default="openai-api")

    p_clip = sub.add_parser("clipboard", help="Copy secret to clipboard briefly")
    p_clip.add_argument("id")
    p_clip.add_argument("--seconds", type=float, default=20.0)

    sub.add_parser(
        "mirror-openai-clipboard",
        help="Alias for: mirror openai-api",
    )

    p_del = sub.add_parser("delete", help="Remove catalog row and Keychain item")
    p_del.add_argument("id")
    p_del.add_argument("--yes", action="store_true")

    p_verify = sub.add_parser("verify", help="Verify all catalog labels exist in Keychain")

    p_scan = sub.add_parser(
        "scan",
        help="Refresh config/.secret/labels.json (catalog + Keychain availability)",
    )
    p_scan.add_argument("--no-save", action="store_true", help="Print only, do not write labels.json")

    p_present = sub.add_parser("present", help="Show labels.json (refresh first)")
    p_present.add_argument("--no-refresh", action="store_true", help="Read file without rescanning Keychain")

    args = parser.parse_args(argv)
    if getattr(args, "secrets_dir", None):
        import os

        os.environ["VOICE_CUA_SECRETS_DIR"] = args.secrets_dir

    if args.cmd == "path":
        print(catalog_path())
        return 0

    if args.cmd == "init":
        installed = init_secrets_from_bundle()
        tracker = refresh_labels_tracker()
        print(
            json.dumps(
                {
                    "ok": True,
                    "secrets_dir": str(catalog_path()),
                    "installed": installed,
                    "labels_file": str(labels_tracker_path()),
                    "summary": tracker["summary"],
                },
                indent=2,
            )
        )
        return 0

    if args.cmd == "labels":
        if labels_tracker_path().exists():
            print(labels_tracker_path().read_text(encoding="utf-8"))
        else:
            print(json.dumps(refresh_labels_tracker(), indent=2))
        return 0

    if args.cmd == "list":
        from voice_cua.keychain_access import enrich_items, list_keychain

        if getattr(args, "platform", None) and not getattr(args, "keychain", False):
            inv = build_inventory(
                platform=args.platform or "",
                query=args.query or "",
            )
            print(json.dumps({"keys": inv["keys"]}, indent=2))
        else:
            items = enrich_items(list_keychain(query=args.query or ""))
            print(json.dumps({"source": "keychain", "count": len(items), "keys": items}, indent=2))
        return 0

    if args.cmd == "status":
        from typing import Any

        tracker = refresh_labels_tracker()
        if args.labels_only:
            print(
                json.dumps(
                    {"available": tracker["available"], "missing": tracker["missing"]},
                    indent=2,
                )
            )
        else:
            # Filter for CLI flags
            labels = tracker["labels"]
            if args.platform or args.query or args.available_only:
                filtered: dict[str, Any] = {}
                platform_l = (args.platform or "").lower()
                query_l = (args.query or "").lower()
                for key, row in labels.items():
                    if platform_l and platform_l not in str(row.get("platform") or "").lower():
                        continue
                    if query_l:
                        blob = json.dumps(row).lower()
                        if query_l not in blob and query_l not in key.lower():
                            continue
                    if args.available_only and not row.get("available"):
                        continue
                    filtered[key] = row
                out = {**tracker, "labels": filtered, "summary": {
                    "total": len(filtered),
                    "available_count": sum(1 for r in filtered.values() if r.get("available")),
                    "missing_count": sum(1 for r in filtered.values() if not r.get("available")),
                }}
                out["available"] = sorted(k for k, r in filtered.items() if r.get("available"))
                out["missing"] = sorted(k for k, r in filtered.items() if not r.get("available"))
                print(json.dumps(out, indent=2))
            else:
                print(json.dumps(tracker, indent=2))
        return 0

    if args.cmd == "label":
        item = find_by_label(args.label)
        if not item:
            print(json.dumps({"ok": False, "error": "unknown label", "label": args.label}), file=sys.stderr)
            return 1
        print(json.dumps({"ok": True, **item}, indent=2))
        return 0

    if args.cmd == "get":
        row = find_key(load_catalog(), args.id)
        if not row:
            print(json.dumps({"ok": False, "error": "unknown id"}), file=sys.stderr)
            return 1
        label = str(row["label"])
        present = keychain_exists(label)
        out = {"ok": present, "id": args.id, "label": label, "in_keychain": present}
        if args.print_value and present:
            out["value"] = keychain_get(label)
        print(json.dumps(out, indent=2))
        return 0 if present else 2

    if args.cmd == "put":
        from typing import Any

        meta: dict[str, Any] = {"id": args.id}
        if args.from_file:
            from pathlib import Path

            data = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                meta.update({k: str(v) for k, v in data.items() if k != "inject" and k != "aliases"})
                if "inject" in data:
                    meta["inject"] = data["inject"]
                if "aliases" in data:
                    meta["aliases"] = data["aliases"]
        for field in ("label", "service", "account", "platform", "env", "role"):
            val = getattr(args, field, None)
            if val:
                meta[field] = val
        for req in ("label", "service", "account"):
            if not str(meta.get(req) or "").strip():
                print(
                    json.dumps({"ok": False, "error": f"missing {req} — use --from-file or flags"}),
                    file=sys.stderr,
                )
                return 1
        cat = load_catalog()
        row = upsert_key(cat, meta)
        value = None
        if args.stdin_value:
            value = sys.stdin.read().rstrip("\n")
        elif args.prompt or not args.stdin_value:
            value = getpass.getpass(f"Secret for {row['label']}: ")
        keychain_put(
            service=row["service"],
            account=row["account"],
            label=row["label"],
            value=value,
        )
        refresh_labels_tracker()
        print(json.dumps({"ok": True, "id": row["id"], "label": row["label"], "path": str(catalog_path() / f"{row['id']}.json")}, indent=2))
        return 0

    if args.cmd == "mirror":
        from voice_cua.auth import mirror_secret_from_clipboard

        result = mirror_secret_from_clipboard(args.id)
        refresh_labels_tracker()
        print(json.dumps(result, indent=2))
        return 0

    if args.cmd == "clipboard":
        import time

        row = find_key(load_catalog(), args.id)
        if not row:
            print("unknown id", file=sys.stderr)
            return 1
        secret = keychain_get(str(row["label"]))
        try:
            set_clipboard(secret)
            print(json.dumps({"ok": True, "copied": True, "seconds": args.seconds, "label": row["label"]}))
            time.sleep(max(0.0, args.seconds))
        finally:
            secret = ""
            clear_clipboard()
        return 0

    if args.cmd == "mirror-openai-clipboard":
        from voice_cua.auth import mirror_secret_from_clipboard

        print(json.dumps(mirror_secret_from_clipboard("openai-api"), indent=2))
        return 0

    if args.cmd == "delete":
        if not args.yes:
            print("pass --yes to delete", file=sys.stderr)
            return 1
        cat = load_catalog()
        row = find_key(cat, args.id)
        if not row:
            print("unknown id", file=sys.stderr)
            return 1
        for label in [str(row["label"]), *(row.get("aliases") or [])]:
            keychain_delete(label)
        delete_key_file(args.id)
        refresh_labels_tracker()
        print(json.dumps({"ok": True, "deleted": args.id}))
        return 0

    if args.cmd == "verify":
        tracker = refresh_labels_tracker()
        ok_all = tracker["summary"]["missing_count"] == 0
        report = [
            {
                "key": k,
                "label": v.get("label") or k,
                "status": v.get("status"),
                "available": v.get("available"),
                "catalog_id": v.get("catalog_id"),
            }
            for k, v in tracker["labels"].items()
            if "catalog" in (v.get("sources") or [])
        ]
        print(
            json.dumps(
                {
                    "ok": ok_all,
                    "labels_file": str(labels_tracker_path()),
                    "available": tracker["available"],
                    "missing": tracker["missing"],
                    "catalog": report,
                },
                indent=2,
            )
        )
        return 0 if ok_all else 2

    if args.cmd == "scan":
        from voice_cua.labels_tracker import build_labels_registry

        data = build_labels_registry()
        if not args.no_save:
            from voice_cua.labels_tracker import save_labels_tracker

            path = save_labels_tracker(data)
            data["saved_to"] = str(path)
        print(json.dumps(data, indent=2))
        return 0

    if args.cmd == "present":
        if args.no_refresh and labels_tracker_path().exists():
            print(labels_tracker_path().read_text(encoding="utf-8"))
            return 0
        print(json.dumps(refresh_labels_tracker(), indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
