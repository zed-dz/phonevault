"""The promises in the README, end to end: same bytes, nothing hard-deleted, undo works, one writer, hostile names."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import PHONE_TREE, PROJECT_ROOT, write
from phonevault.core.config import Config
from phonevault.core.engine import Engine
from phonevault.core.names import inside, is_safe_rel, pc_name, pc_rel
from phonevault.modules import media
from phonevault.transports.base import RemoteFile
from phonevault.transports.local import LocalTransport


def digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def content(root: Path) -> dict[str, str]:
    """relative path -> hash, for every file that is not PhoneVault's own."""
    return {p.relative_to(root).as_posix(): digest(p) for p in root.rglob("*") if p.is_file() and ".phonevault" not in p.parts}


# ---- same bytes, undo, undo the undo ---------------------------------------------

def test_every_backed_up_file_has_the_same_bytes_as_on_the_phone(engine, phone, phone_root):
    for what, roots in (("gallery", None), ("files", ["Download"])):
        _, rid = media.sync(engine, phone, what, roots=roots)
        ops = [o for o in engine.journal.ops(rid) if o["op"] == "copy"]
        assert ops and all(o["status"] == "ok" for o in ops)
        for o in ops:
            assert Path(o["dst"]).read_bytes() == (phone_root / o["src"]).read_bytes()
            assert o["sha"] == digest(phone_root / o["src"])


def test_undo_gives_the_pc_folder_back_exactly_and_deletes_nothing(engine, phone, phone_root, pc, trash):
    write(pc / "PROJECTS" / "mine.jpg", b"already here")
    before_pc, before_phone = content(pc), content(phone_root)

    _, rid = media.sync(engine, phone, "gallery")
    after_sync = content(pc)
    copied = {k: v for k, v in after_sync.items() if k not in before_pc}
    assert len(copied) >= 8 and before_pc.items() <= after_sync.items()

    undo_id = engine.undo(rid)
    assert content(pc) == before_pc
    assert sorted(digest(p) for p in trash.rglob("*") if p.is_file()) == sorted(copied.values())   # every byte is in the trash

    engine.undo(undo_id)                      # undo the undo
    assert content(pc) == after_sync
    assert not [p for p in trash.rglob("*") if p.is_file()]
    assert content(phone_root) == before_phone                # the phone was only read


def test_a_failed_undo_can_be_finished_later(engine, phone, pc, trash, monkeypatch):
    _, rid = media.sync(engine, phone, "gallery")
    from phonevault.core import journal as jmod
    real = jmod._rename

    def stuck(src, dst):
        if Path(src).name == "IMG_0001.jpg":
            raise OSError("file in use")
        return real(src, dst)

    monkeypatch.setattr(jmod, "_rename", stuck)
    urid = engine.undo(rid)
    assert engine.journal.run(urid).status == "partial" and engine.journal.run(rid).undone == 0
    assert list(pc.rglob("IMG_0001.jpg"))
    monkeypatch.setattr(jmod, "_rename", real)
    engine.undo(rid)
    assert engine.journal.run(rid).undone == 1 and not list(pc.rglob("IMG_0001.jpg"))
    assert len([p for p in trash.rglob("*") if p.is_file()]) == 8


def test_undo_works_with_the_default_trash_inside_the_pc_folder(pc, home, phone):
    e = Engine(Config(pc_root=str(pc)))
    try:
        before = content(pc)
        _, rid = media.sync(e, phone, "gallery")
        undo_id = e.undo(rid)
        assert content(pc) == before
        kept = [p for p in (pc / ".phonevault" / "trash").rglob("*") if p.is_file()]
        assert len(kept) == 8
        # the trash is not counted as "already on the PC": the next sync copies again
        plan, _ = media.sync(e, phone, "gallery")
        assert len(plan.to_copy) == 8
        assert str(kept[0]).count(pc.name) == 1, "short path: the PC folder is not repeated inside the trash"
        assert undo_id
    finally:
        e.close()


def test_a_clash_never_overwrites(engine, phone, pc):
    mine = write(pc / "Phone camera" / "IMG_0001.jpg", b"a different picture")
    media.sync(engine, phone, "gallery")
    assert mine.read_bytes() == b"a different picture"
    assert (pc / "Phone camera" / "IMG_0001 (2).jpg").read_bytes() == PHONE_TREE["DCIM/Camera/IMG_0001.jpg"]


def test_a_missing_pc_folder_stops_the_run(engine, phone, pc):
    media.sync(engine, phone, "gallery")
    pc.rename(pc.parent / "unplugged")
    with pytest.raises(RuntimeError, match="missing"):
        media.sync(engine, phone, "gallery")
    assert engine.index.count() >= 8


# ---- one writer --------------------------------------------------------------------

def test_a_second_process_may_look_but_not_write(engine, pc, home, trash):
    """A `phonevault status` during a backup must not call the running job dead or sweep its half files away."""
    rid = engine.journal.start_run("gallery")
    half = write(engine.staging / rid / "000001_half.jpg", b"in flight")
    code = ("from phonevault.core.config import Config\nfrom phonevault.core.engine import Engine\n"
            f"e = Engine(Config(pc_root={str(pc)!r}, trash_dir={str(trash)!r}))\n"
            "print(e.read_only, len(e.journal.runs()))\n"
            "try:\n    e.journal.start_run('x')\nexcept RuntimeError as x:\n    print('refused')\n")
    p = subprocess.run([sys.executable, "-c", code], cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=60)
    assert p.stdout.split() == ["True", "1", "refused"], p.stdout + p.stderr
    assert half.exists() and engine.journal.run(rid).status == "running"


# ---- names from the phone ------------------------------------------------------------

@pytest.mark.parametrize("raw, safe", [("a.jpg", "a.jpg"), ("..", "_"), (".", "_"), ("", "_"), ("...", "_"), ("a:b.jpg", "a_b.jpg"),
                                       ("..\\..\\evil.exe", ".._.._evil.exe"), ("C:x", "C_x"), ("con.txt", "_con.txt"), ("NUL", "_NUL"),
                                       ("a.jpg. ", "a.jpg"), ("tab\there", "tab_here"), ("صورة.jpg", "صورة.jpg"), (".nomedia", ".nomedia")])
def test_pc_name(raw, safe):
    assert pc_name(raw) == safe and pc_name(safe) == safe
    assert is_safe_rel("Download/" + raw) is (raw == safe)


def test_pc_rel_and_inside(tmp_path):
    assert pc_rel("Download/../../x").as_posix() == "Download/_/_/x" and pc_rel("") == Path()
    assert inside(tmp_path / "a" / "b", tmp_path) and inside(tmp_path, tmp_path)
    assert not inside(tmp_path / ".." / "x", tmp_path) and not inside(str(tmp_path) + "-other", tmp_path)


HOSTILE = ["DCIM/Camera/..\\..\\..\\evil.jpg", "DCIM/C:\\Users\\x/evil2.jpg", "DCIM/../../evil3.jpg", "DCIM/Camera/a.jpg:stream.jpg",
           "Pictures/..\\..\\up/evil4.jpg", "Pictures/Camera/nul.jpg"]


class HostilePhone(LocalTransport):
    """Lists names a malicious app could create; serves any of them as one small file."""

    def list_tree(self, root, max_depth=6):
        return [RemoteFile(p, p.rsplit("/", 1)[-1], len(p), 1_600_000_000.0) for p in HOSTILE if p.startswith(root)]

    def pull(self, remote_path, local_path):
        Path(local_path).write_bytes(remote_path.encode())


@pytest.mark.parametrize("what", ["gallery", "files"])
def test_hostile_file_names_stay_inside_the_pc_folder(engine, pc, tmp_path, what):
    everything = {p for p in tmp_path.rglob("*")}
    _, rid = media.sync(engine, HostilePhone(tmp_path), what, roots=["DCIM", "Pictures"])
    ops = [o for o in engine.journal.ops(rid) if o["op"] == "copy"]
    assert len(ops) == len(HOSTILE) and all(o["status"] == "ok" for o in ops), [o["note"] for o in ops]
    for o in ops:
        assert inside(o["dst"], pc) and Path(o["dst"]).read_bytes() == o["src"].encode()
    new = {p for p in tmp_path.rglob("*") if p.is_file()} - everything
    assert all(inside(p, pc) or p.is_relative_to(tmp_path / "home") for p in new)


# ---- the demo, and the jobs a folder cannot show -----------------------------------------

@pytest.fixture
def demo_world(tmp_path, monkeypatch):
    from phonevault import demo, transports
    monkeypatch.setenv("PHONEVAULT_HOME", str(tmp_path / "x"))     # make() changes it; monkeypatch puts it back
    monkeypatch.setenv("PHONEVAULT_NO_DEVICES", "1")
    cfg, phone = demo.make(tmp_path / "demo")
    e = Engine(cfg)
    yield cfg, phone, e
    e.close()
    transports.use(None)


def test_demo_backs_up_everything_and_undoes_it(demo_world, monkeypatch):
    import phonevault.server.app as app_mod
    cfg, phone, e = demo_world
    monkeypatch.setattr(app_mod, "engine", e)
    monkeypatch.setattr(app_mod, "cfg", cfg)
    before = content(cfg.root)
    app_mod._run_job("all", False, "auto")
    assert "failed" not in e.events.state["message"].replace("0 failed", ""), e.events.state["message"]
    runs = {r.kind: r for r in e.journal.runs()}
    assert {"gallery", "files", "apps", "settings", "pim"} <= set(runs)
    assert runs["gallery"].files == 29 and runs["apps"].files == 6
    dups = [o for o in e.journal.ops(runs["gallery"].id) if o["status"] == "skipped-dup"]
    assert len(dups) == 1 and dups[0]["src"].endswith("copy of IMG.jpg")
    assert len(list((cfg.root / "House" / "Garden plans").glob("*.jpg"))) == 5       # filed by name into the folder that was there
    pim = cfg.root / cfg.pim_subfolder / (cfg.root / cfg.pim_subfolder / "LATEST.txt").read_text()
    sms = json.loads((pim / "sms.json").read_text("utf-8"))
    assert sms[1]["body"] == "The bike is done, total, parts=40, work=25." or "parts=40, work=25" in json.dumps(sms[1])
    assert len(json.loads((pim / "contacts.json").read_text("utf-8"))) == 6 and (pim / "contacts.vcf").read_text("utf-8").count("BEGIN:VCARD") == 6
    for kind in ("gallery", "files"):
        e.undo(runs[kind].id)
    left = {k: v for k, v in content(cfg.root).items() if k.startswith(("House", "Work", "Camera", "Screenshots", "_New", "Phone files"))}
    assert left == before


def test_demo_never_reaches_for_a_real_phone(demo_world):
    from phonevault.transports import pick_transport, probe
    cfg, phone, _ = demo_world
    assert pick_transport(cfg) is phone and probe(cfg)["demo"] is True
    for fn in (phone._base, lambda: phone.run("devices")):
        with pytest.raises(RuntimeError, match="demo"):
            fn()


def test_dry_run_of_everything_writes_nothing(demo_world, monkeypatch):
    import phonevault.server.app as app_mod
    cfg, _, e = demo_world
    monkeypatch.setattr(app_mod, "engine", e)
    monkeypatch.setattr(app_mod, "cfg", cfg)
    before = content(cfg.root)
    app_mod._run_job("all", True, "auto")
    assert content(cfg.root) == before and e.journal.runs() == []
    for kind in ("apps", "appdata", "settings", "pim", "restore-all"):
        with pytest.raises(RuntimeError, match="no dry run"):
            app_mod._run_job(kind, True, "auto")
