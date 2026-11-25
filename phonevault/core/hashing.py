"""Two hashes. Quick = cheap fingerprint (size + head + tail). Full = sha256, the truth."""
from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK = 64 * 1024


def quick_hash(path: Path, size: int | None = None) -> str:
    """blake2b over size, first 64 KB and last 64 KB. Fast even on slow disks."""
    p = Path(path)
    if size is None:
        size = p.stat().st_size
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    with p.open("rb") as f:
        h.update(f.read(CHUNK))
        if size > CHUNK * 2:
            f.seek(size - CHUNK)
            h.update(f.read(CHUNK))
        elif size > CHUNK:
            f.seek(CHUNK)
            h.update(f.read())
    return h.hexdigest()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()
