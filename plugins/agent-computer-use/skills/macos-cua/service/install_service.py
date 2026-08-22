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
VOICE_CUA_ID = "com.ingpoc.cua-service.voice-cua"


def voice_cua_agent_root() -> Path | None:
    env = os.environ.get("VOICE_CUA_AGENT_ROOT")
    if env:
        root = Path(env).expanduser()
        if (root / "python" / "voice_cua" / "voice_stack.py").is_file():
            return root
    candidates = [
        SERVICE_DIR.parents[6] / "voice-cua-agent",
        Path("~/Documents/remote-claude/active/apps/voice-cua-agent").expanduser(),
    ]
    for root in candidates:
        if (root / "python" / "voice_cua" / "voice_stack.py").is_file():
            return root
    return None


def build_voice_helper_binary(voice_root: Path, dest: Path) -> bool:
    """Try PyInstaller; return True when dest is executable."""
    script = voice_root / "scripts" / "build_voice_helper.py"
    if not script.is_file():
        return False
    try:
        run([sys.executable, str(script), "--output", str(dest)])
        return dest.is_file() and os.access(dest, os.X_OK)
    except subprocess.CalledProcessError:
        print("  ⚠ PyInstaller voice-cua build failed — dev PYTHONPATH fallback remains")
        return False


def bundle_voice_helper(app_path: Path, *, team: str, skip_sign: bool) -> None:
    """Install voice-cua helper under Contents/Helpers/voice-cua.app."""
    voice_root = voice_cua_agent_root()
    if voice_root is None:
        print("  ⚠ voice-cua-agent repo not found — Voice menu uses dev PYTHONPATH only")
        return

    helper_app = app_path / "Contents" / "Helpers" / "voice-cua.app"
    helper_macos = helper_app / "Contents" / "MacOS"
    helper_macos.mkdir(parents=True, exist_ok=True)
    helper_bin = helper_macos / "voice-cua"

    if not build_voice_helper_binary(voice_root, helper_bin):
        stub = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            f'ROOT="{voice_root}"\n'
            'export PYTHONPATH="${ROOT}/python${PYTHONPATH:+:$PYTHONPATH}"\n'
            'export VOICE_CUA_PORT="${VOICE_CUA_PORT:-8765}"\n'
            'export VOICE_CUA_GATEWAY="${VOICE_CUA_GATEWAY:-http://127.0.0.1:${VOICE_CUA_PORT}}"\n'
            'unset VOICE_CUA_REMOTE_ISLAND\n'
            'exec /usr/bin/python3 -m voice_cua.voice_stack "$@"\n'
        )
        helper_bin.write_text(stub, encoding="utf-8")
        helper_bin.chmod(0o755)
        print("  ⚠ voice-cua stub uses system python3 — mic TCC still prompts for Python until PyInstaller build succeeds")

    plist_src = SERVICE_DIR / "Resources" / "voice-cua-Info.plist"
    if plist_src.is_file():
        shutil.copy2(plist_src, helper_app / "Contents" / "Info.plist")
    else:
        (helper_app / "Contents" / "Info.plist").write_text(
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<plist version="1.0"><dict>'
            f"<key>CFBundleIdentifier</key><string>{VOICE_CUA_ID}</string>"
            f"<key>CFBundleExecutable</key><string>voice-cua</string>"
            f"<key>NSMicrophoneUsageDescription</key>"
            f"<string>Voice CUA listens so you can speak tasks for the agent.</string>"
            f"</dict></plist>",
            encoding="utf-8",
        )

    if skip_sign:
        return
    identity = f"Apple Development: Team {team}"
    try:
        run([
            "codesign", "--force", "--sign", identity,
            "--options", "runtime",
            str(helper_app),
        ])
        print(f"  ✓ Signed voice helper with {identity}")
    except subprocess.CalledProcessError:
        run(["codesign", "--force", "--sign", "-", str(helper_app)])
        print("  ⚠ voice helper ad-hoc signed — grant Microphone to voice-cua helper in System Settings")


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

    # White menubar glyph (Agent Computer Use)
    assets = SERVICE_DIR.parent / "assets"
    for name in ("MenubarIcon.png", "MenubarIcon@2x.png", "MenubarIcon@3x.png"):
        src = assets / name
        if src.is_file():
            shutil.copy2(src, resources / name)

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
    parser.add_argument(
        "--skip-voice-bundle",
        action="store_true",
        help="Do not bundle voice-cua helper",
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
        resources = dest / "Contents" / "Resources"
        resources.mkdir(parents=True, exist_ok=True)
        assets = SERVICE_DIR.parent / "assets"
        for name in ("MenubarIcon.png", "MenubarIcon@2x.png", "MenubarIcon@3x.png"):
            src = assets / name
            if src.is_file():
                shutil.copy2(src, resources / name)
        print(f"  ✓ Replaced {dest_bin} (unsigned copy; existing bundle signature may be invalid until you re-grant TCC if macOS rejects it)")
        print(f"\n✓ CUAService binary updated at {dest}")
        print("  Do not codesign --force --sign - ; that drops Accessibility TCC.")
        return

    print("Creating app bundle...")
    create_app_bundle(binary, dest)

    if not args.skip_voice_bundle:
        bundle_voice_helper(dest, team=args.team, skip_sign=True)

    if not args.skip_sign:
        print("Signing...")
        codesign(dest, args.team)

    print(f"\n✓ CUAService installed at {dest}")
    print(f"  Socket: ~/.cache/macos-cua/cua-service.sock")
    print(f"  Voice: menu Voice ▶ / ⏹ (logs ~/.cache/macos-cua/voice.log)")
    print(f"  Run: open {dest}")


if __name__ == "__main__":
    main()
