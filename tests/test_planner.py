from __future__ import annotations

from pathlib import Path

import pytest

from phonevault.core.albums import AlbumMapper
from phonevault.core.engine import Plan, PlanItem, Planner, is_noise
from phonevault.core.index import FileIndex
from phonevault.transports.base import RemoteFile


def rf(path: str, size: int = 10, mtime: float = 1.0) -> RemoteFile:
    return RemoteFile(path, path.rsplit("/", 1)[-1], size, mtime)


@pytest.fixture
def index(home):
    i = FileIndex(home / "index.db")
    yield i
    i.close()


@pytest.fixture
def planner(cfg, index, home):
    return Planner(cfg, index, AlbumMapper(cfg.root, home / "album-map.json", cfg.new_albums_subfolder, cfg.album_aliases))


def by_name(plan: Plan) -> dict[str, PlanItem]:
    return {i.remote.name: i for i in plan.items}


def test_is_noise_rules():
    assert is_noise(rf("DCIM/.thumbnails/1.jpg"))
    assert is_noise(rf("Pictures/.nomedia"))
    assert is_noise(rf("DCIM/Camera/.trashed-123-IMG.jpg"))
    assert is_noise(rf("DCIM/Camera/._IMG.jpg"))
    assert is_noise(rf("DCIM/Camera/Thumbs.db"))
    assert not is_noise(rf("DCIM/Camera/IMG_1.jpg"))


def test_gallery_skip_noise_for_thumbnails(planner):
    plan = planner.plan_gallery([rf("DCIM/.thumbnails/1.jpg")])
    assert len(plan.items) == 1
    assert plan.items[0].action == "skip-noise"
    assert plan.items[0].reason == "system file"


def test_gallery_excludes_nomedia_as_non_media_extension(planner):
    """`.nomedia` has no media suffix, so the gallery plan drops it before the noise check."""
    plan = planner.plan_gallery([rf("Pictures/.nomedia", 0)])
    assert plan.items == []


def test_files_plan_marks_nomedia_as_noise(planner):
    plan = planner.plan_files([rf("Pictures/.nomedia", 0)])
    assert len(plan.items) == 1
    assert plan.items[0].action == "skip-noise"


def test_same_name_and_size_in_the_mapped_folder_is_compared(planner, pc, mkfile):
    mkfile(pc / "Phone camera" / "IMG_1.jpg", b"x" * 10)
    item = planner.plan_gallery([rf("DCIM/Camera/IMG_1.jpg", 10)]).items[0]
    assert item.action == "check" and "in place" in item.reason
    assert Path(item.dst_folder) == pc / "Phone camera"


def test_same_name_different_size_in_mapped_folder_is_still_a_copy(planner, pc, mkfile):
    mkfile(pc / "Phone camera" / "IMG_1.jpg", b"x" * 10)
    plan = planner.plan_gallery([rf("DCIM/Camera/IMG_1.jpg", 25)])
    assert plan.items[0].action == "copy"


def test_same_name_and_size_elsewhere_on_the_pc_is_compared(planner, index, pc, mkfile):
    moved = mkfile(pc / "Sorted" / "2024" / "IMG_1.jpg", b"x" * 10)
    index.refresh(pc)
    item = planner.plan_gallery([rf("DCIM/Camera/IMG_1.jpg", 10)]).items[0]
    assert item.action == "check" and str(moved) in item.reason


def test_copy_otherwise(planner, pc):
    plan = planner.plan_gallery([rf("DCIM/Camera/IMG_1.jpg", 10)])
    item = plan.items[0]
    assert item.action == "copy"
    assert item.album == "Camera"
    assert item.how == "alias"
    assert Path(item.dst_folder) == pc / "Phone camera"


def test_skip_exists_when_phone_file_was_handled_before(planner, index, pc, mkfile):
    done = mkfile(pc / "Phone camera" / "IMG_1.jpg", b"x" * 10)
    index.mark_phone_seen("DCIM/Camera/IMG_1.jpg", 10, str(done), 1.0)
    plan = planner.plan_gallery([rf("DCIM/Camera/IMG_1.jpg", 10)])
    assert plan.items[0].action == "skip-exists"
    assert plan.items[0].reason.startswith("handled before")


def test_non_media_extensions_excluded_from_gallery_plan(planner):
    files = [rf("Pictures/Notes/todo.txt"), rf("DCIM/Camera/IMG_1.jpg"), rf("DCIM/Camera/clip.MP4")]
    plan = planner.plan_gallery(files)
    assert sorted(by_name(plan)) == ["IMG_1.jpg", "clip.MP4"]


def test_gallery_media_only_false_keeps_everything(planner):
    plan = planner.plan_gallery([rf("Pictures/Notes/todo.txt")], media_only=False)
    assert [i.remote.name for i in plan.items] == ["todo.txt"]


def test_gallery_skips_directories(planner):
    d = RemoteFile("DCIM/Camera", "Camera", 0, None, is_dir=True)
    assert planner.plan_gallery([d]).items == []


def test_file_directly_in_gallery_root_uses_root_as_album(planner, pc):
    plan = planner.plan_gallery([rf("DCIM/IMG_root.jpg")])
    item = plan.items[0]
    assert item.album == "DCIM"
    assert Path(item.dst_folder) == pc / "_New from phone" / "DCIM"


def test_plan_files_mirrors_folder_structure_under_phone_files(planner, pc):
    plan = planner.plan_files([rf("Download/docs/report.pdf"), rf("Download/readme.txt"), rf("Documents/a.docx")])
    items = by_name(plan)
    assert Path(items["report.pdf"].dst_folder) == pc / "Phone files" / "Download" / "docs"
    assert Path(items["readme.txt"].dst_folder) == pc / "Phone files" / "Download"
    assert Path(items["a.docx"].dst_folder) == pc / "Phone files" / "Documents"
    assert items["report.pdf"].album == "Download"
    assert items["a.docx"].album == "Documents"
    assert all(i.how == "mirror" and i.action == "copy" for i in plan.items)
    assert plan.kind == "files"


def test_plan_files_with_subfolder_uses_it_as_album(planner, pc):
    plan = planner.plan_files([rf("OldBackups/apps/x.sb")], subfolder="Phone app data")
    item = plan.items[0]
    assert item.album == "Phone app data"
    assert Path(item.dst_folder) == pc / "Phone app data" / "OldBackups" / "apps"


def test_plain_files_are_compared_too(planner, index, pc, mkfile):
    mkfile(pc / "Sorted" / "readme.txt", b"x" * 10)
    index.refresh(pc)
    assert planner.plan_files([rf("Download/readme.txt", 10)]).items[0].action == "check"


def test_plan_from_local_transport_tree(planner, phone, pc):
    plan = planner.plan_gallery(phone.list_tree("DCIM") + phone.list_tree("Pictures"))
    items = by_name(plan)
    assert "todo.txt" not in items and ".nomedia" not in items
    assert items["1.jpg"].action == "skip-noise"
    assert Path(items["IMG_0001.jpg"].dst_folder) == pc / "Phone camera"
    assert Path(items["Screenshot_1.png"].dst_folder) == pc / "Phone screenshots"
    assert Path(items["logo.png"].dst_folder) == pc / "PROJECTS" / "garden plans"
    assert Path(items["d1.jpg"].dst_folder) == pc / "PROJECTS" / "kitchen renovation sketches"
    assert Path(items["r1.jpg"].dst_folder) == pc / "_New from phone" / "Random Stuff"


def test_plan_summary_numbers_add_up(planner, index, pc, mkfile):
    mkfile(pc / "Phone camera" / "exists.jpg", b"x" * 5)
    mkfile(pc / "Sorted" / "moved.jpg", b"x" * 7)
    index.refresh(pc)
    files = [
        rf("DCIM/Camera/new1.jpg", 100),
        rf("DCIM/Camera/new2.jpg", 200),
        rf("DCIM/Camera/exists.jpg", 5),
        rf("DCIM/Camera/moved.jpg", 7),
        rf("DCIM/.thumbnails/t.jpg", 1),
        rf("Pictures/Screenshots/s.png", 50),
    ]
    plan = planner.plan_gallery(files)
    s = plan.summary()
    assert s["kind"] == "gallery"
    assert s["files"] == 6
    assert s["copy"] == 3 and s["check"] == 2
    assert s["bytes"] == 350
    per_album = {a["album"]: a for a in s["albums"]}
    cam = per_album["Camera"]
    assert (cam["copy"], cam["exists"], cam["check"], cam["noise"], cam["bytes"]) == (2, 0, 2, 0, 300)
    assert per_album[".thumbnails"]["noise"] == 1
    assert per_album["Screenshots"]["copy"] == 1
    assert sum(a["copy"] + a["exists"] + a["check"] + a["noise"] for a in s["albums"]) == s["files"]
    assert sum(a["bytes"] for a in s["albums"]) == s["bytes"]
    # sorted by most copies first
    assert s["albums"][0]["album"] == "Camera"


def test_plan_item_to_dict_is_json_friendly(planner):
    import json
    plan = planner.plan_gallery([rf("DCIM/Camera/IMG_1.jpg")])
    d = plan.items[0].to_dict()
    json.dumps(d)
    assert d["remote"]["path"] == "DCIM/Camera/IMG_1.jpg"
    assert d["action"] == "copy"



def test_extdata_filter_drops_caches_and_noisy_packages():
    from phonevault.modules.media import extdata_filter
    from phonevault.transports.base import RemoteFile
    mk = lambda p: RemoteFile(p, p.rsplit("/", 1)[-1], 10, None)
    files = [mk("Android/media/com.whatsapp.w4b/WhatsApp/Databases/msgstore.db.crypt14"),
             mk("Android/data/com.app/files/save.dat"),
             mk("Android/data/com.app/cache/junk.bin"),
             mk("Android/data/com.app/files/cache/deep.bin"),
             mk("Android/data/com.example.gallery/files/x.jpg"),
             mk("Android/obb/com.game/main.obb"),
             mk("Download/not-android.txt")]
    kept = [f.path for f in extdata_filter(files, {"com.example.gallery"})]
    assert kept == ["Android/media/com.whatsapp.w4b/WhatsApp/Databases/msgstore.db.crypt14",
                    "Android/data/com.app/files/save.dat", "Android/obb/com.game/main.obb"]


def test_documents_inside_album_folders_are_mirrored_not_albums(engine, tmp_path):
    from phonevault.modules import media
    from phonevault.transports.local import LocalTransport
    root = tmp_path / "phone-docs"
    (root / "Pictures/House/Roof").mkdir(parents=True)
    (root / "Pictures/House/Roof/receipt.pdf").write_bytes(b"P" * 500)
    (root / "Pictures/House/Roof/photo.jpg").write_bytes(b"J" * 500)
    tr = LocalTransport(root)
    plan = media.plan_gallery(engine, tr, ["Pictures"])
    by_name = {i.remote.name: i for i in plan.items}
    assert "Phone files" in by_name["receipt.pdf"].dst_folder and by_name["receipt.pdf"].dst_folder.endswith("Roof")
    assert "_New from phone" in by_name["photo.jpg"].dst_folder
