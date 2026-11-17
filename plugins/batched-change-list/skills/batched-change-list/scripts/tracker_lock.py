"""Cross-process advisory locks for tracker mutations and snapshots."""
import errno
import hashlib
import os
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None


def _lock_path(path, purpose):
    root = (Path('/tmp') / f'bcl-{purpose}-locks-{os.getuid()}' if hasattr(os, 'getuid')
            else Path.home() / '.agents' / 'state' / f'bcl-{purpose}-locks')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root / (hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest() + '.lock')


def tracker_lock_path(path):
    return _lock_path(path, 'tracker')


def plan_lock_path(path):
    return _lock_path(path, 'plan')


def verify_lock_path(path):
    return _lock_path(path, 'verify')


@contextmanager
def exclusive_lock(path, *, blocking=True):
    if fcntl is None and msvcrt is None:
        raise RuntimeError('tracker locking unavailable on this platform')
    with Path(path).open('a+b') as lock:
        if fcntl is not None:
            mode = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(lock, mode)
            except BlockingIOError as error:
                raise LockBusyError('lock is already held') from error
        else:
            if os.fstat(lock.fileno()).st_size == 0:
                lock.write(b'\0')
                lock.flush()
            while True:
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EDEADLK):
                        raise
                    if not blocking:
                        raise LockBusyError('lock is already held') from error
                    time.sleep(0.05)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_UN)
            else:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


class LockBusyError(RuntimeError):
    pass
