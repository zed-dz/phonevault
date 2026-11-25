"""Duplicate sweep inside the vault.

A phone file that already lives somewhere in your library should not exist twice.
This finds files under the "new albums" folder (and any folder you name) whose CONTENT is already present
elsewhere in the vault, and moves the newcomer to the trash folder (journaled, undoable). The older copy,
the one you filed by hand, always wins.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from ..core.engine import Engine
from ..core.hashing import quick_hash, sha256


def find_duplicates(engine: Engine, inside: list[Path], progress=None) -> list[tuple[Path, Path]]:
    """(newcomer, kept) pairs: newcomer is under one of `inside`, kept is an identical file elsewhere."""
    cfg = engine.cfg
    root = cfg.root
    inside = [Path(p) for p in inside]
    def is_inside(p: str) -> bool:
        return any(Path(p) == i or Path(p).is_relative_to(i) for i in inside)
    con = engine.index.con
    rows = con.execute("SELECT path, size, quick FROM files WHERE size > 0 AND quick IS NOT NULL").fetchall()
    by_key: dict[tuple[int, str], list[str]] = {}
    for path, size, quick in rows:
        by_key.setdefault((size, quick), []).append(path)
    pairs: list[tuple[Path, Path]] = []
    n = 0
    for key, paths in by_key.items():
        if len(paths) < 2:
            continue
        newcomers = [p for p in paths if is_inside(p)]
        keepers = [p for p in paths if not is_inside(p)]
        if not newcomers or not keepers:
            continue
        for nc in newcomers:
            try:
                s1 = sha256(Path(nc))          # hashed now, not remembered: the one that stays must be the same
            except OSError:
                continue
            for kp in keepers:
                try:
                    if sha256(Path(kp)) == s1:
                        pairs.append((Path(nc), Path(kp)))
                        break
                except OSError:
                    continue
            n += 1
            if progress and n % 100 == 0:
                progress(n, len(pairs))
    return pairs


def sweep(engine: Engine, inside: list[Path] | None = None, dry_run: bool = False) -> dict:
    cfg = engine.cfg
    ev = engine.events
    inside = inside or [cfg.root / cfg.new_albums_subfolder]
    ev.reset("dedupe")
    engine.refresh_index()
    ev.set(phase="planning", message="Comparing file contents")
    pairs = find_duplicates(engine, inside, progress=lambda n, d: ev.set(message=f"Checked {n} candidates, {d} duplicates"))
    freed = sum(p.stat().st_size for p, _ in pairs if p.exists())
    out = {"duplicates": len(pairs), "bytes": freed, "run_id": None, "dry_run": dry_run}
    if dry_run or not pairs:
        ev.set(phase="done", finished=time.time(), message=f"{len(pairs)} duplicates ({freed / 1e6:.0f} MB)" + (" (dry run)" if dry_run else ""))
        return out
    rid = engine.journal.start_run("dedupe", note=f"{len(pairs)} duplicates of files already in the vault")
    out["run_id"] = rid
    ev.set(phase="copying", total_files=len(pairs), run_id=rid, message="Moving duplicates to the trash")
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-dedupe"
    for n, (nc, kp) in enumerate(pairs, 1):
        try:
            q = engine.journal.move_to_trash(nc, stamp)
            engine.journal.record(rid, "quarantine", str(nc), str(q), q.stat().st_size, None,
                                  nc.parent.name, "ok", f"same content as {kp}")
            engine.index.remove_file(nc)
            engine.index.forget_phone_seen_for(str(nc))
            ev.set(done_files=n, current=str(nc))
        except OSError as e:
            engine.journal.record(rid, "quarantine", str(nc), None, status="failed", note=str(e))
    # drop album folders left empty
    for i in inside:
        for dp, dns, fns in sorted(os.walk(i), key=lambda t: -len(t[0])):
            if not dns and not fns and Path(dp) != i:
                try:
                    Path(dp).rmdir()
                    engine.journal.record(rid, "rmdir", None, dp)
                except OSError:
                    pass
    engine.journal.finish_run(rid)
    ev.set(phase="done", finished=time.time(), message=f"{len(pairs)} duplicates moved to the trash ({freed / 1e6:.0f} MB). Undo: {rid}")
    ev.emit("run_done", run_id=rid, status="done")
    return out
