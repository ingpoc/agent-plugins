#!/usr/bin/env python3
"""Build, sign, and install Agent Computer Use.app (bundle id com.ingpoc.cua-service, binary CUAService).

Usage:
    python3 install_service.py [--release [--sign-only]] [--team TEAM_ID] [--dest PATH]

Installs to /Applications/Agent Computer Use.app and keeps ~/.cache/macos-cua/CUAService.app as a
symlink to it, so existing callers (cua_client, operator_ui, `open ~/.cache/...`) keep working.
--release builds a staged copy, runs release/sign-notarize.sh on it (Developer ID, hardened runtime,
notarize + staple), then installs the signed copy. Signing prefers Developer ID over Apple Development:
its designated requirement is team-based, so TCC grants survive certificate renewals.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SERVICE_DIR.parents[2]
VOICE_RUNTIME_ROOT = PLUGIN_ROOT / "runtime" / "voice-cua"
SWIFT_BUILD_ROOT = Path(
    os.environ.get("MACOS_CUA_BUILD_DIR", "~/.cache/macos-cua/build/cua-service")
).expanduser()
INSTALL_DIR = Path("/Applications/Agent Computer Use.app")
LEGACY_DIR = Path("~/.cache/macos-cua/CUAService.app").expanduser()
RELEASE_STAGE = Path("~/.cache/macos-cua/release-stage").expanduser()
SIGN_NOTARIZE = SERVICE_DIR / "release" / "sign-notarize.sh"
TEAM_ID = "9UPQL479Z5"
VOICE_CUA_ID = "com.ingpoc.cua-service.voice-cua"
ENTITLEMENTS = SERVICE_DIR / "Resources" / "CUAService.entitlements"
VOICE_ENTITLEMENTS = SERVICE_DIR / "Resources" / "voice-cua.entitlements"
MIC_ENTITLEMENTS = SERVICE_DIR / "Resources" / "mic-preflight.entitlements"
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
    candidates: list[tuple[str, str]] = []
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
            candidates.append((sha1, details))
    picked = pick_identity(candidates)
    if picked:
        return picked
    raise RuntimeError(
        f'No valid code-signing identity for Apple Developer Team {team}. '
        "Install or refresh its Apple Development certificate in Xcode Settings > Accounts."
    )


def pick_identity(candidates: list[tuple[str, str]]) -> str | None:
    """Developer ID Application first (team-based designated requirement: TCC grants survive
    renewals), else the first team identity (Apple Development: DR pins the leaf CN)."""
    for sha1, subject in candidates:
        if "CN=Developer ID Application" in subject:
            return sha1
    return candidates[0][0] if candidates else None


def ensure_compat_link(dest: Path, legacy: Path = LEGACY_DIR, trash: Path | None = None) -> str:
    """Keep the old install path working as a symlink to the new app.
    A real bundle left at the old path is moved to the Trash (never deleted, never overwritten)."""
    dest = dest.resolve() if dest.exists() else dest
    if dest == legacy:
        return "same-path"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    if legacy.is_symlink():
        if Path(os.readlink(legacy)) == dest:
            return "kept"
        legacy.unlink()
    elif legacy.exists():
        bin_ = (trash or Path.home() / ".Trash") / f"CUAService.app.legacy-{int(time.time())}"
        shutil.move(str(legacy), str(bin_))
    legacy.symlink_to(dest)
    return "linked"


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
    codesign(helper_app, identity, team, entitlements=VOICE_ENTITLEMENTS)
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
        if RELEASE_STAGE in dest.parents:
            shutil.rmtree(dest)   # scratch staging copy
        else:   # an installed app is superseded: Trash, never delete
            shutil.move(str(dest), str(Path.home() / ".Trash" / f"{dest.name}.prev-{int(time.time())}"))
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    # Copy binary
    shutil.copy2(binary, macos / "CUAService")
    (macos / "CUAService").chmod(0o755)

    copy_brand(resources)

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
    stamp_version(contents / "Info.plist")
    print(f"  ✓ App bundle created at {dest}")


PLUGIN_JSON = PLUGIN_ROOT / "plugin.json"


def plugin_version() -> str:
    """The single source of truth for the app version (About, Finder, notarization)."""
    return json.loads(PLUGIN_JSON.read_text())["version"]


def stamp_version(info_plist: Path) -> None:
    """CFBundleShortVersionString = plugin.json version; CFBundleVersion = commit count (build)."""
    with info_plist.open("rb") as f:
        info = plistlib.load(f)
    info["CFBundleShortVersionString"] = plugin_version()
    try:
        build = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=SERVICE_DIR,
                               capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        build = ""
    info["CFBundleVersion"] = build or plugin_version()
    with info_plist.open("wb") as f:
        plistlib.dump(info, f)


BRAND_DIR = SERVICE_DIR.parent / "assets" / "brand"
BRAND_FILES = ("AppIcon.icns", "StatusIcon.pdf", "StatusIcon.png", "StatusIcon@2x.png", "StatusIcon@3x.png", "mark.svg")


def copy_brand(resources: Path) -> None:
    """"Island Cursor" brand: AppIcon.icns (CFBundleIconFile) + template status glyph + mark."""
    for name in BRAND_FILES:
        src = BRAND_DIR / name
        if src.is_file():
            shutil.copy2(src, resources / name)


MEDIAREMOTE_SRC = SERVICE_DIR / "Vendor" / "mediaremote-adapter"


def bundle_mediaremote_adapter(app_path: Path) -> bool:
    """Now Playing: vendored mediaremote-adapter (BSD-3). Framework → Contents/Frameworks, test client →
    Contents/Helpers, perl script → Contents/Resources. Skips (island falls back to AppleScript) if cmake
    is missing or the build fails."""
    cmake = shutil.which("cmake")
    if not MEDIAREMOTE_SRC.is_dir() or not cmake:
        print("  ! mediaremote-adapter skipped (no vendored source or cmake); Now Playing uses AppleScript fallback")
        return False
    build_dir = Path.home() / ".cache" / "macos-cua" / "build" / "mediaremote-adapter"
    build_dir.mkdir(parents=True, exist_ok=True)
    try:
        run([cmake, "-S", str(MEDIAREMOTE_SRC), "-B", str(build_dir), "-DCMAKE_BUILD_TYPE=Release"],
            stdout=subprocess.DEVNULL)
        run([cmake, "--build", str(build_dir), "--config", "Release"], stdout=subprocess.DEVNULL)
    except subprocess.CalledProcessError as exc:
        print(f"  ! mediaremote-adapter build failed ({exc}); Now Playing uses AppleScript fallback")
        return False
    fw_src = build_dir / "MediaRemoteAdapter.framework"
    client_src = build_dir / "MediaRemoteAdapterTestClient"
    script_src = MEDIAREMOTE_SRC / "bin" / "mediaremote-adapter.pl"
    if not (fw_src.is_dir() and client_src.is_file() and script_src.is_file()):
        print("  ! mediaremote-adapter artefacts missing; Now Playing uses AppleScript fallback")
        return False
    contents = app_path / "Contents"
    fw_dst = contents / "Frameworks" / "MediaRemoteAdapter.framework"
    if fw_dst.exists():
        shutil.rmtree(fw_dst)
    fw_dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(fw_src, fw_dst, symlinks=True)
    helpers = contents / "Helpers"
    helpers.mkdir(parents=True, exist_ok=True)
    shutil.copy2(client_src, helpers / "MediaRemoteAdapterTestClient")
    (helpers / "MediaRemoteAdapterTestClient").chmod(0o755)
    (contents / "Resources").mkdir(parents=True, exist_ok=True)
    shutil.copy2(script_src, contents / "Resources" / "mediaremote-adapter.pl")
    shutil.copy2(MEDIAREMOTE_SRC / "LICENSE", contents / "Resources" / "mediaremote-adapter-LICENSE.txt")
    print("  ✓ mediaremote-adapter bundled (Now Playing)")
    return True


def codesign(app_path: Path, identity: str, team: str, entitlements: Path = ENTITLEMENTS) -> None:
    """Inside-out: helper (voice-cua.entitlements), frameworks, test client, mic-preflight
    (mic-preflight.entitlements), then the bundle with its own entitlements."""
    helper = app_path / "Contents" / "Helpers" / "voice-cua.app"
    if helper.is_dir():
        codesign(helper, identity, team, entitlements=VOICE_ENTITLEMENTS)
    frameworks = app_path / "Contents" / "Frameworks"
    if frameworks.is_dir():
        for nested in sorted(frameworks.rglob("*"), key=lambda path: len(path.parts), reverse=True):
            if nested.is_file() and not nested.is_symlink() and is_macho(nested):
                run(["codesign", "--force", "--sign", identity, "--options", "runtime", str(nested)])
        for fw in sorted(frameworks.glob("*.framework")):
            run(["codesign", "--force", "--sign", identity, "--options", "runtime", str(fw)])
    test_client = app_path / "Contents" / "Helpers" / "MediaRemoteAdapterTestClient"
    if test_client.is_file():
        run(["codesign", "--force", "--sign", identity, "--options", "runtime", str(test_client)])
    mic_preflight = app_path / "Contents" / "MacOS" / "mic-preflight"
    if mic_preflight.is_file():
        run([
            "codesign", "--force", "--sign", identity,
            "--options", "runtime", "--entitlements", str(MIC_ENTITLEMENTS),
            str(mic_preflight),
        ])
    run([
        "codesign", "--force", "--sign", identity,
        "--options", "runtime", "--entitlements", str(entitlements),
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
        "--release", action="store_true",
        help="Stage, sign + notarize with release/sign-notarize.sh (Developer ID), then install",
    )
    parser.add_argument(
        "--sign-only", action="store_true", help="With --release: Developer ID sign, skip notarization",
    )
    parser.add_argument(
        "--skip-voice-bundle",
        action="store_true",
        help="Do not bundle voice-cua helper",
    )
    args = parser.parse_args()

    dest = Path(args.dest)
    identity = "" if args.skip_sign or args.replace_binary or args.release else resolve_codesign_identity(args.team)
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
        copy_brand(resources)
        print(f"  ✓ Replaced {dest_bin} (unsigned copy; existing bundle signature may be invalid until you re-grant TCC if macOS rejects it)")
        print(f"\n✓ CUAService binary updated at {dest}")
        print("  Do not codesign --force --sign - ; that drops Accessibility TCC.")
        return

    # --release: assemble + sign a staged copy; the live app is only replaced once it is signed.
    target = RELEASE_STAGE / dest.name if args.release else dest
    print("Creating app bundle...")
    create_app_bundle(binary, target)

    if not args.skip_voice_bundle:
        bundle_voice_helper(target, team=args.team, skip_sign=True)
    bundle_mediaremote_adapter(target)

    if args.release:
        print("Release signing (Developer ID, hardened runtime)...")
        run(["bash", str(SIGN_NOTARIZE)] + (["--sign-only"] if args.sign_only else []) + [str(target)])
        if dest.exists():
            shutil.move(str(dest), str(Path.home() / ".Trash" / f"{dest.name}.prev-{int(time.time())}"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        run(["ditto", str(target), str(dest)])
    elif not args.skip_sign:
        print("Signing...")
        codesign(dest, identity, args.team)

    if Path(args.dest) == INSTALL_DIR:
        print(f"  ✓ Legacy path {LEGACY_DIR}: {ensure_compat_link(dest)}")
    print(f"\n✓ Agent Computer Use installed at {dest}")
    print(f"  Socket: ~/.cache/macos-cua/cua-service.sock")
    print(f"  Voice: Samantha toggle in menu (logs ~/.cache/macos-cua/voice.log)")
    print(f"  Run: open {dest}")


if __name__ == "__main__":
    main()
