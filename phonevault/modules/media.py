"""Gallery albums + plain files + app folders. All three are 'copy new files'."""
from __future__ import annotations

from ..core.engine import Engine, Plan
from ..transports.base import RemoteFile, Transport


def scan_roots(engine: Engine, transport: Transport, roots: list[str]) -> list[RemoteFile]:
    ev = engine.events
    files: list[RemoteFile] = []
    for root in roots:
        ev.set(phase="scanning", message=f"Reading phone folder {root}/")
        try:
            got = transport.list_tree(root)
        except Exception as e:  # noqa: BLE001
            ev.log_line(f"{root}/: {e}", "warn")
            continue
        ev.log_line(f"{root}/: {len(got)} files")
        files.extend(got)
    return files


def plan_gallery(engine: Engine, transport: Transport, roots: list[str] | None = None) -> Plan:
    engine.refresh_index()
    roots = roots or engine.cfg.gallery_roots
    files = scan_roots(engine, transport, roots)
    engine.events.set(phase="planning", message="Comparing with the PC")
    plan = engine.planner.plan_gallery(files)
    # documents, PDFs, text... that live inside album folders are not albums: mirror them under "Phone files"
    from pathlib import PurePosixPath
    from ..core.config import MEDIA_EXT
    docs = [f for f in files if not f.is_dir and PurePosixPath(f.name).suffix.lower() not in MEDIA_EXT]
    if docs:
        extra = engine.planner.plan_files(docs)
        plan.items.extend(extra.items)
    s = plan.summary()
    engine.events.set(phase="planned", message=f"{s['copy']} new files, {s['check']} to compare, {len(s['albums'])} albums")
    return plan


def plan_files(engine: Engine, transport: Transport, roots: list[str] | None = None) -> Plan:
    engine.refresh_index()
    roots = roots or engine.cfg.file_roots
    files = scan_roots(engine, transport, roots)
    engine.events.set(phase="planning", message="Comparing with the PC")
    plan = engine.planner.plan_files(files)
    s = plan.summary()
    engine.events.set(phase="planned", message=f"{s['copy']} new files, {s['check']} to compare")
    return plan


def extdata_filter(files: list[RemoteFile], skip_packages: set[str]) -> list[RemoteFile]:
    """Drop cache folders and noisy packages. Path looks like Android/data/<pkg>/files/x."""
    from ..core.config import EXTDATA_SKIP_DIRS
    out = []
    for f in files:
        parts = f.path.split("/")
        if len(parts) < 3 or parts[0] != "Android":
            continue
        pkg = parts[2]
        if pkg in skip_packages:
            continue
        if any(seg in EXTDATA_SKIP_DIRS for seg in parts[3:-1]):
            continue
        out.append(f)
    return out


def plan_extdata(engine: Engine, transport: Transport) -> Plan:
    """App external data (Android/media, Android/data, Android/obb) -> <pc_root>/Phone app data/Android/..."""
    if not getattr(transport, "has_shell", False):
        raise RuntimeError("App data folders need the adb link (the normal USB file mode cannot see Android/data).")
    engine.refresh_index()
    files = scan_roots(engine, transport, engine.cfg.extdata_roots)
    kept = extdata_filter(files, set(engine.cfg.extdata_skip_packages))
    engine.events.log_line(f"app data: {len(files)} files seen, {len(kept)} kept after skipping caches and noisy apps")
    plan = engine.planner.plan_files(kept, subfolder=engine.cfg.extdata_subfolder)
    plan.kind = "extdata"
    return plan


def sync(engine: Engine, transport: Transport, what: str = "gallery", dry_run: bool = False,
         roots: list[str] | None = None) -> tuple[Plan, str | None]:
    if what == "gallery":
        plan = plan_gallery(engine, transport, roots)
    elif what == "files":
        plan = plan_files(engine, transport, roots)
    elif what == "extdata":
        plan = plan_extdata(engine, transport)
    else:
        raise ValueError(what)
    rid = engine.execute(plan, transport, dry_run=dry_run)
    return plan, rid
