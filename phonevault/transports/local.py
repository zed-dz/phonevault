"""A fake phone: any folder on disk. Used by the tests and the demo."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .base import RemoteFile, Transport


class LocalTransport(Transport):
    name = "local"

    def __init__(self, root: Path):
        self.root = Path(root)

    def available(self) -> bool:
        return self.root.is_dir()

    def describe(self) -> str:
        return f"local folder ({self.root})"

    def list_tree(self, root: str, max_depth: int = 6) -> list[RemoteFile]:
        base = self.root / root
        out: list[RemoteFile] = []
        if not base.exists():
            return out
        for dirpath, _, filenames in os.walk(base):
            for fn in filenames:
                p = Path(dirpath) / fn
                rel = p.relative_to(self.root).as_posix()
                st = p.stat()
                out.append(RemoteFile(rel, fn, st.st_size, st.st_mtime))
        return out

    def list_dir(self, path: str) -> list[RemoteFile]:
        base = self.root / path
        items = []
        for p in sorted(base.iterdir()):
            rel = p.relative_to(self.root).as_posix()
            items.append(RemoteFile(rel, p.name, p.stat().st_size if p.is_file() else 0,
                                    p.stat().st_mtime, p.is_dir()))
        return items

    def pull(self, remote_path: str, local_path: Path) -> None:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.root / remote_path, local_path)

    def sha256(self, remote_path: str) -> str | None:
        from ..core.hashing import sha256
        return sha256(self.root / remote_path)

