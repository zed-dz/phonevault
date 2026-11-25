from __future__ import annotations

import time
from pathlib import Path

import pytest

from conftest import GALLERY_COPY_COUNT
from phonevault.core.journal import Journal, unique_path
from phonevault.modules import media


# ---- undo through the engine -------------------------------------------------

@pytest.fixture
def synced(engine, phone, pc):
    plan = media.plan_gallery(engine, phone)
    rid = engine.execute(plan, phone)
    dsts = [Path(o["dst"]) for o in engine.journal.ops(rid) if o["op"] == "copy" and o["status"] == "ok"]
    assert len(dsts) == GALLERY_COPY_COUNT and all(d.exists() for d in dsts)
    return rid, dsts


def test_undo_moves_copied_files_to_trash_not_deleted(engine, synced, pc, trash):
    rid, dsts = synced
    urid = engine.undo(rid)
    for d in dsts:
        assert not d.exists(), d
    in_trash = {p.name for p in trash.rglob("*") if p.is_file()}
    assert in_trash == {d.name for d in dsts}
    # trash mirrors the original path under a timestamp folder
    q = next(p for p in trash.rglob("IMG_0001.jpg"))
    assert q.parent.name == "Phone camera"
    assert engine.journal.run(rid).undone == 1
    undo_run = engine.journal.run(urid)
    assert undo_run.kind == "undo" and undo_run.undo_of == rid and undo_run.status == "done"
    assert len([o for o in engine.journal.ops(urid) if o["op"] == "quarantine" and o["status"] == "ok"]) == GALLERY_COPY_COUNT
    # the index forgot them, so a later sync would copy them again
    assert engine.index.has_name_size("IMG_0001.jpg", dsts[0].stat().st_size if dsts[0].exists() else 1000) == []
    assert engine.index.phone_seen("DCIM/Camera/IMG_0001.jpg", 1000) is None


def test_undo_removes_recreated_folders_when_empty(engine, synced, pc):
    rid, _ = synced
    made = pc / "_New from phone" / "Random Stuff"
    assert made.is_dir()
    assert any(o["op"] == "mkdir" and o["dst"] == str(made) for o in engine.journal.ops(rid))
    urid = engine.undo(rid)
    assert not made.exists()
    assert any(o["op"] == "rmdir" and o["dst"] == str(made) for o in engine.journal.ops(urid))
    # pre-existing album folders are untouched
    assert (pc / "Phone camera").is_dir()


def test_undo_keeps_recreated_folder_when_owner_added_something(engine, synced, pc, mkfile):
    rid, _ = synced
    made = pc / "_New from phone" / "Random Stuff"
    mkfile(made / "mine.txt", b"my file")
    engine.undo(rid)
    assert made.is_dir() and (made / "mine.txt").exists()


def test_undo_of_undo_restores_files(engine, synced, pc, trash):
    rid, dsts = synced
    urid = engine.undo(rid)
    assert not any(d.exists() for d in dsts)
    urid2 = engine.undo(urid)
    for d in dsts:
        assert d.exists(), d
    assert not [p for p in trash.rglob("*") if p.is_file()]
    assert engine.journal.run(urid).undone == 1
    restores = [o for o in engine.journal.ops(urid2) if o["op"] == "restore" and o["status"] == "ok"]
    assert len(restores) == GALLERY_COPY_COUNT
    assert engine.journal.run(urid2).files == GALLERY_COPY_COUNT
    # restored files are back in the index
    assert engine.index.has_name_size("IMG_0001.jpg", dsts[0].stat().st_size) or \
        any(engine.index.has_name_size(d.name, d.stat().st_size) for d in dsts)


def test_second_sync_after_undo_copies_again(engine, synced, phone, pc):
    rid, dsts = synced
    engine.undo(rid)
    plan = media.plan_gallery(engine, phone)
    assert plan.summary()["copy"] == GALLERY_COPY_COUNT
    engine.execute(plan, phone)
    assert all(d.exists() for d in dsts)


def test_undo_when_file_already_removed_by_hand(engine, synced, trash):
    rid, dsts = synced
    gone = dsts[0]
    gone.unlink()
    urid = engine.undo(rid)  # must not raise
    ops = engine.journal.ops(urid)
    skipped = [o for o in ops if o["status"] == "skipped-dup"]
    assert len(skipped) == 1
    assert skipped[0]["src"] == str(gone) and skipped[0]["note"] == "already gone"
    assert not [o for o in ops if o["status"] == "failed"]
    assert len([o for o in ops if o["op"] == "quarantine" and o["status"] == "ok"]) == GALLERY_COPY_COUNT - 1
    assert engine.journal.run(rid).undone == 1
    assert len([p for p in trash.rglob("*") if p.is_file()]) == GALLERY_COPY_COUNT - 1


def test_undo_restores_to_numbered_name_if_original_slot_is_taken(engine, synced, mkfile):
    rid, dsts = synced
    engine.undo(rid)
    target = dsts[0]
    mkfile(target, b"you put a new file here")
    urid = engine.journal.undo(engine.journal.runs()[0].id)  # undo the undo, journal level
    restored = [o for o in engine.journal.ops(urid) if o["op"] == "restore" and o["src"].endswith(target.name)]
    assert len(restored) == 1
    assert Path(restored[0]["dst"]) == target.with_name(f"{target.stem} (2){target.suffix}")
    assert target.read_bytes() == b"you put a new file here"


# ---- journal unit level -----------------------------------------------------

@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "db" / "journal.db", tmp_path / "trash")
    yield j
    j.close()


def test_unique_path_numbering(tmp_path, mkfile):
    p = tmp_path / "name.jpg"
    assert unique_path(p) == p  # free slot: unchanged
    mkfile(p)
    assert unique_path(p) == tmp_path / "name (2).jpg"
    mkfile(tmp_path / "name (2).jpg")
    assert unique_path(p) == tmp_path / "name (3).jpg"


def test_runs_ordering_newest_first(journal):
    a = journal.start_run("gallery", note="first")
    time.sleep(0.02)
    b = journal.start_run("files", note="second")
    ids = [r.id for r in journal.runs()]
    assert ids == [b, a]
    assert journal.runs(limit=1)[0].id == b
    assert journal.run(a).note == "first" and journal.run(a).status == "running"
    assert journal.run("nope") is None


def test_finish_run_aggregates_ok_copies_and_restores_only(journal):
    rid = journal.start_run("gallery")
    journal.record(rid, "mkdir", None, "X")
    journal.record(rid, "copy", "a", "A", 10, status="ok")
    journal.record(rid, "copy", "b", "B", 20, status="ok")
    journal.record(rid, "restore", "c", "C", 5, status="ok")
    journal.record(rid, "copy", "d", "D", 30, status="skipped-dup")
    journal.record(rid, "copy", "e", "E", 40, status="failed", note="boom")
    journal.finish_run(rid, "done")
    r = journal.run(rid)
    assert r.files == 3
    assert r.bytes == 35
    assert r.status == "done" and r.finished is not None and r.finished >= r.started
    seqs = [o["seq"] for o in journal.ops(rid)]
    assert seqs == [1, 2, 3, 4, 5, 6]


def test_finish_run_keeps_note_unless_given(journal):
    rid = journal.start_run("gallery", note="keep me")
    journal.finish_run(rid)
    assert journal.run(rid).note == "keep me"
    journal.finish_run(rid, "failed", note="replaced")
    assert journal.run(rid).note == "replaced" and journal.run(rid).status == "failed"


def test_quarantine_path_mirrors_path_under_trash_stamp(journal, tmp_path):
    src = tmp_path / "pc" / "Album" / "x.jpg"
    q = journal.quarantine_path(src, stamp="S")
    assert q == journal.trash / "S" / Path(*src.parts[1:])
    assert q.parent.is_dir()


def test_move_to_trash_renames_and_keeps_content(journal, tmp_path, mkfile):
    src = mkfile(tmp_path / "pc" / "Album" / "x.jpg", b"payload")
    q = journal.move_to_trash(src, stamp="S")
    assert not src.exists() and q.read_bytes() == b"payload"
    # a second file with the same original path in the same stamp gets a numbered slot
    src2 = mkfile(src, b"again")
    q2 = journal.move_to_trash(src2, stamp="S")
    assert q2.name == "x (2).jpg"


def test_run_to_dict(journal):
    rid = journal.start_run("pim", note="n")
    d = journal.run(rid).to_dict()
    assert d["id"] == rid and d["kind"] == "pim" and d["undone"] == 0


# ---- "move" op: the review page files albums into category folders ----------

def _tree(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def test_move_path_file_then_undo_brings_it_back(journal, tmp_path, mkfile):
    src = mkfile(tmp_path / "pc" / "_New from phone" / "Holiday" / "x.jpg", b"payload-x")
    dst = tmp_path / "pc" / "Content" / "Holiday" / "x.jpg"
    rid = journal.start_run("review")
    landed = journal.move_path(rid, src, dst, album="Holiday")
    assert landed == dst and dst.read_bytes() == b"payload-x" and not src.exists()
    ops = journal.ops(rid)
    assert [o["op"] for o in ops] == ["move"]
    assert ops[0]["src"] == str(src) and ops[0]["dst"] == str(dst) and ops[0]["size"] == 9 and ops[0]["album"] == "Holiday"
    journal.finish_run(rid)
    assert journal.run(rid).files == 1 and journal.run(rid).bytes == 9  # counted, so the dashboard shows Undo

    urid = journal.undo(rid)
    assert src.read_bytes() == b"payload-x" and not dst.exists()
    assert journal.run(rid).undone == 1 and journal.ops(rid)[0]["undone"] == 1
    back = journal.ops(urid)
    assert len(back) == 1 and back[0]["op"] == "move" and back[0]["src"] == str(dst) and back[0]["dst"] == str(src)
    # undo of the undo moves it forward again
    journal.undo(urid)
    assert dst.read_bytes() == b"payload-x" and not src.exists()


def test_move_path_folder_then_undo(journal, tmp_path, mkfile):
    src = tmp_path / "pc" / "_New from phone" / "Trip"
    mkfile(src / "a.jpg", b"a")
    mkfile(src / "b.mp4", b"bb")
    mkfile(src / "sub" / "c.png", b"ccc")
    dst = tmp_path / "pc" / "Trips + Places" / "Trip 2026"
    rid = journal.start_run("review")
    landed = journal.move_path(rid, src, dst, album="Trip")
    assert landed == dst and not src.exists()
    assert _tree(dst) == {"a.jpg", "b.mp4", "sub/c.png"}
    ops = journal.ops(rid)
    assert len(ops) == 1 and ops[0]["op"] == "move" and ops[0]["size"] == 6  # whole tree, one row
    journal.finish_run(rid)
    assert journal.run(rid).files == 1

    journal.undo(rid)
    assert not dst.exists() and _tree(src) == {"a.jpg", "b.mp4", "sub/c.png"}
    assert (src / "sub" / "c.png").read_bytes() == b"ccc"


def test_move_path_when_target_exists_takes_numbered_slot(journal, tmp_path, mkfile):
    rid = journal.start_run("review")
    # a file whose target name is taken
    src = mkfile(tmp_path / "new" / "x.jpg", b"new")
    taken = mkfile(tmp_path / "cat" / "x.jpg", b"old")
    landed = journal.move_path(rid, src, taken)
    assert landed == taken.with_name("x (2).jpg") and landed.read_bytes() == b"new" and taken.read_bytes() == b"old"
    # a folder whose target name is taken (folder names keep their dots: "v1.2" -> "v1.2 (2)")
    fsrc = tmp_path / "new" / "v1.2"
    mkfile(fsrc / "f.txt", b"f")
    ftaken = tmp_path / "cat" / "v1.2"
    mkfile(ftaken / "theirs.txt", b"t")
    flanded = journal.move_path(rid, fsrc, ftaken)
    assert flanded == ftaken.with_name("v1.2 (2)") and (flanded / "f.txt").exists() and (ftaken / "theirs.txt").exists()
    # undo when the OLD slot got occupied meanwhile: the file comes back beside it, nothing is overwritten
    mkfile(src, b"you wrote this")
    journal.undo(rid)
    assert src.read_bytes() == b"you wrote this"
    assert (tmp_path / "new" / "x (2).jpg").read_bytes() == b"new"
    assert not landed.exists() and taken.read_bytes() == b"old"
    assert (fsrc / "f.txt").exists() and not flanded.exists()


def test_move_path_refuses_missing_source_and_self_nesting(journal, tmp_path, mkfile):
    rid = journal.start_run("review")
    with pytest.raises(FileNotFoundError):
        journal.move_path(rid, tmp_path / "nope.jpg", tmp_path / "x.jpg")
    folder = tmp_path / "A"
    mkfile(folder / "f", b"f")
    with pytest.raises(ValueError):
        journal.move_path(rid, folder, folder / "inner")
    assert (folder / "f").exists()
    assert journal.ops(rid) == []  # nothing happened, nothing recorded


def test_undo_move_when_file_already_gone_is_skipped_not_failed(journal, tmp_path, mkfile):
    src = mkfile(tmp_path / "a" / "x.jpg", b"x")
    rid = journal.start_run("review")
    dst = journal.move_path(rid, src, tmp_path / "b" / "x.jpg")
    dst.unlink()
    urid = journal.undo(rid)
    ops = journal.ops(urid)
    assert len(ops) == 1 and ops[0]["status"] == "skipped-dup" and ops[0]["note"] == "already gone"
    assert journal.run(rid).undone == 1
