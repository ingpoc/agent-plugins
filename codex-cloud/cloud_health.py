#!/usr/bin/env python3
"""Bounded, read-only checks for cloud preview support; no logs are collected."""
import glob
import json
from pathlib import Path
import shutil
import subprocess


def preview_status():
    executable = shutil.which('sites-preview')
    if not executable:
        return {'state': 'unavailable', 'reason': 'sites-preview command not found'}
    try:
        result = subprocess.run([executable, 'status'], capture_output=True, text=True, timeout=5, check=False)
    except subprocess.TimeoutExpired:
        return {'state': 'unknown', 'reason': 'status command timed out'}
    except OSError:
        return {'state': 'unknown', 'reason': 'status command could not run'}
    if result.returncode:
        return {'state': 'unknown', 'reason': 'status command failed'}
    output = result.stdout.strip()
    if output == 'Sites preview stopped':
        return {'state': 'stopped', 'reason': 'no active preview reported'}
    if output.startswith('Sites preview running'):
        return {'state': 'running', 'reason': 'status command reports an active preview'}
    return {'state': 'unknown', 'reason': 'unrecognized status response'}


def helper_status():
    patterns = ['/workspace/**/site-workflow.mjs', str(Path.home()/'.codex/plugins/cache/**/site-workflow.mjs')]
    found = False
    for pattern in patterns:
        if next(glob.iglob(pattern, recursive=True), None):
            found = True
            break
    return {'state': 'found' if found else 'not_found', 'scope': 'workspace and installed plugin cache'}


def health():
    return {'preview': preview_status(), 'sites_source_helper': helper_status(),
            'desktop': {'state': 'unknown', 'reason': 'use the cloud environment and desktop tools to check'},
            'relay_cause': {'state': 'unknown', 'reason': 'preview status does not identify relay failure causes'},
            'scope_note': 'Read-only checks do not start a preview, inspect logs, or establish application health.'}

if __name__ == '__main__':
    print(json.dumps(health(), indent=2))
