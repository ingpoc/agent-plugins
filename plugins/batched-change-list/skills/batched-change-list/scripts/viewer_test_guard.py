"""Find and stop test viewers rooted in this test run's temporary directories."""
import os
import re
import shlex
import signal
import subprocess
import tempfile
import time
from pathlib import Path

_temporary_directory = tempfile.TemporaryDirectory
TEMP_ROOTS = set()


def _tracked_temporary_directory(*args, **kwargs):
    directory = _temporary_directory(*args, **kwargs)
    TEMP_ROOTS.add(Path(directory.name).resolve())
    return directory


tempfile.TemporaryDirectory = _tracked_temporary_directory


def viewer_command_under(command, roots):
    try:
        args = shlex.split(command)
        executable = Path(args[0]).name.lower().removesuffix(".exe")
        if not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable):
            return False
        index = 1
        while index < len(args) and args[index].startswith("-"):
            option = args[index]
            if option in ("-c", "-m"):
                return False
            index += 2 if option in ("-W", "-X") else 1
        if index >= len(args) or Path(args[index]).name != "serve.py":
            return False
        script = Path(args[index])
        if not script.is_absolute():
            return False
        page = data = None
        options_with_values = {"--port", "--inventory", "--viewer-metadata", "--base"}
        command_args = args[index + 1:]
        cursor = 0
        while cursor < len(command_args):
            arg = command_args[cursor]
            if arg == "--data":
                cursor += 1
                if cursor < len(command_args):
                    data = Path(command_args[cursor])
                cursor += 1
                continue
            if arg.startswith("--data="):
                data = Path(arg.partition("=")[2])
            elif arg in options_with_values:
                cursor += 2
                continue
            elif arg.startswith("-"):
                cursor += 1
                continue
            elif page is None:
                page = Path(arg)
            cursor += 1
        roots = [Path(root).resolve() for root in roots]
        return any(path and path.is_absolute() and path.resolve().is_relative_to(root)
                   for path in (page, data) for root in roots)
    except (IndexError, OSError, ValueError):
        return False


def viewer_pids_under(roots):
    roots = [Path(root).resolve() for root in roots]
    result = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=True)
    pids = set()
    for line in result.stdout.splitlines():
        try:
            pid, command = line.strip().split(None, 1)
            if viewer_command_under(command, roots):
                pids.add(int(pid))
        except ValueError:
            continue
    return pids


def stop_viewers(root, pids):
    roots = root if isinstance(root, (set, list, tuple)) else (root,)
    roots = [Path(path).resolve() for path in roots]
    pids = set(map(int, pids)) & viewer_pids_under(roots)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 3
    while pids and time.monotonic() < deadline:
        remaining = set()
        for pid in pids:
            try:
                waited, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                waited = 0
            if not waited:
                try:
                    os.kill(pid, 0)
                    remaining.add(pid)
                except ProcessLookupError:
                    pass
        pids = remaining
        if pids:
            time.sleep(.05)
    if pids:
        raise AssertionError(f"test-owned viewers would not stop under {roots}: {sorted(pids)}")


def assert_no_viewers(roots):
    pids = viewer_pids_under(roots)
    if pids:
        stop_viewers(roots, pids)
        raise AssertionError(f"viewer processes leaked from tests: {sorted(pids)}")
