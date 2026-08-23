#!/usr/bin/env python3
"""Build, sign, and install CUAService.app.

Usage:
    python3 install_service.py [--release] [--team TEAM_ID]
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SERVICE_DIR.parents[2]
VOICE_RUNTIME_ROOT = PLUGIN_ROOT / "runtime" / "voice-cua"
SWIFT_BUILD_ROOT = Path(
    os.environ.get("MACOS_CUA_BUILD_DIR", "~/.cache/macos-cua/build/cua-service")
).expanduser()
INSTALL_DIR = Path("~/.cache/macos-cua/CUAService.app").expanduser()
TEAM_ID = "9UPQL479Z5"
VOICE_CUA_ID = "com.ingpoc.cua-service.voice-cua"
ENTITLEMENTS = SERVICE_DIR / "Resources" / "CUAService.entitlements"
MACHO_MAGICS = {b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"}


def is_macho(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(4) in MACHO_MAGICS
    except OSError:
        return False


def resolve_codesign_identity(team: str) -> str:
    """Return a valid signing identity SHA-1 whose certificate OU matches team."""
    identities = subprocess.run(
        ["security", "find-identity", "-v", "-p", "codesigning"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    valid = set(re.findall(r"\b[0-9A-F]{40}\b", identities))
    certificates = subprocess.run(
        ["security", "find-certificate", "-a", "-p"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    for pem in re.findall(
        r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
        certificates,
        re.DOTALL,
    ):
        details = subprocess.run(
            ["/usr/bin/openssl", "x509", "-noout", "-fingerprint", "-sha1", "-subject", "-nameopt", "RFC2253"],
            input=pem,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        fingerprint = re.search(r"Fingerprint=([0-9A-F:]+)", details)
        sha1 = fingerprint.group(1).replace(":", "") if fingerprint else ""
        if sha1 in valid and re.search(rf"(?:^|,)OU={re.escape(team)}(?:,|$)", details):
            return sha1
    raise RuntimeError(
        f'No valid code-signing identity for Apple Developer Team {team}. '
        "Install or refresh its Apple Development certificate in Xcode Settings > Accounts."
    )


def build_mic_preflight_tool(helper_macos: Path) -> None:
    """Compile AVFoundation mic TCC helper beside voice-cua binary."""
    src = SERVICE_DIR / "Resources" / "mic_preflight.swift"
    dest = helper_macos / "mic-preflight"
    if not src.is_file():
        return
    try:
        run([
            "swiftc",
            "-O",
            "-o",
            str(dest),
            str(src),
            "-framework",
            "AVFoundation",
            "-framework",
            "AppKit",
        ])
        dest.chmod(0o755)
        print("  ✓ Built mic-preflight TCC helper")
    except subprocess.CalledProcessError:
        print("  ⚠ mic-preflight build failed — mic TCC may not prompt until rebuilt")


def build_voice_helper_binary(voice_root: Path, dest: Path) -> None:
    """Build the packaged Voice CUA runtime or fail the installation."""
    script = voice_root / "scripts" / "build_voice_helper.py"
    if not script.is_file():
        raise FileNotFoundError(f"packaged Voice CUA builder missing: {script}")
    run([sys.executable, str(script), "--output", str(dest)])
    if not dest.is_file() or not dest.stat().st_mode & 0o111:
        raise RuntimeError(f"Voice CUA helper build did not produce an executable: {dest}")


def bundle_voice_helper(app_path: Path, *, team: str, skip_sign: bool) -> None:
    """Install voice-cua helper under Contents/Helpers/voice-cua.app."""
    voice_root = VOICE_RUNTIME_ROOT
    if not (voice_root / "python" / "voice_cua" / "voice_stack.py").is_file():
        raise FileNotFoundError(f"packaged Voice CUA runtime missing: {voice_root}")

    helper_app = app_path / "Contents" / "Helpers" / "voice-cua.app"
    helper_macos = helper_app / "Contents" / "MacOS"
    helper_macos.mkdir(parents=True, exist_ok=True)
    helper_bin = helper_macos / "voice-cua"

    build_voice_helper_binary(voice_root, helper_bin)

    build_mic_preflight_tool(helper_macos)

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
    identity = resolve_codesign_identity(team)
    codesign(helper_app, identity, team)
    print(f"  ✓ Signed voice helper for team {team}")


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"  → {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, **kwargs)


def build(release: bool = True) -> Path:
    config = "release" if release else "debug"
    run(
        ["swift", "build", "--scratch-path", str(SWIFT_BUILD_ROOT), "-c", config],
        cwd=SERVICE_DIR,
    )
    build_dir = SWIFT_BUILD_ROOT / config
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


def codesign(app_path: Path, identity: str, team: str) -> None:
    helper = app_path / "Contents" / "Helpers" / "voice-cua.app"
    if helper.is_dir():
        codesign(helper, identity, team)
    frameworks = app_path / "Contents" / "Frameworks"
    if frameworks.is_dir():
        for nested in sorted(frameworks.rglob("*"), key=lambda path: len(path.parts), reverse=True):
            if nested.is_file() and is_macho(nested):
                run(["codesign", "--force", "--sign", identity, "--options", "runtime", str(nested)])
    mic_preflight = app_path / "Contents" / "MacOS" / "mic-preflight"
    if mic_preflight.is_file():
        run([
            "codesign", "--force", "--sign", identity,
            "--options", "runtime", "--entitlements", str(ENTITLEMENTS),
            str(mic_preflight),
        ])
    run([
        "codesign", "--force", "--sign", identity,
        "--options", "runtime", "--entitlements", str(ENTITLEMENTS),
        str(app_path),
    ])
    print(f"  ✓ Signed for team {team}")


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
    identity = "" if args.skip_sign or args.replace_binary else resolve_codesign_identity(args.team)
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
        codesign(dest, identity, args.team)

    print(f"\n✓ CUAService installed at {dest}")
    print(f"  Socket: ~/.cache/macos-cua/cua-service.sock")
    print(f"  Voice: Samantha toggle in menu (logs ~/.cache/macos-cua/voice.log)")
    print(f"  Run: open {dest}")


if __name__ == "__main__":
    main()
