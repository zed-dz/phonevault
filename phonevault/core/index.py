"""SQLite index of every file already on the PC side.

Why: "is this phone file already here?" must be answered without re-reading
the PC folder each run. The index remembers size, mtime, quick hash and sha256.
Unchanged files (same size + mtime) are not re-hashed.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Callable

from .config import SKIP_NAMES
from .hashing import quick_hash, sha256

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  size INTEGER NOT NULL,
  mtime REAL NOT NULL,
  quick TEXT,
  sha TEXT,
  seen REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS files_name_size ON files(name, size);
CREATE INDEX IF NOT EXISTS files_size ON files(size);
CREATE INDEX IF NOT EXISTS files_sha ON files(sha);
CREATE TABLE IF NOT EXISTS phone_seen (
  path TEXT PRIMARY KEY,
  size INTEGER NOT NULL,
  resolved_to TEXT,
  at REAL NOT NULL,
  mtime REAL,
  pc_mtime REAL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS roots (path TEXT PRIMARY KEY, files INTEGER NOT NULL);
"""
ANY = object()


def under(path: str, root: str) -> bool:
    """path is root or sits below it, by its text."""
    p, r = os.path.normcase(path), os.path.normcase(root).rstrip("\\/")
    return p == r or p.startswith(r + os.sep)


class FileIndex:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.con.executescript(SCHEMA)
        have = {r[1] for r in self.con.execute("PRAGMA table_info(phone_seen)")}
        for col in ("mtime", "pc_mtime"):
            if col not in have:
                self.con.execute(f"ALTER TABLE phone_seen ADD COLUMN {col} REAL")

    # ---- which PC folder these rows are about ------------------------------
    def root(self) -> str:
        r = self.con.execute("SELECT value FROM meta WHERE key='root'").fetchone()
        return r[0] if r else ""

    def switch_root(self, new: str) -> None:
        """Forget every row about another PC folder: it must never count as 'already here'."""
        for path in [r[0] for r in self.con.execute("SELECT path FROM files")]:
            if not under(path, new):
                self.con.execute("DELETE FROM files WHERE path=?", (path,))
        for path, pc in self.con.execute("SELECT path, resolved_to FROM phone_seen").fetchall():
            if not pc or not under(pc, new):
                self.con.execute("DELETE FROM phone_seen WHERE path=?", (path,))
        self.con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('root',?)", (new,))
        self.con.commit()

    def root_files(self, root: str) -> int:
        """How many files this PC folder held the last time it was read. -1 = never seen."""
        r = self.con.execute("SELECT files FROM roots WHERE path=?", (os.path.normcase(root),)).fetchone()
        return r[0] if r else -1

    def note_root(self, root: str, files: int) -> None:
        self.con.execute("INSERT OR REPLACE INTO roots(path,files) VALUES(?,?)", (os.path.normcase(root), files))
        self.con.commit()

    # ---- building -------------------------------------------------------
    def refresh(self, root: Path, progress: Callable[[int, str], None] | None = None,
                hash_new: bool = True) -> dict:
        """Walk root, upsert rows, drop rows for files that vanished. Returns counts."""
        root = Path(root)
        now = time.time()
        seen = added = changed = 0
        cur = self.con.cursor()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_NAMES and not d.startswith(".phonevault")]
            for fn in filenames:
                if fn in SKIP_NAMES:
                    continue
                p = Path(dirpath) / fn
                try:
                    st = p.stat()
                except OSError:
                    continue
                seen += 1
                row = cur.execute("SELECT size, mtime, quick FROM files WHERE path=?", (str(p),)).fetchone()
                if row and row[0] == st.st_size and abs(row[1] - st.st_mtime) < 2 and row[2]:
                    cur.execute("UPDATE files SET seen=? WHERE path=?", (now, str(p)))
                    continue
                q = None
                if hash_new:
                    try:
                        q = quick_hash(p, st.st_size)
                    except OSError:
                        q = None
                if row:
                    changed += 1
                else:
                    added += 1
                cur.execute(
                    "INSERT OR REPLACE INTO files(path,name,size,mtime,quick,sha,seen) VALUES(?,?,?,?,?,NULL,?)",
                    (str(p), fn, st.st_size, st.st_mtime, q, now),
                )
                if progress and seen % 200 == 0:
                    progress(seen, str(p))
                if seen % 500 == 0:
                    self.con.commit()
        removed = cur.execute(
            "DELETE FROM files WHERE seen < ? AND path LIKE ?", (now, str(root) + "%")
        ).rowcount
        self.con.commit()
        return {"seen": seen, "added": added, "changed": changed, "removed": removed}

    def add_file(self, p: Path, sha: str | None = None) -> None:
        p = Path(p)
        st = p.stat()
        self.con.execute(
            "INSERT OR REPLACE INTO files(path,name,size,mtime,quick,sha,seen) VALUES(?,?,?,?,?,?,?)",
            (str(p), p.name, st.st_size, st.st_mtime, quick_hash(p, st.st_size), sha, time.time()),
        )
        self.con.commit()

    def remove_file(self, p: Path) -> None:
        self.con.execute("DELETE FROM files WHERE path=?", (str(p),))
        self.con.commit()

    # ---- questions ------------------------------------------------------
    def has_name_size(self, name: str, size: int) -> list[str]:
        return [r[0] for r in self.con.execute("SELECT path FROM files WHERE name=? AND size=?", (name, size))]

    def same_size(self, size: int) -> list[str]:
        return [r[0] for r in self.con.execute("SELECT path FROM files WHERE size=?", (size,))]

    def find_sha(self, sha: str) -> list[str]:
        return [r[0] for r in self.con.execute("SELECT path FROM files WHERE sha=?", (sha,))]

    def find_quick(self, quick: str, size: int) -> list[str]:
        return [r[0] for r in self.con.execute("SELECT path FROM files WHERE quick=? AND size=?", (quick, size))]

    def ensure_sha(self, path: str) -> str:
        row = self.con.execute("SELECT sha FROM files WHERE path=?", (path,)).fetchone()
        if row and row[0]:
            return row[0]
        s = sha256(Path(path))
        self.con.execute("UPDATE files SET sha=? WHERE path=?", (s, path))
        self.con.commit()
        return s

    # ---- phone files we already dealt with (copied or found as duplicates) ---
    def mark_phone_seen(self, phone_path: str, size: int, resolved_to: str | None, mtime: float | None = None) -> None:
        try:
            pc_mtime = Path(resolved_to).stat().st_mtime if resolved_to else None
        except OSError:
            pc_mtime = None
        self.con.execute("INSERT OR REPLACE INTO phone_seen(path,size,resolved_to,at,mtime,pc_mtime) VALUES(?,?,?,?,?,?)",
                         (phone_path, size, resolved_to, time.time(), mtime, pc_mtime))
        self.con.commit()

    def phone_seen(self, phone_path: str, size: int, mtime=ANY) -> str | None:
        """The PC copy of this phone file, if we made or checked it before and nothing changed since:
        same phone path, size and date, and a PC file that still has the size and date it had then."""
        row = self.con.execute("SELECT resolved_to, mtime, pc_mtime FROM phone_seen WHERE path=? AND size=?", (phone_path, size)).fetchone()
        if not row:
            return None
        pc, seen_mtime, pc_mtime = row
        try:
            st = Path(pc).stat() if pc else None
        except OSError:
            st = None
        if st is None or pc_mtime is None or st.st_size != size or abs(st.st_mtime - pc_mtime) > 2:
            self.con.execute("DELETE FROM phone_seen WHERE path=?", (phone_path,))   # gone or changed: look again
            self.con.commit()
            return None
        if mtime is not ANY and (seen_mtime is None) != (mtime is None):
            return None
        if mtime is not ANY and mtime is not None and abs(mtime - seen_mtime) > 2:
            return None
        return pc

    def forget_phone_seen_for(self, pc_path: str) -> None:
        self.con.execute("DELETE FROM phone_seen WHERE resolved_to=?", (pc_path,))
        self.con.commit()

    def count(self) -> int:
        return self.con.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    def close(self) -> None:
        self.con.close()


def folder_names(root: Path) -> list[tuple[str, str]]:
    """(folder name, absolute folder path) for every folder under root. Used by album mapping."""
    out: list[tuple[str, str]] = []
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".phonevault") and d not in SKIP_NAMES]
        for d in dirnames:
            out.append((d, str(Path(dirpath) / d)))
    return out
