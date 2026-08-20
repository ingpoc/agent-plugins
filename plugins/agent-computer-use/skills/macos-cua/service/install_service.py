#!/usr/bin/env python3
"""Build, sign, and install CUAService.app.

Usage:
    python3 install_service.py [--release] [--team TEAM_ID]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent
INSTALL_DIR = Path("~/.cache/macos-cua/CUAService.app").expanduser()
TEAM_ID = "9UPQL479Z5"


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"  → {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, **kwargs)


def build(release: bool = True) -> Path:
    config = "release" if release else "debug"
    run(
        ["swift", "build", "-c", config],
        cwd=SERVICE_DIR,
    )
    build_dir = SERVICE_DIR / ".build" / config
    binary = build_dir / "CUAService"
    if not binary.exists():
        print(f"ERROR: Binary not found at {binary}", file=sys.stderr)
        sys.exit(1)
    return binary


def create_app_bundle(binary: Path, dest: Path) -> None:
    """Create a proper .app bundle from the binary."""
    contents = dest / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"

    if dest.exists():
        shutil.rmtree(dest)
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    # Copy binary
    shutil.copy2(binary, macos / "CUAService")
    (macos / "CUAService").chmod(0o755)

    # Copy Info.plist
    plist_src = SERVICE_DIR / "Resources" / "Info.plist"
    if plist_src.exists():
        shutil.copy2(plist_src, contents / "Info.plist")
    else:
        # Generate minimal plist
        (contents / "Info.plist").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>'
            "<key>CFBundleIdentifier</key><string>com.ingpoc.cua-service</string>"
            "<key>CFBundleName</key><string>CUAService</string>"
            "<key>CFBundleExecutable</key><string>CUAService</string>"
            "<key>LSUIElement</key><true/>"
            "</dict></plist>"
        )
    print(f"  ✓ App bundle created at {dest}")


def codesign(app_path: Path, team: str) -> None:
    identity = f"Apple Development: Team {team}"
    try:
        run([
            "codesign", "--deep", "--force", "--sign", identity,
            "--options", "runtime",
            str(app_path),
        ])
        print(f"  ✓ Signed with {identity}")
    except subprocess.CalledProcessError:
        # Fallback: ad-hoc sign
        print("  ⚠ Team signing failed, using ad-hoc")
        run(["codesign", "--deep", "--force", "--sign", "-", str(app_path)])


def main():
    parser = argparse.ArgumentParser(description="Build and install CUAService")
    parser.add_argument(
        "--debug", action="store_true", help="Build in debug mode"
    )
    parser.add_argument(
        "--team", default=TEAM_ID, help=f"Signing team ID (default: {TEAM_ID})"
    )
    parser.add_argument(
        "--dest", default=str(INSTALL_DIR),
        help=f"Install destination (default: {INSTALL_DIR})"
    )
    parser.add_argument(
        "--skip-sign", action="store_true", help="Skip code signing"
    )
    parser.add_argument(
        "--replace-binary",
        action="store_true",
        help="Copy into existing signed bundle; do not resign (keeps Accessibility TCC)",
    )
    args = parser.parse_args()

    dest = Path(args.dest)
    print(f"Building CUAService ({'debug' if args.debug else 'release'})...")
    binary = build(release=not args.debug)

    if args.replace_binary:
        dest_bin = dest / "Contents" / "MacOS" / "CUAService"
        if not dest_bin.exists():
            print(f"ERROR: missing {dest_bin}; run a full install first", file=sys.stderr)
            sys.exit(1)
        shutil.copy2(binary, dest_bin)
        dest_bin.chmod(0o755)
        print(f"  ✓ Replaced {dest_bin} (unsigned copy; existing bundle signature may be invalid until you re-grant TCC if macOS rejects it)")
        print(f"\n✓ CUAService binary updated at {dest}")
        print("  Do not codesign --force --sign - ; that drops Accessibility TCC.")
        return

    print("Creating app bundle...")
    create_app_bundle(binary, dest)

    if not args.skip_sign:
        print("Signing...")
        codesign(dest, args.team)

    print(f"\n✓ CUAService installed at {dest}")
    print(f"  Socket: ~/.cache/macos-cua/cua-service.sock")
    print(f"  Run: open {dest}")


if __name__ == "__main__":
    main()
