from __future__ import annotations

import hashlib

import pytest

from phonevault.core import index as index_mod
from phonevault.core.index import FileIndex, folder_names


@pytest.fixture
def idx(tmp_path):
    i = FileIndex(tmp_path / "db" / "index.db")
    yield i
    i.close()


@pytest.fixture
def root(tmp_path, mkfile):
    r = tmp_path / "root"
    mkfile(r / "a.jpg", b"aaa")
    mkfile(r / "sub" / "b.jpg", b"bbbb")
    mkfile(r / "sub" / "deeper" / "c.mp4", b"ccccc")
    return r


def test_refresh_counts_first_run(idx, root):
    stats = idx.refresh(root)
    assert stats == {"seen": 3, "added": 3, "changed": 0, "removed": 0}
    assert idx.count() == 3


def test_refresh_counts_added_and_removed(idx, root, mkfile):
    idx.refresh(root)
    (root / "a.jpg").unlink()
    mkfile(root / "new.png", b"n")
    stats = idx.refresh(root)
    assert stats["seen"] == 3
    assert stats["added"] == 1
    assert stats["removed"] == 1
    assert idx.has_name_size("a.jpg", 3) == []
    assert idx.has_name_size("new.png", 1)


def test_refresh_does_not_rehash_unchanged_files(idx, root, monkeypatch):
    idx.refresh(root)
    quick_before = {r[0]: r[1] for r in idx.con.execute("SELECT path, quick FROM files")}
    assert all(quick_before.values())

    calls: list[str] = []
    real = index_mod.quick_hash

    def counting(p, size=None):
        calls.append(str(p))
        return real(p, size)

    monkeypatch.setattr(index_mod, "quick_hash", counting)
    stats = idx.refresh(root)
    assert calls == []
    assert stats["added"] == 0 and stats["changed"] == 0 and stats["seen"] == 3
    quick_after = {r[0]: r[1] for r in idx.con.execute("SELECT path, quick FROM files")}
    assert quick_after == quick_before


def test_refresh_rehashes_a_changed_file(idx, root, mkfile):
    idx.refresh(root)
    old_quick = idx.con.execute("SELECT quick FROM files WHERE name='a.jpg'").fetchone()[0]
    mkfile(root / "a.jpg", b"different and longer")
    stats = idx.refresh(root)
    assert stats["changed"] == 1
    new_quick = idx.con.execute("SELECT quick FROM files WHERE name='a.jpg'").fetchone()[0]
    assert new_quick != old_quick


def test_refresh_skips_noise_names_and_folders(idx, root, mkfile):
    mkfile(root / "Thumbs.db", b"t")
    mkfile(root / ".thumbnails" / "x.jpg", b"x")
    mkfile(root / ".phonevault-staging" / "y.jpg", b"y")
    stats = idx.refresh(root)
    assert stats["seen"] == 3
    assert idx.has_name_size("Thumbs.db", 1) == []
    assert idx.has_name_size("x.jpg", 1) == []


def test_has_name_size_and_same_size(idx, root):
    idx.refresh(root)
    hits = idx.has_name_size("b.jpg", 4)
    assert len(hits) == 1 and hits[0].endswith("b.jpg")
    assert idx.has_name_size("b.jpg", 999) == []
    assert idx.has_name_size("nope.jpg", 4) == []
    assert [p for p in idx.same_size(4)] == hits


def test_find_sha_empty_until_ensure_sha(idx, root):
    idx.refresh(root)
    path = idx.has_name_size("a.jpg", 3)[0]
    expected = hashlib.sha256(b"aaa").hexdigest()
    assert idx.find_sha(expected) == []
    assert idx.ensure_sha(path) == expected
    assert idx.find_sha(expected) == [path]


def test_ensure_sha_is_cached_after_first_call(idx, root, monkeypatch):
    idx.refresh(root)
    path = idx.has_name_size("a.jpg", 3)[0]
    first = idx.ensure_sha(path)
    monkeypatch.setattr(index_mod, "sha256", lambda p: pytest.fail("sha256 recomputed"))
    assert idx.ensure_sha(path) == first


def test_add_file_and_remove_file(idx, root):
    p = root / "a.jpg"
    idx.add_file(p, "deadbeef")
    assert idx.find_sha("deadbeef") == [str(p)]
    assert idx.has_name_size("a.jpg", 3) == [str(p)]
    idx.remove_file(p)
    assert idx.find_sha("deadbeef") == []
    assert idx.count() == 0


def test_phone_seen_round_trip(idx, root):
    pc_file = root / "a.jpg"
    idx.mark_phone_seen("DCIM/Camera/IMG_1.jpg", 3, str(pc_file))
    assert idx.phone_seen("DCIM/Camera/IMG_1.jpg", 3) == str(pc_file)
    # a different size is a different file
    assert idx.phone_seen("DCIM/Camera/IMG_1.jpg", 4) is None
    assert idx.phone_seen("DCIM/Camera/other.jpg", 3) is None


def test_phone_seen_returns_none_when_pc_copy_is_gone(idx, root):
    pc_file = root / "a.jpg"
    idx.mark_phone_seen("DCIM/Camera/IMG_1.jpg", 3, str(pc_file))
    pc_file.unlink()
    assert idx.phone_seen("DCIM/Camera/IMG_1.jpg", 3) is None
    # and the stale row is dropped, so a later re-copy is possible
    assert idx.con.execute("SELECT COUNT(*) FROM phone_seen").fetchone()[0] == 0


def test_phone_seen_without_a_pc_copy_is_not_seen(idx):
    idx.mark_phone_seen("DCIM/Camera/dup.jpg", 10, None)
    assert idx.phone_seen("DCIM/Camera/dup.jpg", 10) is None


def test_forget_phone_seen_for(idx, root):
    pc_file = root / "a.jpg"
    idx.mark_phone_seen("DCIM/Camera/IMG_1.jpg", 3, str(pc_file))
    idx.forget_phone_seen_for(str(pc_file))
    assert idx.phone_seen("DCIM/Camera/IMG_1.jpg", 3) is None


def test_folder_names_lists_nested_folders(root, mkfile):
    mkfile(root / ".thumbnails" / "x.jpg", b"x")
    names = folder_names(root)
    by_name = {n: p for n, p in names}
    assert set(by_name) == {"sub", "deeper"}
    assert by_name["deeper"].endswith("deeper")
    assert ".thumbnails" not in by_name
