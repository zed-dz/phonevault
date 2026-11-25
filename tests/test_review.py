"""The review page: list "_New from phone" albums, thumbnails, file them into category folders, undo.

Runs the FastAPI app in-process with Starlette's TestClient against a tmp pc_root.
Nothing here touches a phone, adb, MTP or the real dashboard port.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from phonevault.core import thumbs

NEW = "_New from phone"
JPEG_MAGIC = b"\xff\xd8\xff"


def jpg(path: Path, colour=(40, 120, 200), size=(96, 64), mtime: float | None = None) -> Path:
    """An abstract colour-block image, no people, no animals."""
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", size, colour)
    for x in range(0, size[0], 16):
        for y in range(0, size[1], 16):
            if (x // 16 + y // 16) % 2:
                im.paste((colour[2], colour[0], colour[1]), (x, y, x + 8, y + 8))
    im.save(path, "PNG" if path.suffix.lower() == ".png" else "JPEG")
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


def wait_idle(engine, timeout: float = 10.0) -> None:
    t0 = time.time()
    while engine.busy.locked():
        if time.time() - t0 > timeout:
            raise AssertionError("worker never finished")
        time.sleep(0.02)
    time.sleep(0.05)


def tree(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def srv(engine, cfg, pc, mkfile, monkeypatch):
    """The app wired to this test's engine + tmp pc_root, plus a populated _New from phone."""
    import phonevault.server.app as app_mod

    monkeypatch.setattr(app_mod, "engine", engine)
    monkeypatch.setattr(app_mod, "cfg", cfg)
    for d in ("Content", "Trips + Places", "Phone files", "Phone apps", "Phone settings", "Phone app data", ".hidden"):
        (pc / d).mkdir(parents=True, exist_ok=True)
    jpg(pc / NEW / "Holiday" / "h1.jpg", (200, 60, 60), mtime=1_700_000_000)
    jpg(pc / NEW / "Holiday" / "h2.jpg", (60, 200, 60), mtime=1_700_000_100)
    jpg(pc / NEW / "Holiday" / "h3.png", (60, 60, 200), mtime=1_700_000_200)
    mkfile(pc / NEW / "Holiday" / "clip.mp4", b"not really a video" * 50, mtime=1_700_000_300)
    jpg(pc / NEW / "Receipts" / "r1.jpg", (120, 120, 120))
    jpg(pc / NEW / "Receipts" / "r2.jpg", (90, 90, 90))
    jpg(pc / NEW / "loose1.jpg", (10, 200, 200))
    jpg(pc / NEW / "loose2.jpg", (200, 10, 200))
    jpg(pc / "Content" / "existing.jpg", (1, 2, 3))
    engine.mapper.set("Pictures/Holiday", str(pc / NEW / "Holiday"))
    engine.mapper.set("Receipts", str(pc / NEW / "Receipts"))
    engine.index.refresh(pc)
    cfg.api_token = "review-test-token"
    client = TestClient(app_mod.app, client=("127.0.0.1", 50000), headers={"X-PhoneVault-Token": cfg.api_token})
    client.mod = app_mod  # type: ignore[attr-defined]
    return client


# ---- listing -------------------------------------------------------------------

def test_review_lists_albums_categories_and_loose_files(srv, pc):
    d = srv.get("/api/review").json()
    assert d["new_folder"] == NEW
    # categories = your top-level folders only: no tool folders, no dot folders
    assert "Content" in d["categories"] and "Trips + Places" in d["categories"] and "PROJECTS" in d["categories"]
    for tool in (NEW, "Phone files", "Phone apps", "Phone settings", "Phone app data", ".hidden"):
        assert tool not in d["categories"]
    assert d["categories"] == sorted(d["categories"], key=str.casefold)
    by = {a["name"]: a for a in d["albums"]}
    assert set(by) == {"Holiday", "Receipts", "(loose files)"}
    h = by["Holiday"]
    assert h["files"] == 4 and h["bytes"] > 0 and abs(h["newest_mtime"] - 1_700_000_300) < 2
    assert h["sample"][0] == f"{NEW}/Holiday/clip.mp4"  # newest first
    assert len(h["sample"]) == 4 and h["sample_files"][0]["is_video"] is True
    assert by["(loose files)"]["files"] == 2 and by["(loose files)"]["loose"] is True
    assert sorted(by["(loose files)"]["sample"]) == [f"{NEW}/loose1.jpg", f"{NEW}/loose2.jpg"]


def test_review_sample_caps_at_12(srv, pc):
    for i in range(15):
        jpg(pc / NEW / "Big" / f"b{i:02d}.jpg", (i * 10, 50, 50), mtime=1_600_000_000 + i)
    a = next(x for x in srv.get("/api/review").json()["albums"] if x["name"] == "Big")
    assert a["files"] == 15 and len(a["sample"]) == 12 and a["sample"][0].endswith("b14.jpg")


def test_review_files_paged(srv, pc):
    for i in range(7):
        jpg(pc / NEW / "Big" / f"b{i}.jpg", (i * 30, 50, 50), mtime=1_600_000_000 + i)
    p1 = srv.get("/api/review/files", params={"album": "Big", "offset": 0, "limit": 3}).json()
    p2 = srv.get("/api/review/files", params={"album": "Big", "offset": 3, "limit": 3}).json()
    p3 = srv.get("/api/review/files", params={"album": "Big", "offset": 6, "limit": 3}).json()
    assert p1["total"] == 7 and [f["name"] for f in p1["files"]] == ["b6.jpg", "b5.jpg", "b4.jpg"]
    assert [f["name"] for f in p2["files"]] == ["b3.jpg", "b2.jpg", "b1.jpg"] and [f["name"] for f in p3["files"]] == ["b0.jpg"]
    f = p1["files"][0]
    assert f["rel"] == f"{NEW}/Big/b6.jpg" and f["size"] > 0 and f["is_video"] is False
    assert srv.get("/api/review/files", params={"album": "(loose files)"}).json()["total"] == 2
    assert srv.get("/api/review/files", params={"album": "Nope"}).status_code == 404
    assert srv.get("/api/review/files", params={"album": "../Content"}).status_code == 400


def test_review_empty_root(srv, pc):
    shutil.rmtree(pc / NEW)
    d = srv.get("/api/review").json()
    assert d["albums"] == [] and "Content" in d["categories"]


# ---- thumbs + files ----------------------------------------------------------------

def test_thumb_route_serves_jpeg_and_blocks_escapes(srv, pc, home):
    r = srv.get("/thumb", params={"rel": f"{NEW}/Holiday/h1.jpg"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:3] == JPEG_MAGIC
    with Image.open(__import__("io").BytesIO(r.content)) as im:
        assert max(im.size) <= 256
    assert list((home / "thumbs").glob("*.jpg")), "thumbnail was cached under app_dir()/thumbs"
    # a video with no decodable frame still gets a JPEG (placeholder)
    r = srv.get("/thumb", params={"rel": f"{NEW}/Holiday/clip.mp4"})
    assert r.status_code == 200 and r.content[:3] == JPEG_MAGIC
    # outside the root, absolute, folder, missing: all 404
    for bad in ("../x.jpg", f"{NEW}/../../x.jpg", "C:/Windows/notepad.exe", "/etc/passwd", f"{NEW}/Holiday", f"{NEW}/Holiday/none.jpg", ""):
        assert srv.get("/thumb", params={"rel": bad}).status_code == 404, bad
    # a path with backslashes is accepted (Windows-style)
    assert srv.get("/thumb", params={"rel": f"{NEW}\\Holiday\\h2.jpg"}).status_code == 200


def test_file_route_streams_the_original(srv, pc):
    want = (pc / NEW / "Holiday" / "h3.png").read_bytes()
    r = srv.get("/file", params={"rel": f"{NEW}/Holiday/h3.png"})
    assert r.status_code == 200 and r.content == want and r.headers["content-type"].startswith("image/png")
    assert "inline" in r.headers.get("content-disposition", "")
    assert srv.get("/file", params={"rel": "../../secret.txt"}).status_code == 404


def test_thumbs_placeholder_never_raises(tmp_path, home, monkeypatch, mkfile):
    monkeypatch.setattr(thumbs.shutil, "which", lambda name: None)  # no ffmpeg: video -> placeholder
    v = mkfile(tmp_path / "v.mp4", b"garbage" * 10)
    data = thumbs.thumb(v)
    assert data[:3] == JPEG_MAGIC
    with Image.open(__import__("io").BytesIO(data)) as im:
        assert im.size == (256, 256)
    assert thumbs.thumb(v, 64) == thumbs.thumb(v, 64)  # second call served from the cache
    # a broken image, a missing file and a weird size all answer with a JPEG
    broken = mkfile(tmp_path / "broken.jpg", b"\xff\xd8 not an image")
    assert thumbs.thumb(broken)[:3] == JPEG_MAGIC
    assert thumbs.thumb(tmp_path / "missing.heic")[:3] == JPEG_MAGIC
    assert thumbs.thumb(jpg(tmp_path / "ok.png"), 48)[:3] == JPEG_MAGIC
    assert thumbs.placeholder(".MOV", 128)[:3] == JPEG_MAGIC
    cached = list((home / "thumbs").glob("*.jpg"))
    assert cached and all(c.stat().st_size > 0 for c in cached)


def test_thumbs_cache_key_changes_with_mtime(tmp_path):
    p = tmp_path / "a.jpg"
    assert thumbs.cache_key(p, 1.0, 10) != thumbs.cache_key(p, 2.0, 10)
    assert thumbs.cache_key(p, 1.0, 10) == thumbs.cache_key(p, 1.0, 10)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not on PATH")
def test_thumbs_video_frame_with_ffmpeg(tmp_path, home):
    import subprocess
    clip = tmp_path / "pattern.mp4"
    # a synthetic colour-bar clip: no camera, no people
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10", "-t", "2",
                    "-pix_fmt", "yuv420p", str(clip)], check=True, capture_output=True, timeout=60)
    data = thumbs.thumb(clip, 128)
    assert data[:3] == JPEG_MAGIC
    with Image.open(__import__("io").BytesIO(data)) as im:
        assert im.size[0] == 128 and im.size[1] < 128   # a real 4:3 frame, not the square placeholder
    assert data != thumbs.placeholder(".mp4", 128)


# ---- filing albums -----------------------------------------------------------------

def test_move_album_to_category_then_undo(srv, pc, engine):
    old, new = pc / NEW / "Holiday", pc / "Content" / "Holiday"
    r = srv.post("/api/review/move", json={"album": "Holiday", "category": "Content"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] and d["moved"] == 4 and d["dst"] == str(new) and d["rel"] == "Content/Holiday"
    assert not old.exists() and tree(new) == {"h1.jpg", "h2.jpg", "h3.png", "clip.mp4"}
    # one journaled run, one move op, counted so the dashboard shows the Undo button
    run = engine.journal.run(d["run_id"])
    assert run.kind == "review" and run.status == "done" and run.files == 1 and run.bytes > 0 and "Holiday → Content/Holiday" in run.note
    ops = engine.journal.ops(d["run_id"])
    assert len(ops) == 1 and ops[0]["op"] == "move" and ops[0]["src"] == str(old) and ops[0]["dst"] == str(new)
    # the album map follows the folder
    assert engine.mapper.saved["Pictures/Holiday"] == str(new)
    assert engine.mapper.saved["Receipts"] == str(pc / NEW / "Receipts")
    # the index knows the new paths and forgot the old ones
    assert engine.index.has_name_size("h1.jpg", (new / "h1.jpg").stat().st_size) == [str(new / "h1.jpg")]
    assert not [p for p in engine.index.con.execute("SELECT path FROM files") if str(old) in p[0]]
    # the review list no longer shows it
    assert "Holiday" not in {a["name"] for a in srv.get("/api/review").json()["albums"]}

    # undo through the same job the dashboard button uses
    srv.mod._undo_job(d["run_id"])
    assert tree(old) == {"h1.jpg", "h2.jpg", "h3.png", "clip.mp4"} and not new.exists()
    assert engine.mapper.saved["Pictures/Holiday"] == str(old)
    assert engine.index.has_name_size("h1.jpg", (old / "h1.jpg").stat().st_size) == [str(old / "h1.jpg")]
    assert engine.journal.run(d["run_id"]).undone == 1
    assert "Holiday" in {a["name"] for a in srv.get("/api/review").json()["albums"]}


def test_undo_via_api_endpoint(srv, pc, engine):
    rid = srv.post("/api/review/move", json={"album": "Receipts", "category": "PROJECTS", "new_name": "receipts 2026"}).json()["run_id"]
    assert (pc / "PROJECTS" / "receipts 2026" / "r1.jpg").exists()
    r = srv.post(f"/api/undo/{rid}")
    assert r.status_code == 200
    wait_idle(engine)
    assert (pc / NEW / "Receipts" / "r1.jpg").exists() and not (pc / "PROJECTS" / "receipts 2026").exists()
    assert engine.mapper.saved["Receipts"] == str(pc / NEW / "Receipts")
    assert srv.post(f"/api/undo/{rid}").status_code == 400  # already undone


def test_rename_album_in_place(srv, pc, engine):
    r = srv.post("/api/review/move", json={"album": "Holiday", "category": NEW, "new_name": "Holiday 2026"})
    assert r.status_code == 200, r.text
    assert (pc / NEW / "Holiday 2026" / "h1.jpg").exists() and not (pc / NEW / "Holiday").exists()
    assert engine.mapper.saved["Pictures/Holiday"] == str(pc / NEW / "Holiday 2026")
    # same place, same name = nothing to do
    assert srv.post("/api/review/move", json={"album": "Holiday 2026", "category": NEW}).status_code == 400


def test_move_without_merge_numbers_the_folder_when_target_exists(srv, pc):
    jpg(pc / "Content" / "Holiday" / "theirs.jpg", (5, 5, 5))
    d = srv.post("/api/review/move", json={"album": "Holiday", "category": "Content"}).json()
    assert d["dst"] == str(pc / "Content" / "Holiday (2)") and d["merged"] is False
    assert (pc / "Content" / "Holiday (2)" / "h1.jpg").exists() and (pc / "Content" / "Holiday" / "theirs.jpg").exists()


def test_move_with_merge_puts_files_into_existing_folder_then_undo(srv, pc, engine):
    target = pc / "Content" / "Holiday"
    jpg(target / "theirs.jpg", (5, 5, 5))
    jpg(target / "h1.jpg", (6, 6, 6))  # name clash: ours lands as "h1 (2).jpg"
    d = srv.post("/api/review/move", json={"album": "Holiday", "category": "Content", "merge": True}).json()
    assert d["merged"] is True and d["moved"] == 4
    assert tree(target) == {"theirs.jpg", "h1.jpg", "h1 (2).jpg", "h2.jpg", "h3.png", "clip.mp4"}
    assert not (pc / NEW / "Holiday").exists()
    ops = engine.journal.ops(d["run_id"])
    assert [o["op"] for o in ops] == ["move"] * 4 + ["rmdir"]
    assert engine.journal.run(d["run_id"]).files == 4
    assert engine.mapper.saved["Pictures/Holiday"] == str(target)

    srv.mod._undo_job(d["run_id"])
    assert tree(pc / NEW / "Holiday") == {"h1.jpg", "h2.jpg", "h3.png", "clip.mp4"}
    assert tree(target) == {"theirs.jpg", "h1.jpg"}
    assert (target / "h1.jpg").stat().st_size == jpg(pc / "tmp.jpg", (6, 6, 6)).stat().st_size  # theirs untouched
    assert engine.mapper.saved["Pictures/Holiday"] == str(pc / NEW / "Holiday")


def test_loose_files_need_a_name_and_move_one_by_one(srv, pc, engine):
    assert srv.post("/api/review/move", json={"album": "(loose files)", "category": "Content"}).status_code == 400
    d = srv.post("/api/review/move", json={"album": "(loose files)", "category": "Content", "new_name": "Odds"}).json()
    assert d["moved"] == 2 and tree(pc / "Content" / "Odds") == {"loose1.jpg", "loose2.jpg"}
    assert (pc / NEW).is_dir() and (pc / NEW / "Holiday").is_dir()   # the review root itself never moves
    assert "(loose files)" not in {a["name"] for a in srv.get("/api/review").json()["albums"]}
    srv.mod._undo_job(d["run_id"])
    assert (pc / NEW / "loose1.jpg").exists() and not (pc / "Content" / "Odds" / "loose1.jpg").exists()


def test_move_selected_files(srv, pc, engine):
    body = {"album": "Holiday", "files": [f"{NEW}/Holiday/h1.jpg", f"{NEW}/Holiday/clip.mp4"], "category": "Trips + Places"}
    d = srv.post("/api/review/move-files", json=body).json()
    assert d["moved"] == 2 and d["album_emptied"] is False
    assert tree(pc / "Trips + Places" / "Holiday") == {"h1.jpg", "clip.mp4"}
    assert tree(pc / NEW / "Holiday") == {"h2.jpg", "h3.png"}
    assert engine.mapper.saved["Pictures/Holiday"] == str(pc / NEW / "Holiday")  # album still there: map untouched
    ops = engine.journal.ops(d["run_id"])
    assert [o["op"] for o in ops] == ["move", "move"] and engine.journal.run(d["run_id"]).files == 2
    # the rest, into a renamed folder: album is now empty -> removed, map follows
    d2 = srv.post("/api/review/move-files", json={"album": "Holiday", "files": [f"{NEW}/Holiday/h2.jpg", f"{NEW}/Holiday/h3.png"],
                                                  "category": "Content", "new_name": "Holiday rest"}).json()
    assert d2["album_emptied"] is True and not (pc / NEW / "Holiday").exists()
    assert engine.mapper.saved["Pictures/Holiday"] == str(pc / "Content" / "Holiday rest")
    srv.mod._undo_job(d2["run_id"])
    assert tree(pc / NEW / "Holiday") == {"h2.jpg", "h3.png"}
    assert engine.mapper.saved["Pictures/Holiday"] == str(pc / NEW / "Holiday")


def test_move_files_rejects_files_outside_the_album(srv, pc):
    r = srv.post("/api/review/move-files", json={"album": "Holiday", "files": [f"{NEW}/Receipts/r1.jpg"], "category": "Content"})
    assert r.status_code == 400
    r = srv.post("/api/review/move-files", json={"album": "Holiday", "files": ["../../x"], "category": "Content"})
    assert r.status_code == 404
    r = srv.post("/api/review/move-files", json={"album": "Holiday", "files": [], "category": "Content"})
    assert r.status_code == 400
    assert (pc / NEW / "Receipts" / "r1.jpg").exists()


def test_move_validation(srv, pc):
    assert srv.post("/api/review/move", json={"album": "Holiday", "category": "Phone files"}).status_code == 400   # tool folder
    assert srv.post("/api/review/move", json={"album": "Holiday", "category": "Nope"}).status_code == 400
    assert srv.post("/api/review/move", json={"album": "Nope", "category": "Content"}).status_code == 404
    assert srv.post("/api/review/move", json={"album": "../Content", "category": "Content"}).status_code == 400
    assert srv.post("/api/review/move", json={"album": "Holiday", "category": "Content", "new_name": "a/b"}).status_code == 400
    assert srv.post("/api/review/move", json={"album": "Holiday", "category": "Content", "new_name": ".."}).status_code == 400
    assert (pc / NEW / "Holiday" / "h1.jpg").exists()


def test_move_is_refused_while_a_job_runs(srv, pc, engine):
    assert engine.busy.acquire(blocking=False)
    try:
        r = srv.post("/api/review/move", json={"album": "Holiday", "category": "Content"})
        assert r.status_code == 409
        r = srv.post("/api/review/move-files", json={"album": "Holiday", "files": [f"{NEW}/Holiday/h1.jpg"], "category": "Content"})
        assert r.status_code == 409
    finally:
        engine.busy.release()
    assert (pc / NEW / "Holiday" / "h1.jpg").exists()
    assert not engine.busy.locked()
    assert srv.post("/api/review/move", json={"album": "Holiday", "category": "Content"}).status_code == 200
    assert not engine.busy.locked()


def test_phone_seen_follows_the_move(srv, pc, engine):
    """A phone file resolved to the album must stay 'handled' after filing, so the next sync does not copy it again."""
    engine.index.mark_phone_seen("Pictures/Holiday/h1.jpg", (pc / NEW / "Holiday" / "h1.jpg").stat().st_size, str(pc / NEW / "Holiday" / "h1.jpg"))
    srv.post("/api/review/move", json={"album": "Holiday", "category": "Content"})
    size = (pc / "Content" / "Holiday" / "h1.jpg").stat().st_size
    assert engine.index.phone_seen("Pictures/Holiday/h1.jpg", size) == str(pc / "Content" / "Holiday" / "h1.jpg")
