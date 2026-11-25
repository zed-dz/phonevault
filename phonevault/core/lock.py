"""One writer at a time. The dashboard and the command line share one journal, so only the process that
holds <app_dir>/engine.lock may start a run or clean up after a crash. Everyone else reads."""
from __future__ import annotations

import os
from pathlib import Path

_held: dict[str, list] = {}        # lock path -> [open file, how many engines of this process use it]


def acquire(path: Path) -> tuple[bool, bool]:
    """(we hold it, we just took it). Never blocks."""
    key = os.path.normcase(str(path))
    if key in _held:
        _held[key][1] += 1
        return True, False
    f = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return False, False
    _held[key] = [f, 1]
    return True, True


def release(path: Path) -> None:
    key = os.path.normcase(str(path))
    h = _held.get(key)
    if not h:
        return
    h[1] -= 1
    if h[1] <= 0:
        _held.pop(key)
        h[0].close()               # closing the file gives the lock back
