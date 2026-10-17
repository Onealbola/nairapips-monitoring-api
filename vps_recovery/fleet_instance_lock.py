"""Lifetime OS lock per fleet role. No PID guessing, process killing or trading."""
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def instance_lock(state_root, role):
    if role not in ('supervisor', 'roster-watcher', 'breach-relay'):
        raise ValueError('Unknown fleet role')
    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    handle = (root / ('fleet-' + role + '.lock')).open('a+b')
    locked = False
    try:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise RuntimeError('Another updated ' + role + ' instance already holds this fleet lock') from exc
        yield
    finally:
        if locked:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
