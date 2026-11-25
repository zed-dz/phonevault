"""The undo book. Every file the tool writes or moves is one row here.

Rules:
- The tool never deletes. "Remove" = move to the quarantine (trash) folder on the
  same drive, which is an instant rename.
- undo(run_id) reverses that run's rows newest-first. A copied file goes to
  trash; a quarantined file comes back. Undo is itself a run, so it can be undone too.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .names import inside


class LinkError(OSError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, started REAL NOT NULL, finished REAL,
  status TEXT NOT NULL DEFAULT 'running', note TEXT, files INTEGER DEFAULT 0, bytes INTEGER DEFAULT 0,
  undone INTEGER DEFAULT 0, undo_of TEXT
);
CREATE TABLE IF NOT EXISTS ops (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, seq INTEGER NOT NULL,
  op TEXT NOT NULL,
  src TEXT, dst TEXT, size INTEGER DEFAULT 0, sha TEXT, album TEXT,
  status TEXT NOT NULL,
  note TEXT, at REAL NOT NULL, undone INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ops_run ON ops(run_id, seq);
"""

# op values: copy | restore | quarantine | move | mkdir | rmdir | undo-failed
# status values: ok | skipped-dup | failed
#
# "move": src = where the file or folder was, dst = where it is now (the review
# page files albums into category folders this way). Undo renames dst back to
# src, into a numbered slot if the owner put something new there meanwhile.


@dataclass
class Run:
    id: str
    kind: str
    started: float
    finished: float | None
    status: str
    note: str | None
    files: int
    bytes: int
    undone: int
    undo_of: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Journal:
    def __init__(self, db_path: Path, trash: Path, root: Path | None = None):
        self.db_path = Path(db_path)
        self.trash = Path(trash)
        self.root = Path(root) if root else None    # paths under it keep their short form in the trash
        self.read_only = False                      # another process owns the journal (see core/lock.py)
        self.gate: Callable[[], None] | None = None # raises when the PC folder must not be written to
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.con.executescript(SCHEMA)
        self._seq: dict[str, int] = {}

    # ---- runs -----------------------------------------------------------
    def start_run(self, kind: str, note: str = "", undo_of: str | None = None) -> str:
        if self.read_only:
            raise RuntimeError("PhoneVault is already running (the dashboard or another command). "
                               "Use that one, or close it first.")
        if self.gate and kind != "restore":         # a restore writes to the phone, not to this drive
            self.gate()
        rid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.con.execute(
            "INSERT INTO runs(id,kind,started,status,note,undo_of) VALUES(?,?,?,?,?,?)",
            (rid, kind, time.time(), "running", note, undo_of),
        )
        self.con.commit()
        self._seq[rid] = 0
        return rid

    def finish_run(self, rid: str, status: str = "done", note: str | None = None) -> None:
        agg = self.con.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0) FROM ops WHERE run_id=? AND status='ok' AND op IN ('copy','restore','move')",
            (rid,),
        ).fetchone()
        self.con.execute(
            "UPDATE runs SET finished=?, status=?, files=?, bytes=?, note=COALESCE(?, note) WHERE id=?",
            (time.time(), status, agg[0], agg[1], note, rid),
        )
        self.con.commit()

    def runs(self, limit: int = 50) -> list[Run]:
        rows = self.con.execute(
            "SELECT id,kind,started,finished,status,note,files,bytes,undone,undo_of FROM runs ORDER BY started DESC LIMIT ?",
            (limit,),
        )
        return [Run(*r) for r in rows]

    def run(self, rid: str) -> Run | None:
        r = self.con.execute(
            "SELECT id,kind,started,finished,status,note,files,bytes,undone,undo_of FROM runs WHERE id=?", (rid,)
        ).fetchone()
        return Run(*r) if r else None

    # ---- ops ------------------------------------------------------------
    def record(self, rid: str, op: str, src: str | None, dst: str | None, size: int = 0,
               sha: str | None = None, album: str | None = None, status: str = "ok",
               note: str | None = None) -> int:
        self._seq[rid] = self._seq.get(rid, 0) + 1
        cur = self.con.execute(
            "INSERT INTO ops(run_id,seq,op,src,dst,size,sha,album,status,note,at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (rid, self._seq[rid], op, src, dst, size, sha, album, status, note, time.time()),
        )
        self.con.commit()
        return int(cur.lastrowid or 0)

    def ops(self, rid: str) -> list[dict[str, Any]]:
        cur = self.con.execute("SELECT * FROM ops WHERE run_id=? ORDER BY seq", (rid,))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def settle(self, op_id: int, status: str, note: str | None = None) -> None:
        self.con.execute("UPDATE ops SET status=?, note=COALESCE(?, note) WHERE id=?", (status, note, op_id))
        self.con.commit()

    def settle_pending(self) -> int:
        """After a power cut: a file that was on its way to the trash is there, or it is not."""
        n = 0
        for oid, src, dst in self.con.execute("SELECT id, src, dst FROM ops WHERE status='pending'").fetchall():
            there = bool(dst) and Path(dst).exists()
            self.settle(oid, "ok" if there and not Path(src).exists() else "failed", "settled after an interruption")
            n += 1
        return n

    # ---- links ----------------------------------------------------------
    def guard(self, p: Path) -> None:
        """Refuse to touch a path that a link or junction leads out of the PC folder (or out of the trash)."""
        if self.root is None or inside(p, self.root):
            return
        if not _is_inside(self.trash, self.root) and inside(p, self.trash):
            return
        raise LinkError(f"{p} is outside the PC folder {self.root} (a link or junction leads out). Nothing was written there.")

    # ---- quarantine -----------------------------------------------------
    def quarantine_path(self, dst: Path, stamp: str | None = None) -> Path:
        """Mirror the file's path under trash/<timestamp>/ so nothing collides."""
        stamp = stamp or time.strftime("%Y%m%d-%H%M%S")
        dst = Path(dst)
        rel = Path(*dst.parts[1:]) if dst.anchor else dst
        if self.root is not None and _is_inside(dst, self.root):
            rel = Path(*dst.parts[len(self.root.parts):])
        q = self.trash / stamp / rel
        self.guard(dst)
        self.guard(q)
        q.parent.mkdir(parents=True, exist_ok=True)
        return q

    def move_to_trash(self, dst: Path, stamp: str | None = None) -> Path:
        q = self.quarantine_path(dst, stamp)
        if q.exists():
            q = unique_path(q)
        try:
            os.replace(dst, q)  # same drive: instant rename
        except OSError:
            shutil.move(str(dst), str(q))
        return q

    # ---- move -----------------------------------------------------------
    def move_path(self, rid: str, src: Path, dst: Path, album: str | None = None) -> Path:
        """Rename a file or a whole folder and write one 'move' row. Returns where it landed.

        Parent folders are created; an occupied dst gets a numbered slot (unique_path).
        Same drive = instant rename; a different drive falls back to shutil.move.
        """
        src, dst = Path(src), Path(dst)
        if not src.exists():
            raise FileNotFoundError(f"nothing at {src}")
        if dst == src:
            return src
        if src.is_dir() and _is_inside(dst, src):
            raise ValueError(f"cannot move {src} into itself")
        self.guard(src)
        self.guard(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            dst = unique_path(dst)
        size = tree_size(src)
        try:
            _rename(src, dst)
        except Exception as e:  # noqa: BLE001
            self.record(rid, "move", str(src), str(dst), size, None, album, "failed", str(e))
            raise
        self.record(rid, "move", str(src), str(dst), size, None, album)
        return dst

    # ---- undo -----------------------------------------------------------
    def undo(self, rid: str, progress: Callable[[int, int, str], None] | None = None,
             on_file_removed: Callable[[Path], None] | None = None,
             on_file_restored: Callable[[Path], None] | None = None) -> str:
        """Reverse a run. Returns the id of the undo run."""
        rows = self.ops(rid)
        urid = self.start_run("undo", note=f"undo of {rid}", undo_of=rid)
        stamp = time.strftime("%Y%m%d-%H%M%S") + "-undo"
        total = len(rows)
        done = failed = 0
        for row in reversed(rows):
            done += 1
            if row["status"] != "ok" or row["undone"]:
                continue
            try:
                if row["op"] in ("copy", "restore") and row["dst"]:
                    dst = Path(row["dst"])
                    if dst.exists():
                        q = unique_path(self.quarantine_path(dst, stamp))
                        oid = self.record(urid, "quarantine", str(dst), str(q), row["size"], row["sha"], row["album"], "pending")
                        try:
                            _rename(dst, q)
                        except Exception:
                            self.settle(oid, "failed")
                            raise
                        self.settle(oid, "ok")
                        if on_file_removed:
                            on_file_removed(dst)
                    else:
                        self.record(urid, "quarantine", str(dst), None, status="skipped-dup", note="already gone")
                elif row["op"] == "quarantine":
                    q = Path(row["dst"])
                    back = Path(row["src"])
                    if q.exists():
                        self.guard(q)
                        self.guard(back)
                        back.parent.mkdir(parents=True, exist_ok=True)
                        if back.exists():
                            back = unique_path(back)
                        _rename(q, back)
                        self.record(urid, "restore", str(q), str(back), row["size"], row["sha"], row["album"])
                        if on_file_restored:
                            on_file_restored(back)
                elif row["op"] == "move":
                    now_at = Path(row["dst"])
                    back = Path(row["src"])
                    if now_at.exists():
                        self.guard(now_at)
                        self.guard(back)
                        back.parent.mkdir(parents=True, exist_ok=True)
                        if back.exists():
                            back = unique_path(back)
                        _rename(now_at, back)
                        # recorded as a move too, so undoing the undo moves it forward again
                        self.record(urid, "move", str(now_at), str(back), row["size"], row["sha"], row["album"])
                    else:
                        self.record(urid, "move", str(now_at), None, status="skipped-dup", note="already gone")
                elif row["op"] == "mkdir":
                    d = Path(row["dst"])
                    self.guard(d)
                    try:
                        d.rmdir()  # only when empty
                        self.record(urid, "rmdir", None, str(d))
                    except OSError:
                        pass
                self.con.execute("UPDATE ops SET undone=1 WHERE id=?", (row["id"],))
                self.con.commit()
            except Exception as e:  # keep going, record the failure
                failed += 1
                self.record(urid, "undo-failed", row["src"], row["dst"], status="failed", note=str(e))
            if progress:
                progress(done, total, row["dst"] or "")
        if not failed:          # a partly undone run can be undone again; the finished rows are skipped
            self.con.execute("UPDATE runs SET undone=1 WHERE id=?", (rid,))
        self.finish_run(urid, "partial" if failed else "done")
        return urid

    def close(self) -> None:
        self.con.close()


def unique_path(p: Path) -> Path:
    """name.jpg -> name (2).jpg -> name (3).jpg ...  (a folder keeps its whole name: v1.2 -> v1.2 (2))"""
    if not p.exists():
        return p
    stem, suffix = (p.name, "") if p.is_dir() else (p.stem, p.suffix)
    n = 2
    while True:
        cand = p.with_name(f"{stem} ({n}){suffix}")
        if not cand.exists():
            return cand
        n += 1


def _rename(src: Path, dst: Path) -> None:
    try:
        os.replace(src, dst)  # same drive: instant, files and folders alike
    except OSError:
        shutil.move(str(src), str(dst))


def _is_inside(p: Path, folder: Path) -> bool:
    try:
        return Path(os.path.normcase(str(p))).is_relative_to(Path(os.path.normcase(str(folder))))
    except ValueError:
        return False


def tree_size(p: Path) -> int:
    """Bytes of one file, or of every file under a folder."""
    try:
        if p.is_file():
            return p.stat().st_size
        total = 0
        for dirpath, _, names in os.walk(p):
            for n in names:
                try:
                    total += (Path(dirpath) / n).stat().st_size
                except OSError:
                    pass
        return total
    except OSError:
        return 0
