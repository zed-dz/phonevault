"""Ways a backup tool can lose a file while saying everything is fine. Each test is one that once failed."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from phonevault.core.config import Config
from phonevault.core.engine import Engine
from phonevault.modules import media
from phonevault.transports.base import Transport
from phonevault.transports.local import LocalTransport

T0 = 1_700_000_000


class Blind(LocalTransport):
    """A link that cannot hash on the phone (MTP, or a phone without sha256sum)."""

    def sha256(self, remote_path):
        return None


def wr(p: Path, data: bytes, t: float = T0) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    os.utime(p, (t, t))
    return p


def found(root: Path, data: bytes) -> list[str]:
    return sorted(p.name for p in root.rglob("*") if p.is_file() and ".phonevault" not in p.parts and p.read_bytes() == data)


@pytest.fixture
def ph(tmp_path):
    r = tmp_path / "ph"
    r.mkdir()
    return r


@pytest.fixture(params=[LocalTransport, Blind], ids=["phone hashes", "phone cannot hash"])
def link(request, ph):
    return request.param(ph)


@pytest.fixture
def eng(tmp_path, home):
    (tmp_path / "pc1").mkdir()
    e = Engine(Config(pc_root=str(tmp_path / "pc1")))
    yield e
    e.close()


# ---- the phone is only read ---------------------------------------------------------

def test_no_code_path_can_remove_or_overwrite_on_the_phone():
    from phonevault.transports.adb import AdbTransport
    from phonevault.transports.mtp import MtpTransport
    for cls in (Transport, LocalTransport, AdbTransport, MtpTransport):
        assert not hasattr(cls, "delete"), cls
    assert not hasattr(MtpTransport, "push") and not hasattr(LocalTransport, "push")
    fields = Config.__dataclass_fields__
    for gone in ("move_mode", "mirror_mode", "mirror_max_missing_pct", "auto_confirm", "verify_hash"):
        assert gone not in fields
    src = Path(media.__file__).parents[1]
    for p in src.rglob("*.py"):
        text = p.read_text("utf-8")
        assert "rm -f" not in text and "MoveHere" not in text, p
    assert not (src / "modules" / "mirror.py").exists()


def test_backup_undo_and_undo_the_undo_leave_the_phone_alone(eng, ph, link):
    wr(ph / "DCIM/Camera/a.jpg", b"A" * 100)
    before = {p: (p.read_bytes(), p.stat().st_mtime) for p in ph.rglob("*") if p.is_file()}
    _, rid = media.sync(eng, link, "gallery")
    eng.undo(eng.undo(rid))
    assert {p: (p.read_bytes(), p.stat().st_mtime) for p in ph.rglob("*") if p.is_file()} == before


# ---- "already on the PC" must be true now ------------------------------------------

def test_a_rotted_pc_copy_does_not_count_for_a_renamed_phone_file(eng, ph, link):
    wr(ph / "DCIM/Camera/a.jpg", b"GOOD" * 100)
    media.sync(eng, link, "gallery")
    pc = next(eng.cfg.root.rglob("a.jpg"))
    wr(pc, b"ROT!" * 100, pc.stat().st_mtime)                       # same size, same date, other bytes
    (ph / "DCIM/Camera/a.jpg").rename(ph / "DCIM/Camera/renamed.jpg")
    media.sync(eng, link, "gallery")
    assert found(eng.cfg.root, b"GOOD" * 100) == ["renamed.jpg"]


def test_a_pc_copy_removed_after_the_plan_does_not_count(eng, ph, link):
    wr(ph / "DCIM/Camera/a.jpg", b"GOOD" * 100)
    media.sync(eng, link, "gallery")
    (ph / "DCIM/Camera/a.jpg").rename(ph / "DCIM/Camera/renamed.jpg")
    plan = media.plan_gallery(eng, link)
    shutil.move(next(eng.cfg.root.rglob("a.jpg")), eng.cfg.root.parent / "gone.jpg")
    eng.execute(plan, link)
    assert found(eng.cfg.root, b"GOOD" * 100) == ["renamed.jpg"]


@pytest.mark.parametrize("damage", ["gone", "other size", "other date and bytes"])
def test_a_changed_pc_copy_is_copied_again(eng, ph, link, damage):
    wr(ph / "DCIM/Camera/a.jpg", b"GOOD" * 100)
    media.sync(eng, link, "gallery")
    pc = next(eng.cfg.root.rglob("a.jpg"))
    if damage == "gone":
        pc.rename(eng.cfg.root.parent / "elsewhere.jpg")
    elif damage == "other size":
        wr(pc, b"cut", T0)
    else:
        wr(pc, b"EDIT" * 100, T0 + 900)
    media.sync(eng, link, "gallery")
    assert len(found(eng.cfg.root, b"GOOD" * 100)) == 1
    if damage != "gone":
        assert pc.exists(), "what you changed stays"


def test_an_unchanged_file_is_not_read_again(eng, ph, monkeypatch):
    wr(ph / "DCIM/Camera/a.jpg", b"GOOD" * 100)
    link = LocalTransport(ph)
    media.sync(eng, link, "gallery")
    monkeypatch.setattr(link, "sha256", lambda p: pytest.fail("no need to hash"))
    monkeypatch.setattr(link, "pull", lambda *a: pytest.fail("no need to copy"))
    plan, rid = media.sync(eng, link, "gallery")
    assert rid is None and plan.items[0].action == "skip-exists"


def test_a_file_you_moved_on_the_pc_is_found_by_content_without_a_transfer(eng, ph, monkeypatch):
    wr(ph / "DCIM/Camera/a.jpg", b"GOOD" * 100)
    link = LocalTransport(ph)
    media.sync(eng, link, "gallery")
    pc = next(eng.cfg.root.rglob("a.jpg"))
    (eng.cfg.root / "Trips").mkdir()
    pc.rename(eng.cfg.root / "Trips" / "my name.jpg")
    monkeypatch.setattr(link, "pull", lambda *a: pytest.fail("the phone's hash was enough"))
    media.sync(eng, link, "gallery")
    assert found(eng.cfg.root, b"GOOD" * 100) == ["my name.jpg"]
    plan, rid = media.sync(eng, link, "gallery")
    assert rid is None and plan.items[0].action == "skip-exists"


# ---- same name, same size, other content -------------------------------------------

@pytest.mark.parametrize("one_run", [True, False])
def test_same_name_and_size_in_two_albums(eng, ph, link, one_run):
    wr(ph / "DCIM/Camera/IMG_0001.jpg", b"AAAA" * 100)
    if not one_run:
        media.sync(eng, link, "gallery")
    wr(ph / "Pictures/Other cam/IMG_0001.jpg", b"BBBB" * 100, T0 + 50)
    media.sync(eng, link, "gallery")
    assert found(eng.cfg.root, b"AAAA" * 100) and found(eng.cfg.root, b"BBBB" * 100)


def test_same_name_and_size_already_in_the_target_folder(eng, ph, link):
    mine = wr(eng.cfg.root / "Camera" / "IMG_0001.jpg", b"MINE" * 100)
    wr(ph / "DCIM/Camera/IMG_0001.jpg", b"THEY" * 100)
    media.sync(eng, link, "gallery")
    assert mine.read_bytes() == b"MINE" * 100 and found(eng.cfg.root, b"THEY" * 100) == ["IMG_0001 (2).jpg"]


@pytest.mark.parametrize("what, rel", [("gallery", "DCIM/Camera/a.jpg"), ("files", "Documents/notes.db")])
def test_an_edit_that_keeps_name_and_size_is_copied_and_the_old_one_stays(eng, ph, link, what, rel):
    wr(ph / rel, b"AAAA" * 100)
    media.sync(eng, link, what)
    wr(ph / rel, b"BBBB" * 100, T0 + 500)
    media.sync(eng, link, what)
    assert len(found(eng.cfg.root, b"AAAA" * 100)) == 1 and len(found(eng.cfg.root, b"BBBB" * 100)) == 1


def test_dedupe_hashes_again_before_it_trusts_the_other_copy(eng):
    wr(eng.cfg.root / "Trips" / "keep.jpg", b"SAME" * 100)
    nc = wr(eng.cfg.root / "_New from phone" / "x" / "new.jpg", b"SAME" * 100)
    from phonevault.modules import dedupe
    eng.refresh_index()
    for p in eng.cfg.root.rglob("*.jpg"):
        eng.index.ensure_sha(str(p))
    wr(eng.cfg.root / "Trips" / "keep.jpg", b"ROT!" * 100)          # same size and date: the index still trusts it
    assert dedupe.sweep(eng)["duplicates"] == 0 and nc.exists()


# ---- another PC folder ---------------------------------------------------------------

@pytest.mark.parametrize("old_gone", [True, False])
def test_a_new_pc_folder_gets_everything(tmp_path, home, ph, link, old_gone):
    for i in range(5):
        wr(ph / f"DCIM/Camera/a{i}.jpg", b"GOOD%d" % i * 100)
    (tmp_path / "pc1").mkdir()
    cfg = Config(pc_root=str(tmp_path / "pc1"))
    e = Engine(cfg)
    media.sync(e, link, "gallery")
    e.close()
    if old_gone:
        (tmp_path / "pc1").rename(tmp_path / "unplugged")
    cfg.pc_root = str(tmp_path / "pc2")
    e = Engine(cfg)
    try:
        plan, rid = media.sync(e, link, "gallery")
        assert rid and "Nothing to do" not in e.events.state["message"]
        assert len(list((tmp_path / "pc2" / "Camera").glob("*.jpg"))) == 5
        wr(ph / "DCIM/Camera/new.jpg", b"NEW" * 77)
        media.sync(e, link, "gallery")
        assert (tmp_path / "pc2" / "Camera" / "new.jpg").exists()
        assert all(v.startswith(str(tmp_path / "pc2")) for v in e.mapper.saved.values())
    finally:
        e.close()


def test_changing_the_folder_in_the_dashboard_does_the_same(eng, ph, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import phonevault.server.app as mod
    wr(ph / "DCIM/Camera/a.jpg", b"GOOD" * 100)
    media.sync(eng, LocalTransport(ph), "gallery")
    eng.cfg.api_token = "t" * 30
    monkeypatch.setattr(mod, "engine", eng)
    monkeypatch.setattr(mod, "cfg", eng.cfg)
    c = TestClient(mod.app, client=("127.0.0.1", 1), headers={"X-PhoneVault-Token": "t" * 30})
    assert c.post("/api/config", json={"pc_root": str(tmp_path / "pc2")}).status_code == 200
    media.sync(eng, LocalTransport(ph), "gallery")
    assert (tmp_path / "pc2" / "Camera" / "a.jpg").exists()
    assert c.post("/api/config", json={"pc_root": str(tmp_path) + "\x00x"}).status_code == 400


def test_settings_wait_until_the_dashboard_is_closed(home, tmp_path, capsys):
    from conftest import PROJECT_ROOT
    import sys
    from phonevault import cli
    code = ("import sys\nfrom phonevault.core import lock\nfrom phonevault.core.config import app_dir\n"
            "print(lock.acquire(app_dir() / 'engine.lock'), flush=True)\nsys.stdin.read()\n")
    p = subprocess.Popen([sys.executable, "-c", code], cwd=str(PROJECT_ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert "True" in p.stdout.readline()
        assert cli.main(["config", "--set", f"pc_root={tmp_path}"]) == 1
        assert Config.load().pc_root != str(tmp_path)
    finally:
        p.communicate("")
    assert cli.main(["config", "--set", f"pc_root={tmp_path}"]) == 0


WRITERS = ["gallery", "files", "apps", "appdata", "settings", "pim", "dedupe", "review", "undo", "upload"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    from phonevault import demo, transports
    import phonevault.server.app as mod
    monkeypatch.setenv("PHONEVAULT_HOME", str(tmp_path / "x"))
    monkeypatch.setenv("PHONEVAULT_NO_DEVICES", "1")
    cfg, phone = demo.make(tmp_path / "demo")
    cfg.api_token = "t" * 30
    e = Engine(cfg)
    monkeypatch.setattr(mod, "engine", e)
    monkeypatch.setattr(mod, "cfg", cfg)
    _, rid = media.sync(e, phone, "gallery")
    yield cfg, phone, e, mod, rid
    e.close()
    transports.use(None)


def write_with(name, world):
    from fastapi.testclient import TestClient
    from phonevault.modules import apps, dedupe, pim, settings
    cfg, phone, e, mod, rid = world
    c = TestClient(mod.app, client=("127.0.0.1", 1), headers={"X-PhoneVault-Token": "t" * 30}, raise_server_exceptions=False)
    if name in ("gallery", "files"):
        wr(phone.root / "DCIM/Camera/fresh.jpg", b"fresh" * 9)
        wr(phone.root / "Download/fresh.txt", b"fresh2" * 9)
        media.sync(e, phone, name)
    elif name == "apps":
        apps.backup_apks(e, phone)
    elif name == "appdata":
        apps.adb_backup(e, phone)
    elif name == "settings":
        settings.backup_settings(e, phone)
    elif name == "pim":
        pim.backup_pim(e, phone)
    elif name == "dedupe":
        shutil.copy(next((cfg.root / "Camera").glob("*.jpg")), cfg.root / "_New from phone" / "Tile samples" / "dup.jpg")
        dedupe.sweep(e)
    elif name == "review":
        r = c.post("/api/review/move", json={"album": "Tile samples", "category": "Work"})
        if r.status_code != 200:
            raise RuntimeError(r.text)
    elif name == "undo":
        e.undo(rid)
    elif name == "upload":
        r = c.post("/api/upload", files={"file": ("f", b"abc")}, data={"path": "Download/up.txt"})
        if r.status_code != 200:
            raise RuntimeError(r.text)


def tree(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("name", WRITERS)
def test_a_failing_drive_gets_nothing_from_any_job(world, monkeypatch, name):
    from phonevault.core import drivecheck
    cfg, phone, *_ = world
    if name == "dedupe":
        shutil.copy(next((cfg.root / "Camera").glob("*.jpg")), cfg.root / "_New from phone" / "Tile samples" / "dup.jpg")
    before = tree(cfg.root)
    monkeypatch.setattr(drivecheck, "check_drive", lambda *a, **k: {"letter": "X", "ok": False, "reason": "Windows reports volume unhealthy"})
    with pytest.raises(RuntimeError, match="not safe to write"):
        write_with(name, world)
    assert tree(cfg.root) == before


@pytest.mark.parametrize("name", [w for w in WRITERS if w not in ("review",)])
def test_an_unplugged_drive_is_an_error_for_every_job(world, name):
    cfg, *_ = world
    if name == "dedupe":
        name = "files"
    cfg.root.rename(cfg.root.parent / "unplugged")
    with pytest.raises(RuntimeError, match="missing"):
        write_with(name, world)
    assert not cfg.root.exists()


def test_an_unmounted_empty_folder_is_not_filled(eng, ph):
    wr(ph / "DCIM/Camera/a.jpg", b"GOOD" * 100)
    media.sync(eng, LocalTransport(ph), "gallery")
    eng.cfg.root.rename(eng.cfg.root.parent / "unmounted")
    eng.cfg.root.mkdir()
    with pytest.raises(RuntimeError, match="empty now"):
        media.sync(eng, LocalTransport(ph), "gallery")


# ---- apps -----------------------------------------------------------------------------

def test_a_half_saved_app_is_marked_and_tried_again(world):
    from phonevault.modules import apps
    cfg, phone, e, *_ = world
    shell, pkg = phone.shell, "org.example.notes"

    def with_split(cmd, timeout=120):
        if cmd.startswith(f"pm path {pkg}"):
            return f"package:/data/app/{pkg}/base.apk\npackage:/data/app/{pkg}/split_x.apk"
        return shell(cmd, timeout)

    phone.shell = with_split
    r = apps.backup_apks(e, phone)
    assert r["failed"] == 1 and e.journal.run(r["run_id"]).status == "partial"
    entry = {a["package"]: a for a in json.loads((cfg.root / cfg.apps_subfolder / "apps.json").read_text("utf-8"))["apps"]}[pkg]
    assert entry["complete"] is False and entry["files"] == []
    wr(phone.root / "data/app" / pkg / "split_x.apk", b"split")
    r = apps.backup_apks(e, phone)
    assert r["pulled"] == 1 and r["unchanged"] == 5 and r["failed"] == 0
    assert sorted(p.name for p in (cfg.root / cfg.apps_subfolder / pkg).iterdir()) == ["base.apk", "split_x.apk"]
    # an update that fails half way keeps the set we have
    phone.shell = lambda cmd, timeout=120: with_split(cmd, timeout).replace("versionCode:241", "versionCode:242").replace(
        "versionCode=241", "versionCode=242")
    (phone.root / "data/app" / pkg / "split_x.apk").unlink()
    r = apps.backup_apks(e, phone)
    assert r["failed"] == 1
    entry = {a["package"]: a for a in r["apps"]}[pkg]
    assert entry["files"] == ["base.apk", "split_x.apk"] and entry["complete"] is False
    assert (cfg.root / cfg.apps_subfolder / pkg / "split_x.apk").read_bytes() == b"split"
    # a file that vanished from the PC is fetched again
    phone.shell = shell
    (cfg.root / cfg.apps_subfolder / "org.example.timer" / "base.apk").unlink()
    assert apps.backup_apks(e, phone)["pulled"] == 2


# ---- links -----------------------------------------------------------------------------

def make_link(link: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_a_link_in_the_pc_folder_is_never_written_through(eng, ph, tmp_path):
    out = tmp_path / "outside"
    (eng.cfg.root / "Camera").mkdir()
    make_link(eng.cfg.root / "Camera" / "jn", out)
    make_link(eng.cfg.root / "Trips", out)
    make_link(eng.cfg.root / "Phone apps", out)
    wr(ph / "Pictures/jn/evil.jpg", b"one" * 50)
    wr(ph / "Pictures/Trips/evil.jpg", b"two" * 60)
    wr(ph / "DCIM/Camera/fine.jpg", b"ok" * 70)
    _, rid = media.sync(eng, LocalTransport(ph), "gallery")
    ops = {o["src"]: o for o in eng.journal.ops(rid) if o["op"] == "copy"}
    assert ops["DCIM/Camera/fine.jpg"]["status"] == "ok"
    assert ops["Pictures/Trips/evil.jpg"]["status"] == "failed" and "outside the PC folder" in ops["Pictures/Trips/evil.jpg"]["note"]
    assert list(out.iterdir()) == []
    with pytest.raises(OSError, match="outside"):
        eng.safe_dir(eng.cfg.root / "Phone apps")


def test_moves_undo_and_trash_do_not_follow_links(eng, ph, tmp_path):
    out = tmp_path / "outside"
    f = wr(eng.cfg.root / "a.jpg", b"x")
    make_link(eng.cfg.root / "jn", out)
    rid = eng.journal.start_run("review")
    with pytest.raises(OSError, match="outside"):
        eng.journal.move_path(rid, f, eng.cfg.root / "jn" / "a.jpg")
    wr(out / "theirs.jpg", b"not ours")
    with pytest.raises(OSError, match="outside"):
        eng.journal.move_to_trash(eng.cfg.root / "jn" / "theirs.jpg")
    assert f.exists() and (out / "theirs.jpg").exists()
    # a folder that became a link after the copy: undo leaves the other place alone
    wr(ph / "DCIM/Camera/a.jpg", b"GOOD" * 9)
    _, run = media.sync(eng, LocalTransport(ph), "gallery")
    (eng.cfg.root / "Camera").rename(tmp_path / "cam")
    make_link(eng.cfg.root / "Camera", tmp_path / "cam")
    eng.undo(run)
    assert (tmp_path / "cam" / "a.jpg").exists() and eng.journal.run(run).undone == 0


def test_staging_behind_a_link_is_refused(eng, ph, tmp_path):
    make_link(eng.cfg.root / ".phonevault", tmp_path / "outside")
    wr(ph / "DCIM/Camera/a.jpg", b"GOOD" * 9)
    with pytest.raises(OSError, match="outside"):
        media.sync(eng, LocalTransport(ph), "gallery")
    assert list((tmp_path / "outside").iterdir()) == []


# ---- undo ------------------------------------------------------------------------------

def test_power_cut_in_the_middle_of_an_undo_loses_nothing(tmp_path, home, ph, monkeypatch):
    from phonevault.core import journal as jmod
    for i in range(4):
        wr(ph / f"DCIM/Camera/a{i}.jpg", b"GOOD%d" % i * 100)
    cfg = Config(pc_root=str(tmp_path / "pc1"))
    e = Engine(cfg)
    _, rid = media.sync(e, LocalTransport(ph), "gallery")
    real, n = jmod._rename, [0]

    def cut(a, b):
        real(a, b)
        n[0] += 1
        if n[0] == 2:
            raise KeyboardInterrupt            # moved, and the journal was never told

    monkeypatch.setattr(jmod, "_rename", cut)
    monkeypatch.setattr(e.journal, "settle", lambda oid, status, note=None, _r=e.journal.settle: None if status == "failed" else _r(oid, status, note))
    with pytest.raises(KeyboardInterrupt):
        e.undo(rid)
    e.close()
    monkeypatch.setattr(jmod, "_rename", real)
    e = Engine(cfg)
    try:
        e.undo(rid)
        assert not list((tmp_path / "pc1" / "Camera").glob("*.jpg")) if (tmp_path / "pc1" / "Camera").exists() else True
        for r in [r for r in e.journal.runs() if r.kind == "undo"]:
            e.undo(r.id)
        assert len(list((tmp_path / "pc1" / "Camera").glob("*.jpg"))) == 4
    finally:
        e.close()


def test_the_page_offers_to_undo_an_undo(world):
    page = (Path(world[3].__file__).parent / "static" / "index.html").read_text("utf-8")
    assert "Undo the undo" in page and 'r.kind !== "undo"' not in page
    from fastapi.testclient import TestClient
    cfg, phone, e, mod, rid = world
    c = TestClient(mod.app, client=("127.0.0.1", 1), headers={"X-PhoneVault-Token": "t" * 30})
    n = len(list((cfg.root / "Camera").glob("*.jpg")))

    def press(run):
        assert c.post(f"/api/undo/{run}").status_code == 200
        while e.busy.locked():
            time.sleep(0.02)

    press(rid)
    assert not list(cfg.root.glob("Camera/*.jpg"))
    press(next(r.id for r in e.journal.runs() if r.kind == "undo"))
    assert len(list((cfg.root / "Camera").glob("*.jpg"))) == n


# ---- server -----------------------------------------------------------------------------

@pytest.mark.parametrize("mtime", ["nan", "inf", "-inf", "1e30", "-5", "soon"])
def test_a_silly_date_is_a_400_and_leaves_nothing(world, mtime):
    from fastapi.testclient import TestClient
    cfg, _, _, mod, _ = world
    c = TestClient(mod.app, client=("127.0.0.1", 1), headers={"X-PhoneVault-Token": "t" * 30})
    r = c.post("/api/upload", files={"file": ("f", b"abc")}, data={"path": "Download/up.txt", "mtime": mtime})
    assert r.status_code == 400 and not list(cfg.root.rglob("up.txt"))


def test_an_upload_does_not_freeze_the_dashboard(world, monkeypatch):
    import httpx
    import uvicorn
    from phonevault.core import drivecheck
    cfg, _, _, mod, _ = world
    monkeypatch.setattr(drivecheck, "refuse_if_unhealthy", lambda p: time.sleep(2.0))
    port = 8700 + os.getpid() % 60
    while mod.port_taken(port):
        port += 1
    srv = uvicorn.Server(uvicorn.Config(mod.app, port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    while not srv.started:
        time.sleep(0.05)
    h = {"X-PhoneVault-Token": "t" * 30}
    out = {}

    def send():
        out["r"] = httpx.post(f"http://127.0.0.1:{port}/api/upload", headers=h, files={"file": ("f", b"x")}, data={"path": "Download/z.txt"}, timeout=60)

    up = threading.Thread(target=send)
    up.start()
    time.sleep(0.5)
    worst = 0.0
    while up.is_alive():
        t = time.time()
        assert httpx.get(f"http://127.0.0.1:{port}/api/plan", headers=h, timeout=60).status_code == 200
        worst = max(worst, time.time() - t)
        time.sleep(0.1)
    srv.should_exit = True
    th.join(10)
    assert out["r"].status_code == 200 and worst < 1.5, worst


def test_upload_says_how_big_it_is_first(world, monkeypatch):
    from fastapi.testclient import TestClient
    _, _, _, mod, _ = world
    c = TestClient(mod.app, client=("127.0.0.1", 1), headers={"X-PhoneVault-Token": "t" * 30})
    assert c.post("/api/upload", content=iter([b"x"])).status_code == 411

    class Usage:
        free = mod.MIN_FREE + 10

    monkeypatch.setattr(mod.shutil, "disk_usage", lambda p: Usage)
    assert c.post("/api/upload", files={"file": ("f", b"x" * 500)}, data={"path": "Download/z"}).status_code == 507


def test_upload_compares_bytes_even_when_name_size_and_date_match(world):
    from fastapi.testclient import TestClient
    cfg, _, _, mod, _ = world
    c = TestClient(mod.app, client=("127.0.0.1", 1), headers={"X-PhoneVault-Token": "t" * 30})
    for body, want in ((b"AAAA", 200), (b"BBBB", 200), (b"BBBB", 208)):
        r = c.post("/api/upload", files={"file": ("f", body)}, data={"path": "Download/u.txt", "mtime": str(T0)})
        assert r.status_code == want, r.text
    assert found(cfg.root, b"AAAA") == ["u.txt"] and found(cfg.root, b"BBBB") == ["u (2).txt"]


def test_the_album_table_is_full_after_sync_everything(world):
    cfg, phone, e, mod, _ = world
    wr(phone.root / "DCIM/Camera/fresh.jpg", b"fresh" * 9)
    mod._run_job("all", False, "auto")
    s = e.events.snapshot()
    assert s["albums"]["Camera"]["copied"] == 1 and s["albums"]["Download"]["copied"] == 2
    assert s["total_files"] == s["done_files"] >= 4 and s["failed"] == 0


def test_no_small_buttons():
    import re
    from phonevault.server import app as mod
    css = (Path(mod.__file__).parent / "static" / "index.html").read_text("utf-8")
    for rule in re.findall(r"^\s*([^{\n]*button[^{\n]*)\{([^}]*)\}", css, re.M):
        m = re.search(r"min-height: (\d+)px", rule[1])
        assert not m or int(m.group(1)) >= 44, rule
