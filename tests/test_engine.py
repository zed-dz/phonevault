"""Engine.execute end to end against the fake phone (LocalTransport)."""
from __future__ import annotations

import queue
import shutil
import threading
import time
from pathlib import Path

from conftest import GALLERY_COPY_COUNT, PHONE_MTIME, PHONE_TREE
from phonevault.modules import media
from phonevault.transports.local import LocalTransport


def drain(q: queue.Queue) -> list[dict]:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def test_new_files_land_in_the_right_album_folders(engine, phone, pc):
    plan = media.plan_gallery(engine, phone)
    assert plan.summary()["copy"] == GALLERY_COPY_COUNT
    rid = engine.execute(plan, phone)
    assert rid
    assert (pc / "Phone camera" / "IMG_0001.jpg").read_bytes() == PHONE_TREE["DCIM/Camera/IMG_0001.jpg"]
    assert (pc / "Phone camera" / "VID_0001.mp4").exists()
    assert (pc / "Phone screenshots" / "Screenshot_1.png").exists()
    assert (pc / "PROJECTS" / "garden plans" / "logo.png").exists()
    assert (pc / "PROJECTS" / "kitchen renovation sketches" / "d1.jpg").exists()
    assert (pc / "_New from phone" / "Random Stuff" / "r1.jpg").exists()
    # nothing non-media or noisy came over
    assert not (pc / "_New from phone" / "Notes").exists()
    assert not list(pc.rglob("1.jpg"))
    assert (pc / "Phone files" / "Pictures" / "Notes" / "todo.txt").exists()  # documents inside album folders are mirrored, never dropped
    run = engine.journal.run(rid)
    assert run.status == "done" and run.files == GALLERY_COPY_COUNT
    assert run.bytes == sum(len(v) for k, v in PHONE_TREE.items() if Path(k).suffix in (".jpg", ".mp4", ".png", ".txt")
                            and ".thumbnails" not in k and k.split("/")[0] in ("DCIM", "Pictures", "Movies"))


def test_phone_mtime_is_preserved_even_when_transport_drops_metadata(engine, phone_root, pc):
    class PlainCopy(LocalTransport):
        def pull(self, remote_path, local_path):
            Path(local_path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.root / remote_path, local_path)  # no mtime, unlike copy2

    tr = PlainCopy(phone_root)
    plan = media.plan_gallery(engine, tr)
    engine.execute(plan, tr)
    dst = pc / "Phone camera" / "IMG_0001.jpg"
    assert abs(dst.stat().st_mtime - PHONE_MTIME) < 2


def test_content_duplicate_with_different_name_is_skipped(engine, phone, pc, mkfile):
    old = mkfile(pc / "Phone camera" / "old_name.jpg", PHONE_TREE["DCIM/Camera/IMG_0001.jpg"])
    plan = media.plan_gallery(engine, phone)
    assert {i.remote.name: i.action for i in plan.items}["IMG_0001.jpg"] == "copy"  # name differs, planner cannot know
    rid = engine.execute(plan, phone)
    assert not (pc / "Phone camera" / "IMG_0001.jpg").exists()
    ops = {o["src"]: o for o in engine.journal.ops(rid)}
    dup = ops["DCIM/Camera/IMG_0001.jpg"]
    assert dup["status"] == "skipped-dup"
    assert dup["dst"] == str(old)
    assert engine.events.state["dups"] == 1
    assert engine.index.phone_seen("DCIM/Camera/IMG_0001.jpg", len(PHONE_TREE["DCIM/Camera/IMG_0001.jpg"])) == str(old)
    assert engine.journal.run(rid).files == GALLERY_COPY_COUNT - 1


def test_name_collision_with_different_content_gets_numbered_name(engine, phone, pc, mkfile):
    original = b"something else entirely"
    mkfile(pc / "Phone camera" / "IMG_0001.jpg", original)
    plan = media.plan_gallery(engine, phone)
    engine.execute(plan, phone)
    assert (pc / "Phone camera" / "IMG_0001.jpg").read_bytes() == original
    assert (pc / "Phone camera" / "IMG_0001 (2).jpg").read_bytes() == PHONE_TREE["DCIM/Camera/IMG_0001.jpg"]


def test_dry_run_copies_nothing_and_returns_none(engine, phone, pc):
    plan = media.plan_gallery(engine, phone)
    assert engine.execute(plan, phone, dry_run=True) is None
    assert not list(pc.rglob("*.jpg")) and not list(pc.rglob("*.mp4"))
    assert engine.journal.runs() == []
    assert engine.events.state["phase"] == "done"
    assert "Dry run" in engine.events.state["message"]


def test_empty_plan_returns_none(engine, phone):
    from phonevault.core.engine import Plan
    assert engine.execute(Plan("gallery"), phone) is None
    assert engine.events.state["message"] == "Nothing to do"


def test_second_run_plans_zero_copies(engine, phone, pc):
    first = media.plan_gallery(engine, phone)
    engine.execute(first, phone)
    second = media.plan_gallery(engine, phone)
    s = second.summary()
    assert s["copy"] == 0
    for i in second.items:
        if i.action != "skip-noise":
            assert i.action == "skip-exists", i
            assert i.reason.startswith("handled before")
    assert engine.execute(second, phone) is None


def test_events_counters_and_run_done(engine, phone):
    q = engine.events.subscribe()
    plan = media.plan_gallery(engine, phone)
    rid = engine.execute(plan, phone)
    evs = drain(q)
    types = [e["type"] for e in evs]
    assert "run_start" in types
    assert types.count("file_done") == GALLERY_COPY_COUNT
    done = [e for e in evs if e["type"] == "run_done"]
    assert len(done) == 1
    assert done[0]["run_id"] == rid and done[0]["status"] == "done"
    assert done[0]["copied"] == GALLERY_COPY_COUNT and done[0]["dups"] == 0 and done[0]["failed"] == 0
    st = engine.events.state
    assert st["phase"] == "done"
    assert st["done_files"] == st["total_files"] == GALLERY_COPY_COUNT
    assert st["dups"] == 0 and st["failed"] == 0
    assert st["total_bytes"] == plan.summary()["bytes"] == st["done_bytes"]
    assert st["albums"]["Camera"]["copied"] == 3
    assert st["run_id"] == rid
    assert f"{GALLERY_COPY_COUNT} copied, 0 duplicates skipped, 0 failed" == st["message"]


def test_cancel_stops_early(engine, phone_root, pc):
    class SlowCancelling(LocalTransport):
        pulled = 0

        def pull(self, remote_path, local_path):
            time.sleep(0.02)
            super().pull(remote_path, local_path)
            SlowCancelling.pulled += 1
            if SlowCancelling.pulled == 1:
                t = threading.Thread(target=engine.cancel)  # cancel from another thread
                t.start()
                t.join()

    tr = SlowCancelling(phone_root)
    plan = media.plan_gallery(engine, tr)
    rid = engine.execute(plan, tr)
    assert SlowCancelling.pulled == 1
    assert engine.journal.run(rid).status == "cancelled"
    assert engine.events.state["phase"] == "cancelled"
    ok_ops = [o for o in engine.journal.ops(rid) if o["op"] == "copy" and o["status"] == "ok"]
    assert len(ok_ops) == 1
    copied = [p for p in pc.rglob("*") if p.is_file()]
    assert len(copied) == 1
    # staging folder of the run was cleaned
    assert not (engine.staging / rid).exists()


def test_transfer_failure_is_recorded_not_raised(engine, phone_root, pc):
    class Broken(LocalTransport):
        def pull(self, remote_path, local_path):
            if remote_path.endswith("IMG_0002.jpg"):
                raise OSError("cable yanked")
            super().pull(remote_path, local_path)

    tr = Broken(phone_root)
    plan = media.plan_gallery(engine, tr)
    rid = engine.execute(plan, tr)
    failed = [o for o in engine.journal.ops(rid) if o["status"] == "failed"]
    assert len(failed) == 1 and "cable yanked" in failed[0]["note"]
    assert engine.events.state["failed"] == 1
    assert engine.journal.run(rid).status == "done"
    assert not (pc / "Phone camera" / "IMG_0002.jpg").exists()


def test_files_sync_mirrors_download_folder(engine, phone, pc):
    plan, rid = media.sync(engine, phone, "files", roots=["Download"])
    assert rid
    assert (pc / "Phone files" / "Download" / "docs" / "report.pdf").exists()
    assert (pc / "Phone files" / "Download" / "readme.txt").exists()
    assert plan.kind == "files"
