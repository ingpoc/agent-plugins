#!/usr/bin/env python3
"""Build and upload the macOS release assets for Agent Computer Use."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
REPO = "ingpoc/agent-plugins"
PLUGIN = "agent-computer-use"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_mount(dmg: Path, mountpoint: Path) -> Path:
    result = subprocess.run(
        ["hdiutil", "attach", "-readonly", "-nobrowse", "-plist", "-mountpoint", str(mountpoint), str(dmg)],
        check=True, capture_output=True,
    )
    info = plistlib.loads(result.stdout)
    actual_mount = next((Path(entity["mount-point"]) for entity in info.get("system-entities", [])
                         if entity.get("mount-point") and Path(entity["mount-point"]).resolve() == mountpoint.resolve()), None)
    if actual_mount is None:
        raise RuntimeError(f"hdiutil did not mount {dmg} at {mountpoint}")
    return actual_mount


def _default_detach(mountpoint: Path) -> None:
    subprocess.run(["hdiutil", "detach", str(mountpoint)], check=True, capture_output=True)


def read_app(dmg: Path, mount_image=None, detach_image=None) -> dict:
    """Mount a DMG read-only, extract its single app identity, then detach."""
    mount_image = mount_image or _default_mount
    detach_image = detach_image or _default_detach
    with tempfile.TemporaryDirectory(prefix="agent-plugins-dmg-") as temp:
        mountpoint = Path(temp) / "mount"
        mountpoint.mkdir()
        mounted = mount_image(dmg, mountpoint)
        try:
            apps = list(Path(mounted).glob("*.app"))
            if len(apps) != 1:
                raise RuntimeError(f"expected one .app in {dmg}, found {len(apps)}")
            plist_path = apps[0] / "Contents" / "Info.plist"
            info = plistlib.loads(plist_path.read_bytes())
            values = {
                "bundleId": info.get("CFBundleIdentifier"),
                "version": info.get("CFBundleShortVersionString"),
                "build": info.get("CFBundleVersion"),
                "minMacOS": info.get("LSMinimumSystemVersion"),
            }
            if not all(isinstance(value, str) and value.strip() for value in values.values()):
                raise RuntimeError(f"incomplete app metadata in {plist_path}")
            return values
        finally:
            detach_image(Path(mounted))


def build_manifest(dmgs: list[Path], out: Path, *, root: Path = ROOT, mount_image=None,
                   detach_image=None, generated_at: str | None = None) -> Path:
    if not dmgs:
        raise RuntimeError("provide at least one --dmg")
    out.mkdir(parents=True, exist_ok=True)
    source_installer = root / "scripts" / "install-mac.sh"
    if not source_installer.is_file():
        raise RuntimeError(f"missing installer script: {source_installer}")
    entries = []
    names = set()
    for supplied in dmgs:
        dmg = supplied.resolve()
        if not dmg.is_file() or dmg.suffix.lower() != ".dmg":
            raise RuntimeError(f"not a DMG file: {supplied}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.dmg", dmg.name):
            raise RuntimeError("DMG filename must be a plain release asset name")
        if dmg.name in names:
            raise RuntimeError(f"duplicate DMG filename: {dmg.name}")
        names.add(dmg.name)
        app = read_app(dmg, mount_image, detach_image)
        entries.append((app, dmg))
    identities = {(app["version"], app["build"], app["bundleId"], app["minMacOS"])
                  for app, _dmg in entries}
    if len(identities) != 1:
        raise RuntimeError("ambiguous DMGs: app version, build, bundle ID, and minimum macOS must agree")
    for dmg in dmgs:
        shutil.copy2(dmg, out / dmg.name)
    shutil.copy2(source_installer, out / "install-mac.sh")
    app, primary_dmg = entries[0]
    manifest = {
        "schema": 1,
        "repo": REPO,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "plugins": {PLUGIN: {
            "version": app["version"],
            "build": app["build"],
            "url": f"https://github.com/{REPO}/releases/download/agent-computer-use-v{app['version']}/{primary_dmg.name}",
            "sha256": sha256(primary_dmg),
            "minMacOS": app["minMacOS"],
            "bundleId": app["bundleId"],
        }},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    assets = sorted([out / dmg.name for _app, dmg in entries] + [out / "manifest.json", out / "install-mac.sh"], key=lambda p: p.name)
    (out / "SHA256SUMS").write_text("".join(f"{sha256(path)}  {path.name}\n" for path in assets), encoding="utf-8")
    return out / "manifest.json"


def validate_upload(directory: Path) -> list[Path]:
    manifest_path = directory / "manifest.json"
    sums_path = directory / "SHA256SUMS"
    if not manifest_path.is_file() or not sums_path.is_file():
        raise RuntimeError("upload directory must contain manifest.json and SHA256SUMS")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    plugins = manifest.get("plugins", {})
    release = plugins.get(PLUGIN)
    if manifest.get("schema") != 1 or manifest.get("repo") != REPO or not isinstance(release, dict):
        raise RuntimeError("unsupported or ambiguous manifest")
    if not all(isinstance(release.get(key), str) and release[key] for key in
               ("version", "build", "url", "sha256", "minMacOS", "bundleId")):
        raise RuntimeError("manifest has incomplete agent-computer-use metadata")
    primary_name = Path(urlparse(release["url"]).path).name
    if not primary_name or not primary_name.lower().endswith(".dmg"):
        raise RuntimeError("manifest URL must identify a DMG asset")
    expected = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if not match or Path(match.group(2)).name != match.group(2) or match.group(2) in expected:
            raise RuntimeError("invalid SHA256SUMS")
        expected[match.group(2)] = match.group(1)
    required = {"manifest.json", "install-mac.sh", primary_name}
    if not required <= set(expected) or any(not (name.endswith(".dmg") or name in {"manifest.json", "install-mac.sh"}) for name in expected):
        raise RuntimeError("SHA256SUMS must cover the manifest, installer, and DMG assets")
    files = []
    for name, digest in expected.items():
        path = directory / name
        if not path.is_file() or sha256(path) != digest:
            raise RuntimeError(f"missing or checksum-mismatched asset: {name}")
        files.append(path)
    if expected.get(primary_name) != release["sha256"]:
        raise RuntimeError("manifest DMG checksum does not match SHA256SUMS")
    return sorted([*files, sums_path], key=lambda path: path.name)


def upload(tag: str, directory: Path, *, yes: bool = False, dry_run: bool = False,
           run=subprocess.run) -> int:
    if yes and dry_run:
        raise RuntimeError("--yes and --dry-run cannot be used together")
    if not re.fullmatch(r"agent-computer-use-v\d+\.\d+\.\d+", tag):
        raise RuntimeError("tag must be agent-computer-use-vMAJOR.MINOR.PATCH")
    files = validate_upload(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    if tag != "agent-computer-use-v" + manifest["plugins"][PLUGIN]["version"]:
        raise RuntimeError("upload tag disagrees with manifest version")
    command = ["gh", "release", "upload", tag, *(str(path) for path in files), "--clobber", "-R", REPO]
    print(shlex.join(command))
    if not yes:
        return 0
    return run(command, check=False).returncode


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="build a self-contained release asset directory")
    build.add_argument("--dmg", action="append", type=Path, required=True)
    build.add_argument("--out", type=Path, required=True)
    send = sub.add_parser("upload", help="upload a validated asset directory to GitHub Releases")
    send.add_argument("--tag", required=True)
    send.add_argument("--dir", type=Path, required=True)
    mode = send.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the command without uploading (default)")
    mode.add_argument("--yes", action="store_true", help="execute gh release upload")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "build":
            build_manifest(args.dmg, args.out)
        else:
            return upload(args.tag, args.dir, yes=args.yes, dry_run=args.dry_run)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"release_assets: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
