"""Size-based rotation for Samantha's logs, done by the writer (0.2.86).

``samantha-activity.jsonl`` and ``voice.log`` in ``~/.cache/macos-cua`` roll over at 50 MB. The last
five rotated files are kept gzipped (``samantha-activity.1.jsonl.gz`` newest … ``.5.jsonl.gz``; ``voice.1.log.gz``
…); anything older is deleted. The Swift service (``CUALogRotation``) follows the same protocol, so Python and
Swift writers can share one file:

* every append opens the path with ``O_APPEND``, takes a shared ``flock`` on that open file, and checks the
  descriptor still is the file at the path (same inode). If a rotation renamed it, it reopens and retries,
  so no row ever lands in a rotated file.
* a persistent ``<log>.rotation.lock`` serializes rotations across Python and Swift processes, including gzip;
  the writer whose append crosses the limit takes an exclusive ``flock`` on the open log, re-checks
  inode and size, shifts the older ``.N.gz`` files up, renames the live file to ``<stem>.1<suffix>`` and only
  then releases the log lock. The new live file is created by the next append. ``.1`` is gzipped under the rotation lock
  (temp file then rename), so a crash leaves either the plain or the gzipped copy, never half of one.

Only these exact log names are ever renamed or deleted: never a socket, lock or anything else in the folder.
"""

from __future__ import annotations

import errno
import fcntl
import gzip
import os
import shutil
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

MAX_BYTES = 50 * 1024 * 1024
KEEP = 5
_thread_lock = threading.Lock()


@contextmanager
def _rotation_lock(path: Path):
    fd = os.open(str(path) + '.rotation.lock', os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def max_bytes() -> int:
    """``VOICE_CUA_LOG_MAX_BYTES`` overrides the 50 MB limit (tests only)."""
    raw = os.environ.get("VOICE_CUA_LOG_MAX_BYTES", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return MAX_BYTES
    return value if value > 0 else MAX_BYTES


def _split(path: Path) -> tuple[str, str]:
    """``samantha-activity.jsonl`` -> (``samantha-activity``, ``.jsonl``); ``voice.log`` -> (``voice``, ``.log``)."""
    suffix = path.suffix
    return (path.name[: -len(suffix)] if suffix else path.name), suffix


def rotated_path(path: Path, index: int, *, gz: bool = True) -> Path:
    stem, suffix = _split(path)
    return path.with_name(f"{stem}.{index}{suffix}" + (".gz" if gz else ""))


def rotated_files(path: Path) -> list[Path]:
    """Existing rotated copies, newest first (plain ``.N`` left by a crash before gzip included)."""
    out: list[Path] = []
    for i in range(1, KEEP + 1):
        for candidate in (rotated_path(path, i, gz=False), rotated_path(path, i)):
            if candidate.is_file():
                out.append(candidate)
    return out


def _same_file(fd: int, path: Path) -> bool:
    try:
        a, b = os.fstat(fd), os.stat(path)
    except FileNotFoundError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _open_live(path: Path, lock: int) -> int:
    """Open the live file and lock it, retrying while another writer rotates it away."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(50):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        fcntl.flock(fd, lock)
        if _same_file(fd, path):
            return fd
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    raise OSError(errno.EAGAIN, f"{path.name} kept moving during rotation")


def gzip_file(src: Path, dst: Path) -> None:
    tmp = dst.with_name(dst.name + ".tmp")
    with src.open("rb") as fin, gzip.open(tmp, "wb") as fout:
        shutil.copyfileobj(fin, fout, 1024 * 1024)
    os.replace(tmp, dst)
    src.unlink()


def _shift(path: Path) -> None:
    """Make room for a new ``.1``: compress a leftover plain ``.N``, drop ``.KEEP``, move ``.N`` -> ``.N+1``."""
    for i in range(1, KEEP + 1):
        plain = rotated_path(path, i, gz=False)
        if plain.is_file():
            gzip_file(plain, rotated_path(path, i))
    oldest = rotated_path(path, KEEP)
    if oldest.is_file():
        oldest.unlink()
    for i in range(KEEP - 1, 0, -1):
        src = rotated_path(path, i)
        if src.is_file():
            os.replace(src, rotated_path(path, i + 1))


def rotate_if_needed(path: Path, limit: int | None = None) -> bool:
    """Rotate ``path`` when it is at or over ``limit``. Returns True when this call rotated it."""
    limit = limit or max_bytes()
    try:
        if os.stat(path).st_size < limit:
            return False
    except FileNotFoundError:
        return False
    with _thread_lock, _rotation_lock(path):
        fd = _open_live(path, fcntl.LOCK_EX)
        try:
            if os.fstat(fd).st_size < limit:
                return False  # another writer rotated it first
            _shift(path)
            os.replace(path, rotated_path(path, 1, gz=False))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        plain = rotated_path(path, 1, gz=False)
        if plain.is_file():
            gzip_file(plain, rotated_path(path, 1))
    return True


def append(path: Path, data: bytes, limit: int | None = None) -> None:
    """Append ``data`` in ONE ``O_APPEND`` write under a shared lock, then rotate if the file is now full."""
    with _thread_lock:
        fd = _open_live(path, fcntl.LOCK_SH)
        try:
            os.write(fd, data)
            size = os.fstat(fd).st_size
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
    if size >= (limit or max_bytes()):
        rotate_if_needed(path, limit)


class RotatingStdStreams:
    """Keeps this process's stdout/stderr (``voice.log``, inherited from CUAService) rotating.

    Python-level writes go through this wrapper. Before every line the wrapper re-checks that its
    descriptor still is the live file at ``path`` and reopens when another writer rotated it, so a row can
    never land in a renamed (or already deleted) copy. Once the file is full it rotates under the exclusive
    lock and ``dup2``s a fresh ``O_APPEND`` descriptor BEFORE the lock is released, so nothing else writes
    into the rotated copy.
    """

    def __init__(self, path: Path, stream, limit: int | None = None) -> None:
        self.path, self._stream, self._limit = path, stream, limit
        self._lock = threading.Lock()
        self._pending = ""

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def write(self, text: str) -> int:
        with self._lock:
            self._pending += text
            while "\n" in self._pending:
                line, self._pending = self._pending.split("\n", 1)
                self._write_line(line + "\n")
            return len(text)

    def flush(self) -> None:
        with self._lock:
            if self._pending:
                self._write_line(self._pending)
                self._pending = ""
            self._stream.flush()

    def _stream_fd(self) -> int | None:
        try:
            return self._stream.fileno()
        except (AttributeError, OSError, ValueError):
            return None   # in-memory stream (tests): no descriptor to revalidate

    def _reopen_locked(self, fd: int) -> None:
        """Point ``fd`` (and stderr when it mirrors stdout) at the current live file."""
        fresh = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            targets = (1, 2) if fd == 1 else (fd,)
            for target in targets:
                os.dup2(fresh, target)
        finally:
            os.close(fresh)

    def _write_line(self, line: str) -> None:
        stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        payload = f"{stamp} {line}"
        fd = self._stream_fd()
        if fd is None:
            self._stream.write(payload)
            self._stream.flush()
            return
        with _thread_lock:
            for _ in range(50):
                if not _same_file(fd, self.path):
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self._reopen_locked(fd)
                fcntl.flock(fd, fcntl.LOCK_SH)
                if _same_file(fd, self.path):
                    try:
                        self._stream.write(payload)
                        self._stream.flush()
                        size = os.fstat(fd).st_size
                    finally:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    break
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:
                raise OSError(errno.EAGAIN, f"{self.path.name} kept moving during rotation")
        if size >= (self._limit or max_bytes()):
            self._rotate()

    def _rotate(self) -> None:
        with _thread_lock, _rotation_lock(self.path):
            self._rotate_locked()

    def _rotate_locked(self) -> None:
        limit = self._limit or max_bytes()
        fd = _open_live(self.path, fcntl.LOCK_EX)
        try:
            if os.fstat(fd).st_size < limit:
                return
            _shift(self.path)
            os.replace(self.path, rotated_path(self.path, 1, gz=False))
            stream_fd = self._stream_fd()
            if stream_fd is not None:
                self._reopen_locked(stream_fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        gzip_file(rotated_path(self.path, 1, gz=False), rotated_path(self.path, 1))


def install_voice_log_rotation(path: Path | None = None, limit: int | None = None) -> bool:
    """When fd 1/2 are the voice.log file, reopen it ``O_APPEND`` and route Python's streams through rotation."""
    import sys

    path = path or Path.home() / ".cache/macos-cua/voice.log"
    try:
        if not _same_file(1, path):
            return False
    except OSError:
        return False
    fresh = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        for target in (1, 2):
            os.dup2(fresh, target)
    finally:
        os.close(fresh)
    out = os.fdopen(1, "w", encoding="utf-8", buffering=1, closefd=False)
    sys.stdout = RotatingStdStreams(path, out, limit)
    sys.stderr = sys.stdout
    return True
