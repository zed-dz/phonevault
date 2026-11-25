"""Plan, then copy. The only code path that writes phone files into the PC folder. It only reads the phone.

A phone file is left alone only when
  1. we copied or compared it before, its path, size and date on the phone are the same, and the PC copy
     still has the size and date it had then; or
  2. a PC file with the same SHA-256 exists, hashed again right now.
Name and size alone never count. For 2 the phone's own sha256sum is used when adb can run it; otherwise the
file is copied to a staging folder and hashed there. Everything else is copied: staging, size check, hash,
move into place, keep the phone's date, one journal row. Everything a run did can be undone with its id.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable, Iterable

from .albums import AlbumMapper
from . import lock
from .config import Config, MEDIA_EXT, SKIP_NAMES, app_dir
from .events import Events
from .hashing import sha256
from .index import FileIndex, under
from .journal import Journal, unique_path
from .names import pc_name, pc_rel
from ..transports.base import RemoteFile, Transport


@dataclass
class PlanItem:
    remote: RemoteFile
    album: str              # display name of the album / group
    dst_folder: str         # absolute PC folder
    action: str             # copy | check (compare content first) | skip-exists | skip-noise
    reason: str = ""
    how: str = ""           # how the album was mapped

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class Plan:
    kind: str
    items: list[PlanItem] = field(default_factory=list)

    @property
    def to_copy(self) -> list[PlanItem]:
        return [i for i in self.items if i.action in ("copy", "check")]

    def summary(self) -> dict:
        by_album: dict[str, dict] = {}
        for i in self.items:
            a = by_album.setdefault(i.album, {"album": i.album, "dst": i.dst_folder, "how": i.how,
                                               "copy": 0, "exists": 0, "check": 0, "noise": 0, "bytes": 0})
            key = {"copy": "copy", "skip-exists": "exists", "check": "check", "skip-noise": "noise"}[i.action]
            a[key] += 1
            if i.action == "copy":
                a["bytes"] += i.remote.size
        return {
            "kind": self.kind,
            "files": len(self.items),
            "copy": sum(i.action == "copy" for i in self.items),
            "check": sum(i.action == "check" for i in self.items),
            "bytes": sum(i.remote.size for i in self.items if i.action == "copy"),
            "albums": sorted(by_album.values(), key=lambda a: (-a["copy"], a["album"].lower())),
        }


def is_noise(rf: RemoteFile) -> bool:
    if rf.name in SKIP_NAMES or rf.name.startswith(".trashed-") or rf.name.startswith("._"):
        return True
    parts = rf.path.split("/")
    return any(p in SKIP_NAMES for p in parts[:-1])


class Planner:
    def __init__(self, cfg: Config, index: FileIndex, mapper: AlbumMapper):
        self.cfg = cfg
        self.index = index
        self.mapper = mapper

    def _decide(self, rf: RemoteFile, dst_folder: str) -> tuple[str, str]:
        if is_noise(rf):
            return "skip-noise", "system file"
        seen = self.index.phone_seen(rf.path, rf.size, rf.mtime)
        if seen and under(seen, self.cfg.pc_root):
            return "skip-exists", f"handled before -> {seen}"
        target = Path(dst_folder) / pc_name(rf.name)
        try:
            if target.exists() and target.stat().st_size == rf.size:
                return "check", "same name and size in place: compare content"
        except OSError:
            pass
        hits = self.index.has_name_size(pc_name(rf.name), rf.size)
        if hits:
            return "check", f"same name and size at {hits[0]}: compare content"
        return "copy", ""

    def plan_gallery(self, files: Iterable[RemoteFile], media_only: bool = True) -> Plan:
        plan = Plan("gallery")
        for rf in files:
            if rf.is_dir:
                continue
            if media_only and Path(rf.name).suffix.lower() not in MEDIA_EXT:
                continue
            if is_noise(rf):
                plan.items.append(PlanItem(rf, rf.parent.rsplit("/", 1)[-1], "", "skip-noise", "system file", "-"))
                continue
            album = rf.parent.rsplit("/", 1)[-1] if rf.parent else rf.top
            if rf.parent in self.cfg.gallery_roots or not rf.parent:
                album = rf.parent or rf.top       # files sitting directly in DCIM/ or Pictures/
            m = self.mapper.resolve(album, rf.parent)
            action, reason = self._decide(rf, m.pc_folder)
            plan.items.append(PlanItem(rf, album, m.pc_folder, action, reason, m.how))
        return plan

    def plan_files(self, files: Iterable[RemoteFile], subfolder: str | None = None) -> Plan:
        """Mirror the phone's own folder structure under <pc_root>/<Phone files>/."""
        plan = Plan("files")
        base = self.cfg.root / (subfolder or self.cfg.files_subfolder)
        for rf in files:
            if rf.is_dir:
                continue
            dst_folder = str(base / pc_rel(rf.parent))
            album = rf.top if subfolder is None else subfolder
            action, reason = self._decide(rf, dst_folder)
            plan.items.append(PlanItem(rf, album, dst_folder, action, reason, "mirror"))
        return plan


class Engine:
    """Owns the index, journal, mapper and the progress bus. One per process."""

    def __init__(self, cfg: Config | None = None, events: Events | None = None):
        self.cfg = cfg or Config.load()
        self.events = events or Events()
        d = app_dir()
        self.index = FileIndex(d / "index.db")
        self.journal = Journal(d / "journal.db", self.cfg.trash, self.cfg.root)
        self.mapper = AlbumMapper(self.cfg.root, d / "album-map.json", self.cfg.new_albums_subfolder, self.cfg.album_aliases)
        self.planner = Planner(self.cfg, self.index, self.mapper)
        self._cancel = threading.Event()
        self._pause = threading.Event()
        self.busy = threading.Lock()
        self._lock_path = d / "engine.lock"
        self._holds_lock, fresh = lock.acquire(self._lock_path)
        self.journal.read_only = not self._holds_lock
        self.journal.gate = self.ready_to_write
        if self._holds_lock:
            self.follow_root()
        if fresh:
            self.recover()       # never while another process is copying: its half files are not leftovers

    @property
    def read_only(self) -> bool:
        return not self._holds_lock

    # ---- is the PC folder the right one, there, and healthy? ---------------
    def follow_root(self) -> None:
        """pc_root changed since last time: forget what we knew about the old folder."""
        self.mapper.pc_root = self.cfg.root
        self.journal.root, self.journal.trash = self.cfg.root, self.cfg.trash
        old, new = self.index.root(), str(self.cfg.root)
        if os.path.normcase(old) == os.path.normcase(new):
            return
        self.index.switch_root(new)
        self.mapper.rebase(old, new)
        if old:
            self.events.log_line(f"The PC folder is now {new}. Everything is compared again.", "warn")

    def ready_to_write(self) -> None:
        """Every writer calls this first (through journal.start_run at the latest)."""
        self.follow_root()
        root = self.cfg.root
        had = self.index.root_files(str(root))
        if had >= 0 and not root.exists():
            raise RuntimeError(f"The PC folder {root} is missing. Plug the drive in, or change pc_root. "
                               "If you removed it on purpose, make the folder again.")
        if had > 0 and not any(root.iterdir()):
            raise RuntimeError(f"The PC folder {root} held {had} files and is empty now. Is the drive mounted? "
                               "If you emptied it on purpose, put any file in it and run again.")
        if had < 0 and root.exists():
            self.index.note_root(str(root), 0)      # seen: from now on "missing" means unplugged
        from . import drivecheck
        drivecheck.refuse_if_unhealthy(root)   # a sick disk gets nothing written
        root.mkdir(parents=True, exist_ok=True)
        if had < 0:
            self.index.note_root(str(root), 0)
        self.journal.guard(self.cfg.state_dir / "staging")

    def safe_dir(self, p: Path) -> Path:
        """A folder of ours inside the PC folder, made if needed. Refused when a link leads out."""
        self.journal.guard(p)
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ---- crash / power-cut recovery ----------------------------------------
    def recover(self) -> dict:
        """Called on every start. A power cut can only leave two things behind:
        runs still marked 'running' (mark them 'interrupted'; their journal rows stay valid and undoable)
        and half-written files in the staging folder (never inside an album; moved to the trash, not deleted).
        The next sync re-plans and skips everything that was already copied, so it simply continues."""
        out = {"interrupted": 0, "staged_files": 0}
        try:
            cur = self.journal.con.execute("UPDATE runs SET status='interrupted', finished=COALESCE(finished, ?) "
                                           "WHERE status='running'", (time.time(),))
            self.journal.con.commit()
            out["interrupted"] = cur.rowcount
            self.journal.settle_pending()
        except Exception:  # noqa: BLE001
            pass
        try:
            st = self.cfg.state_dir / "staging"
            for d in list(st.iterdir()):
                if d.is_dir() and d.name not in ("upload",):
                    for f in list(d.rglob("*")):
                        if f.is_file():
                            try:
                                self.journal.move_to_trash(f, "staging-leftovers")
                                out["staged_files"] += 1
                            except OSError:
                                pass
                    try:
                        for sub in sorted(d.rglob("*"), reverse=True):
                            if sub.is_dir():
                                sub.rmdir()
                        d.rmdir()
                    except OSError:
                        pass
        except OSError:
            pass
        if out["interrupted"] or out["staged_files"]:
            self.events.log_line(f"Recovered after an interruption: {out['interrupted']} run(s) marked interrupted, "
                                 f"{out['staged_files']} half-copied file(s) moved to the trash", "warn")
        return out

    # ---- control --------------------------------------------------------
    def cancel(self) -> None:
        self._cancel.set()

    def pause(self, on: bool = True) -> None:
        if on:
            self._pause.set()
        else:
            self._pause.clear()

    def _wait_if_paused(self) -> None:
        while self._pause.is_set() and not self._cancel.is_set():
            time.sleep(0.2)

    @property
    def staging(self) -> Path:
        return self.safe_dir(self.cfg.state_dir / "staging")

    # ---- index ----------------------------------------------------------
    def refresh_index(self) -> dict:
        self.follow_root()
        had = self.index.root_files(str(self.cfg.root))
        if had >= 0 and not self.cfg.root.exists():
            # An unplugged drive must not look like an empty backup, nor like "nothing to do".
            raise RuntimeError(f"The PC folder {self.cfg.pc_root} is missing. Plug the drive in, or change pc_root.")
        self.events.set(phase="indexing", message=f"Reading {self.cfg.pc_root}")
        stats = self.index.refresh(self.cfg.root, progress=lambda n, p: self.events.set(message=f"Indexed {n} PC files"))
        if stats["seen"]:
            self.index.note_root(str(self.cfg.root), stats["seen"])
        self.mapper.invalidate()
        self.events.log_line(f"PC index: {stats['seen']} files ({stats['added']} new, {stats['removed']} gone)")
        return stats

    # ---- execute --------------------------------------------------------
    def same_on_pc(self, sha: str, size: int, memo: dict) -> str | None:
        """A PC file with this content, proven by hashing it again now (once per run and file). None = copy it."""
        for path in dict.fromkeys(self.index.find_sha(sha) + self.index.same_size(size)):
            if not under(path, self.cfg.pc_root):
                continue
            try:
                st = Path(path).stat()
                if st.st_size != size:
                    continue
                key = (path, st.st_mtime_ns)
                if key not in memo:
                    memo[key] = sha256(Path(path))
                    self.index.con.execute("UPDATE files SET sha=? WHERE path=?", (memo[key], path))
                    self.index.con.commit()
                if memo[key] == sha:
                    return path
            except OSError:
                continue
        return None

    def execute(self, plan: Plan, transport: Transport, dry_run: bool = False, note: str = "") -> str | None:
        """Copy what is new. Returns the run id (None on dry run). Reads the phone, never writes to it."""
        items = plan.to_copy
        ev = self.events
        if not dry_run and items:
            self.ready_to_write()
        ev.reset(plan.kind, transport.describe())
        ev.set(total_files=len(items), total_bytes=sum(i.remote.size for i in items), phase="copying",
               message=f"{len(items)} files to copy or compare" + (" (dry run)" if dry_run else ""))
        for i in plan.items:
            ev.album_bump(i.album, "planned", 1 if i.action in ("copy", "check") else 0)
        if dry_run or not items:
            ev.set(phase="done", finished=time.time(), message="Nothing to do" if not items else "Dry run only")
            return None

        self._cancel.clear()
        rid = self.journal.start_run(plan.kind, note=note or f"{transport.describe()}; {len(items)} files")
        ev.set(run_id=rid)
        memo: dict = {}

        def is_dup(it: PlanItem, sha: str, dup: str) -> None:
            remote, size = it.remote.path, it.remote.size
            self.index.mark_phone_seen(remote, size, dup, it.remote.mtime)
            self.journal.record(rid, "copy", remote, dup, size, sha, it.album, "skipped-dup", f"same content already at {dup}")
            ev.album_bump(it.album, "dups")
            ev.set(dups=ev.state["dups"] + 1, done_files=ev.state["done_files"] + 1, done_bytes=ev.state["done_bytes"] + size)
            ev.emit("file_done", path=remote, status="dup", dst=dup, album=it.album)
            ev.tick_speed()

        ev.emit("run_start", run_id=rid, kind=plan.kind, files=len(items))
        by_remote = {i.remote.path: i for i in items}
        staging_dir = self.staging / rid
        staging_dir.mkdir(parents=True, exist_ok=True)
        # A PC file of the same size may be the same file. Let the phone hash its copy first: that saves the transfer.
        left = []
        for it in items:
            sha = None
            if not self._cancel.is_set() and it.remote.size and self.index.same_size(it.remote.size):
                self._wait_if_paused()
                ev.set(current=it.remote.path, current_album=it.album, current_dst=it.dst_folder, message="Comparing content")
                try:
                    sha = transport.sha256(it.remote.path)
                except Exception:  # noqa: BLE001
                    sha = None
            dup = self.same_on_pc(sha, it.remote.size, memo) if sha else None
            if dup:
                is_dup(it, sha, dup)
            else:
                left.append(it)
        items = left
        ev.set(message=f"{len(items)} files to copy")
        pairs = [(i.remote.path, staging_dir / f"{n:06d}{Path(pc_name(i.remote.name)).suffix[:12]}") for n, i in enumerate(items)]
        made_dirs: set[str] = set()

        # Byte-level progress: watch the staging file grow while the transport writes it.
        watch: dict[str, Path] = {}
        stop_watch = threading.Event()

        def watcher() -> None:
            while not stop_watch.is_set():
                for remote, local in list(watch.items()):
                    try:
                        sz = local.stat().st_size if local.exists() else 0
                    except OSError:
                        sz = 0
                    ev.set(file_done_bytes=sz)
                ev.tick_speed()
                time.sleep(0.4)

        threading.Thread(target=watcher, daemon=True).start()

        def on_start(remote: str) -> None:
            self._wait_if_paused()
            it = by_remote[remote]
            watch.clear()
            watch[remote] = dict(pairs)[remote]
            ev.set(current=remote, current_album=it.album, current_dst=it.dst_folder,
                   file_bytes=it.remote.size, file_done_bytes=0)
            ev.emit("file_start", path=remote, album=it.album, size=it.remote.size)

        def on_done(remote: str, local: Path, ok: bool, err: str) -> None:
            it = by_remote[remote]
            watch.pop(remote, None)
            try:
                if not ok:
                    raise RuntimeError(err or "transfer failed")
                if not local.exists():
                    raise RuntimeError("file did not arrive")
                got = local.stat().st_size
                if it.remote.size and got != it.remote.size:
                    raise RuntimeError(f"size mismatch: phone {it.remote.size}, got {got}")
                sha = sha256(local)
                dup = self.same_on_pc(sha, got, memo)
                if dup:
                    local.unlink(missing_ok=True)
                    is_dup(it, sha, dup)
                    return
                dst_folder = Path(it.dst_folder)
                self.journal.guard(dst_folder)
                if not dst_folder.exists():
                    dst_folder.mkdir(parents=True, exist_ok=True)
                    if str(dst_folder) not in made_dirs:
                        made_dirs.add(str(dst_folder))
                        self.journal.record(rid, "mkdir", None, str(dst_folder), album=it.album)
                dst = unique_path(dst_folder / pc_name(it.remote.name))
                os.replace(local, dst)
                if it.remote.mtime:
                    try:
                        os.utime(dst, (it.remote.mtime, it.remote.mtime))
                    except OSError:
                        pass
                self.index.add_file(dst, sha)
                self.index.mark_phone_seen(remote, got, str(dst), it.remote.mtime)
                self.journal.record(rid, "copy", remote, str(dst), got, sha, it.album, "ok")
                ev.album_bump(it.album, "copied")
                ev.album_bump(it.album, "bytes", got)
                ev.set(done_files=ev.state["done_files"] + 1, done_bytes=ev.state["done_bytes"] + got)
                ev.emit("file_done", path=remote, status="ok", dst=str(dst), album=it.album, size=got)
            except Exception as e:  # noqa: BLE001
                local.unlink(missing_ok=True) if local.exists() else None
                self.journal.record(rid, "copy", remote, it.dst_folder, it.remote.size, None, it.album, "failed", str(e))
                ev.album_bump(it.album, "failed")
                ev.set(failed=ev.state["failed"] + 1, done_files=ev.state["done_files"] + 1)
                ev.emit("file_failed", path=remote, error=str(e), album=it.album)
            finally:
                ev.tick_speed()

        status = "done"
        try:
            transport.pull_batch(pairs, on_done, cancel=self._cancel.is_set, on_start=on_start,
                                 sizes={i.remote.path: i.remote.size for i in items})
            if self._cancel.is_set():
                status = "cancelled"
        except Exception as e:  # noqa: BLE001
            status = "failed"
            ev.log_line(f"Run failed: {e}", "error")
        finally:
            stop_watch.set()
            try:
                for leftover in staging_dir.iterdir():      # half downloads; the phone still has the files
                    leftover.unlink(missing_ok=True)
                staging_dir.rmdir()
            except OSError:
                pass
        n = self.index.con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        self.index.note_root(str(self.cfg.root), max(n, 1))
        self.journal.finish_run(rid, status)
        ev.set(phase=status, finished=time.time(), current="", message=self._final_message(ev.state))
        ev.emit("run_done", run_id=rid, status=status, copied=ev.state["done_files"] - ev.state["dups"] - ev.state["failed"],
                dups=ev.state["dups"], failed=ev.state["failed"])
        return rid

    @staticmethod
    def _final_message(s: dict) -> str:
        copied = s["done_files"] - s["dups"] - s["failed"]
        return f"{copied} copied, {s['dups']} duplicates skipped, {s['failed']} failed"

    # ---- undo -----------------------------------------------------------
    def undo(self, rid: str) -> str:
        """Reverse a run. Touches PC files only: copies go to the trash folder, the phone is not contacted."""
        r = self.journal.run(rid)
        if r and r.kind == "restore":
            raise RuntimeError("That run changed the phone, not the PC. It cannot be undone from here.")
        ev = self.events
        ev.reset("undo")
        ev.set(phase="undoing", run_id=rid, message=f"Undoing {rid}")
        def prog(done: int, total: int, path: str) -> None:
            ev.set(done_files=done, total_files=total, current=path)

        urid = self.journal.undo(rid, progress=prog,
                                 on_file_removed=lambda p: (self.index.remove_file(p), self.index.forget_phone_seen_for(str(p))),
                                 on_file_restored=lambda p: self.index.add_file(p) if p.is_file() else None)
        ev.set(phase="done", finished=time.time(), current="", message=f"Undo done: {urid}")
        ev.emit("run_done", run_id=urid, status="done")
        return urid

    def close(self) -> None:
        self.index.close()
        self.journal.close()
        if self._holds_lock:
            self._holds_lock = False
            lock.release(self._lock_path)
