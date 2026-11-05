#!/usr/bin/env python3
"""Align ACU package and app version metadata before committing a release."""
import argparse
import json
import plistlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'plugins/agent-computer-use'


def set_version(root, version):
    if not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', version):
        raise ValueError('version must be stable major.minor.patch')
    manifests = [root / name for name in ('plugin.json', '.cursor-plugin/plugin.json',
                 '.claude-plugin/plugin.json', '.grok-plugin/plugin.json')]
    changes = {}
    for path in manifests:
        content = path.read_text()
        old = json.loads(content)['version']
        if not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', old):
            raise ValueError(f'invalid current version: {path}')
        if tuple(map(int, version.split('.'))) < tuple(map(int, old.split('.'))):
            raise ValueError(f'version would decrease: {path}')
        updated, count = re.subn(r'("version"\s*:\s*")' + re.escape(old) + r'(")',
                                  lambda m: m.group(1) + version + m.group(2), content)
        if count != 1:
            raise ValueError(f'expected one version field: {path}')
        changes[path] = updated
    path = root / 'skills/macos-cua/service/Resources/Info.plist'
    content = path.read_text()
    info = plistlib.loads(content.encode())
    for key in ('CFBundleShortVersionString', 'CFBundleVersion'):
        old = info[key]
        if not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', old) or tuple(map(int, version.split('.'))) < tuple(map(int, old.split('.'))):
            raise ValueError(f'invalid or decreasing {key}')
        content, count = re.subn(r'(<key>' + key + r'</key>\s*<string>)[^<]*(</string>)',
                                 lambda m: m.group(1) + version + m.group(2), content)
        if count != 1:
            raise ValueError(f'expected one {key}')
    changes[path] = content
    for path, content in changes.items():
        path.write_text(content)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version')
    args = parser.parse_args()
    try:
        set_version(ROOT, args.version)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f'version update failed: {exc}\n')
    print(f'ACU version metadata aligned to {args.version}')
