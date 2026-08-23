#!/usr/bin/env python3
"""Build the voice-cua helper for CUAService bundling."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "python"
CONFIG = ROOT / "config" / ".secret"
BUILD_ROOT = Path(
    os.environ.get("VOICE_CUA_BUILD_DIR", "~/.cache/macos-cua/build/voice-cua")
).expanduser()
DIST = BUILD_ROOT / "dist"
BUILD = BUILD_ROOT / "work"
PLUGIN_ROOT = ROOT.parents[1]
MACOS_CUA_ROOT = PLUGIN_ROOT / "skills" / "macos-cua"
MACOS_CUA_SCRIPTS = MACOS_CUA_ROOT / "scripts"
MACOS_CUA_SERVICE = MACOS_CUA_ROOT / "service"


def run(cmd: list[str], **kwargs) -> None:
    print("  →", " ".join(cmd))
    subprocess.run(cmd, check=True, **kwargs)


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        run([sys.executable, "-m", "pip", "install", "-q", "pyinstaller>=6.0"])


def build(*, clean: bool = True, onefile: bool = False) -> Path:
    if clean:
        for d in (DIST, BUILD):
            if d.exists():
                shutil.rmtree(d)
    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    ensure_pyinstaller()
    if not (MACOS_CUA_SCRIPTS / "compact_mcp.py").is_file():
        print(f"ERROR: missing {MACOS_CUA_SCRIPTS / 'compact_mcp.py'}", file=sys.stderr)
        sys.exit(1)
    if not (MACOS_CUA_SERVICE / "cua_client.py").is_file():
        print(f"ERROR: missing {MACOS_CUA_SERVICE / 'cua_client.py'}", file=sys.stderr)
        sys.exit(1)
    if not CONFIG.is_dir():
        print(f"ERROR: missing {CONFIG}", file=sys.stderr)
        sys.exit(1)
    command = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--onefile" if onefile else "--onedir",
            "--specpath",
            str(BUILD_ROOT),
            "--workpath",
            str(BUILD),
            "--distpath",
            str(DIST),
    ]
    if not onefile:
        command.extend([
            "--windowed",
            "--osx-bundle-identifier",
            "com.ingpoc.cua-service.voice-cua",
        ])
    command.extend([
            "--name",
            "voice-cua",
            "--paths",
            str(PY),
            "--paths",
            str(MACOS_CUA_SCRIPTS),
            "--paths",
            str(MACOS_CUA_SERVICE),
            "--add-data",
            f"{CONFIG}:config/.secret",
            "--hidden-import",
            "voice_cua.voice_stack",
            "--hidden-import",
            "voice_cua.realtime_session",
            "--hidden-import",
            "voice_cua.gateway",
            "--hidden-import",
            "voice_cua.tools",
            "--hidden-import",
            "voice_cua.audio_io",
            "--hidden-import",
            "voice_cua.voice_settings",
            "--hidden-import",
            "voice_cua.activity_log",
            "--hidden-import",
            "voice_cua.startup_trace",
            "--hidden-import",
            "voice_cua.session_hub",
            "--hidden-import",
            "voice_cua.voice_meter",
            "--hidden-import",
            "voice_cua.cua_bridge",
            "--hidden-import",
            "compact_mcp",
            "--hidden-import",
            "cua_client",
            "--hidden-import",
            "websocket",
            str(PY / "voice_cua" / "voice_stack.py"),
    ])
    run(command, cwd=ROOT)
    binary = (
        DIST / "voice-cua"
        if onefile
        else DIST / "voice-cua.app" / "Contents" / "MacOS" / "voice-cua"
    )
    if not binary.exists():
        print(f"ERROR: missing {binary}", file=sys.stderr)
        sys.exit(1)
    binary.chmod(0o755)
    return binary


def main() -> None:
    parser = argparse.ArgumentParser(description="Build voice-cua PyInstaller helper")
    parser.add_argument("--no-clean", action="store_true")
    parser.add_argument(
        "--onefile",
        action="store_true",
        help="Build a slower self-extracting binary instead of the default fast onedir bundle",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Copy binary to this path when set",
    )
    args = parser.parse_args()
    binary = build(clean=not args.no_clean, onefile=args.onefile)
    print(f"✓ built {binary}")
    if args.output:
        in_app_bundle = (
            args.output.parent.name == "MacOS"
            and args.output.parent.parent.name == "Contents"
        )
        built_app = binary.parents[2] if binary.parents[2].suffix == ".app" else None
        if in_app_bundle and built_app:
            target_app = args.output.parents[2]
            if target_app.exists():
                shutil.rmtree(target_app)
            shutil.copytree(built_app, target_app, symlinks=True)
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(binary, args.output)
            args.output.chmod(0o755)
        print(f"✓ copied to {args.output}")


if __name__ == "__main__":
    main()
