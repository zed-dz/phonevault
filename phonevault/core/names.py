"""Names that come from the phone are input, not paths.

An Android file name may hold characters Windows treats as a path separator, a drive or a stream
(`..\\x`, `C:x`, `a.jpg:s`). Every name is turned into one plain folder or file name before it
touches the PC. Only the name changes; the bytes of the file do not.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def pc_name(name: str) -> str:
    """One safe folder/file name. '..' -> '_', 'a:b.jpg' -> 'a_b.jpg', 'con.txt' -> '_con.txt'."""
    n = _BAD.sub("_", str(name)).rstrip(" .")
    if not n:
        return "_"
    if n.split(".", 1)[0].strip().lower() in _RESERVED:
        n = "_" + n
    return n


def pc_rel(path: str) -> Path:
    """A phone path ("Download/a/b") as a relative PC path made of safe names only."""
    parts = [pc_name(s) for s in str(path).split("/") if s]
    return Path(*parts) if parts else Path()


def is_safe_rel(path: str) -> bool:
    """True when every part of a '/'-separated path is already a plain name."""
    parts = path.split("/")
    return bool(path) and all(p and pc_name(p) == p for p in parts)


def inside(p: Path | str, folder: Path | str) -> bool:
    """True when p is folder or sits under it, after links and '..' are resolved."""
    try:
        a = os.path.normcase(str(Path(p).resolve()))
        b = os.path.normcase(str(Path(folder).resolve()))
    except OSError:
        return False
    return a == b or a.startswith(b.rstrip("\\/") + os.sep)
