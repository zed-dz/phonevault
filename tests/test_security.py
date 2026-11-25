"""The dashboard carries a phone's private data over HTTP. What must hold, one test per promise."""
from __future__ import annotations

import inspect
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TOKEN = "s3cret-token-for-tests"
H = {"X-PhoneVault-Token": TOKEN}
NEW = "_New from phone"


@pytest.fixture
def mod(engine, cfg, monkeypatch):
    import phonevault.server.app as app_mod
    cfg.api_token = TOKEN
    monkeypatch.setattr(app_mod, "engine", engine)
    monkeypatch.setattr(app_mod, "cfg", cfg)
    return app_mod


@pytest.fixture
def pc_client(mod):
    """A browser on this PC, already signed in."""
    return TestClient(mod.app, client=("127.0.0.1", 50000), headers=H)


@pytest.fixture
def lan_client(mod):
    """A phone on the same network, with the token."""
    return TestClient(mod.app, client=("192.0.2.7", 50000), headers=H)


# ---- who may talk to it ---------------------------------------------------------

def test_default_bind_is_this_pc_only():
    from phonevault.server.app import serve
    assert inspect.signature(serve).parameters["host"].default == "127.0.0.1"


def test_serve_listens_on_localhost_unless_remote_is_asked_for(home, monkeypatch):
    from phonevault import cli
    import phonevault.server.app as app_mod
    seen = []
    monkeypatch.setattr(app_mod, "serve", lambda cfg, host, **k: seen.append(host))
    assert cli.main(["serve", "--no-open"]) == 0
    assert cli.main(["serve", "--no-open", "--remote"]) == 0
    assert seen == ["127.0.0.1", "0.0.0.0"]


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "192.0.2.7"])
def test_every_route_needs_the_token_even_from_this_pc(mod, host):
    c = TestClient(mod.app, client=(host, 50000))
    checked = 0
    for route in mod.app.routes:
        for method in getattr(route, "methods", None) or []:
            if method == "HEAD":
                continue
            r = c.request(method, route.path.replace("{run_id}", "x"))
            assert r.status_code == 401, f"{method} {route.path} -> {r.status_code}"
            checked += 1
    assert checked > 20


def test_wrong_token_is_refused(mod):
    c = TestClient(mod.app, client=("127.0.0.1", 1))
    assert c.get("/api/state", headers={"X-PhoneVault-Token": "nope"}).status_code == 401
    assert c.get("/api/state", headers={"X-PhoneVault-Token": TOKEN + "x"}).status_code == 401
    assert c.get("/", params={"t": "nope"}).status_code == 401
    assert c.get("/api/state", params={"t": TOKEN}).status_code == 401     # the link only signs in on the page itself


def test_no_token_set_means_nothing_is_served(mod, cfg):
    cfg.api_token = ""
    c = TestClient(mod.app, client=("127.0.0.1", 1))
    assert c.get("/api/state").status_code == 401
    assert c.get("/api/state", headers={"X-PhoneVault-Token": ""}).status_code == 401
    assert c.get("/", params={"t": ""}).status_code == 401


def test_the_link_becomes_a_cookie_and_leaves_the_address_bar(mod):
    c = TestClient(mod.app, client=("127.0.0.1", 1))
    r = c.get("/", params={"t": TOKEN}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    assert c.get("/").status_code == 200                 # the cookie now works
    assert c.get("/api/state").status_code == 200


def test_token_never_leaks_or_changes_over_http(pc_client, cfg):
    assert TOKEN not in pc_client.get("/api/state").text
    assert pc_client.post("/api/config", json={"api_token": "hijack"}).status_code == 400
    assert cfg.api_token == TOKEN


def test_token_is_made_once_and_kept(cfg):
    from phonevault.core.config import ensure_api_token
    cfg.api_token = ""
    tok = ensure_api_token(cfg)
    assert len(tok) >= 32 and ensure_api_token(cfg) == tok and tok in cfg.path().read_text("utf-8")


# ---- other web pages (CSRF, DNS rebinding, framing) ------------------------------

@pytest.mark.parametrize("bad", [{"Origin": "https://evil.example"}, {"Origin": "null"}, {"Origin": "http://testserver.evil.example"},
                                 {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}])
def test_another_web_page_cannot_press_buttons(pc_client, bad):
    for path in ("/api/shutdown", "/api/cancel", "/api/pause", "/api/job", "/api/undo/x", "/api/watch/tick"):
        assert pc_client.post(path, headers=bad).status_code == 403, path
    assert pc_client.put("/api/albums", headers=bad, json={"key": "x"}).status_code == 403
    r = pc_client.post("/api/upload", headers=bad, files={"file": ("a.jpg", b"x")}, data={"path": "DCIM/a.jpg"})
    assert r.status_code == 403


def test_our_own_page_can(pc_client):
    assert pc_client.post("/api/cancel", headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}).status_code == 200


def test_a_rebound_name_has_no_cookie_and_gets_nothing(mod):
    c = TestClient(mod.app, client=("127.0.0.1", 1), base_url="http://evil.example")
    assert c.get("/api/state").status_code == 401


def test_security_headers_and_script_hash(pc_client, mod):
    r = pc_client.get("/")
    csp = r.headers["content-security-policy"]
    assert "script-src 'sha256-" in csp and "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert "frame-ancestors 'none'" in csp and r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"
    assert len(re.findall(r"<script", r.text)) == 1 and not re.search(r"\son\w+=", r.text.split("<script>")[0])
    assert pc_client.get("/api/state").headers["x-content-type-options"] == "nosniff"


# ---- what a signed-in device may not do -------------------------------------------

def test_only_the_pc_changes_settings_stops_it_or_writes_to_the_phone(lan_client, pc_client, cfg):
    old = cfg.pc_root
    assert lan_client.post("/api/config", json={"pc_root": str(Path(old).parent)}).status_code == 403
    assert lan_client.post("/api/shutdown").status_code == 403
    assert lan_client.post("/api/job", json={"kind": "restore-all"}).status_code == 403
    assert cfg.pc_root == old
    assert pc_client.post("/api/shutdown").status_code == 200


def test_a_tunnel_on_this_pc_does_not_count_as_this_pc(pc_client):
    """`tailscale serve`, a reverse proxy or a tunnel connects from 127.0.0.1 for somebody else."""
    for h in ("X-Forwarded-For", "Forwarded", "Tailscale-User-Login", "CF-Connecting-IP"):
        assert pc_client.post("/api/shutdown", headers={h: "198.51.100.9"}).status_code == 403, h


@pytest.mark.parametrize("key", ["adb_path", "trash_dir", "device_serial", "gallery_roots", "album_aliases"])
def test_settings_that_start_programs_or_remove_safety_are_not_reachable(pc_client, cfg, key):
    before = getattr(cfg, key)
    assert pc_client.post("/api/config", json={key: "C:\\evil.exe"}).status_code == 400
    assert getattr(cfg, key) == before


@pytest.mark.parametrize("body", [{"pc_root": "relative"}, {"wifi_address": "--help"}, {"wifi_address": "x;calc:1"}, {"dashboard_port": "80"},
                                  {"dashboard_port": "99999"}])
def test_bad_settings_are_refused(pc_client, cfg, body):
    before = cfg.to_dict()
    assert pc_client.post("/api/config", json=body).status_code == 400
    assert cfg.to_dict() == before


def test_settings_do_not_change_under_a_running_job(pc_client, engine, tmp_path):
    assert engine.busy.acquire(blocking=False)
    try:
        assert pc_client.post("/api/config", json={"pc_root": str(tmp_path)}).status_code == 409
    finally:
        engine.busy.release()


def test_album_mapping_cannot_point_outside_the_pc_folder(pc_client, engine, cfg, tmp_path):
    for bad in (str(tmp_path / "elsewhere"), str(cfg.root / ".." / "elsewhere"), "relative/x", str(cfg.state_dir / "trash")):
        assert pc_client.put("/api/albums", json={"key": "DCIM/Camera", "pc_folder": bad}).status_code == 400, bad
    assert "DCIM/Camera" not in engine.mapper.saved
    good = str(cfg.root / "Trips")
    assert pc_client.put("/api/albums", json={"key": "DCIM/Camera", "pc_folder": good}).status_code == 200


# ---- paths -------------------------------------------------------------------------

BAD_RELS = ["../x.jpg", "..\\x.jpg", f"{NEW}/../../x.jpg", "C:/Windows/win.ini", "C:\\Windows\\win.ini", "/etc/passwd", "\\\\server\\share\\x",
            "a.jpg:stream", "./x.jpg", "", "x\x00.jpg"]


@pytest.mark.parametrize("rel", BAD_RELS)
def test_file_and_thumb_never_leave_the_pc_folder(pc_client, pc, mkfile, rel):
    mkfile(pc.parent / "x.jpg", b"outside")
    for route in ("/file", "/thumb"):
        assert pc_client.get(route, params={"rel": rel}).status_code in (400, 404)


def test_a_link_inside_the_folder_cannot_lead_out(pc_client, pc, tmp_path, mkfile):
    mkfile(tmp_path / "secret" / "s.jpg", b"secret")
    try:
        os.symlink(tmp_path / "secret", pc / "link", target_is_directory=True)
    except OSError:
        pytest.skip("this account may not make links")
    assert pc_client.get("/file", params={"rel": "link/s.jpg"}).status_code == 404


@pytest.mark.parametrize("name", ["page.html", "pic.svg", "x.xhtml", "doc.pdf", "noext"])
def test_a_file_from_the_phone_never_runs_as_a_page(pc_client, pc, mkfile, name):
    mkfile(pc / "Phone files" / name, b"<script>fetch('/api/shutdown',{method:'POST'})</script>")
    r = pc_client.get("/file", params={"rel": f"Phone files/{name}"})
    assert r.status_code == 200 and r.headers["content-disposition"].startswith("attachment")
    assert r.headers["content-type"] == "application/octet-stream" and "sandbox" in r.headers["content-security-policy"]


def up(client, path, content=b"x" * 64, **extra):
    return client.post("/api/upload", files={"file": ("ignored", content)}, data={"path": path, **extra})


@pytest.mark.parametrize("path", ["", "/", "..", "../x.jpg", "DCIM/../../x.jpg", "DCIM\\..\\..\\x.jpg", "DCIM/.. /x.jpg", "DCIM/.../x.jpg"])
def test_upload_refuses_climbing_paths(lan_client, pc, path):
    before = {p for p in pc.parent.rglob("*")}
    assert up(lan_client, path).status_code in (400, 422)
    assert {p for p in pc.parent.rglob("*") if "home" not in p.parts and p.is_file()} <= before


@pytest.mark.parametrize("path", ["C:/Users/x/evil.txt", "D:evil.txt", "Download/a.txt:hidden", "Download/con.txt", "//server/share/a.txt",
                                  "Download/a?.txt", "Download/trail. /x.txt"])
def test_upload_lands_inside_the_pc_folder_whatever_the_name(lan_client, pc, path):
    r = up(lan_client, path, os.urandom(50))
    assert r.status_code == 200, r.text
    dst = Path(r.json()["dst"])
    assert dst.is_file() and dst.resolve().is_relative_to(pc.resolve())
    assert not set(':?*"<>|') & set(str(dst.relative_to(pc)))


def test_upload_stops_before_the_disk_is_full(lan_client, mod, pc, monkeypatch):
    class Usage:
        def __init__(self, p):
            self.free = 10 ** 12 if "staging" not in str(p) else 1      # room for the form, none for the copy

    monkeypatch.setattr(mod.shutil, "disk_usage", Usage)
    assert up(lan_client, "Download/big.bin").status_code == 507
    assert not list(pc.rglob("big.bin"))


def test_upload_keeps_bytes_and_date_and_can_be_undone(lan_client, engine, pc):
    data = os.urandom(5000)
    r = up(lan_client, "Download/a.bin", data, mtime="1600000000")
    dst = Path(r.json()["dst"])
    assert dst.read_bytes() == data and abs(dst.stat().st_mtime - 1_600_000_000) < 2
    assert up(lan_client, "Download/a.bin", data).status_code == 208
    engine.undo(r.json()["run_id"])
    assert not dst.exists() and [p.read_bytes() for p in engine.cfg.trash.rglob("a.bin")] == [data]


def test_undo_of_a_restore_run_is_refused(pc_client, engine):
    rid = engine.journal.start_run("restore")
    engine.journal.record(rid, "restore", "apps", None)
    engine.journal.finish_run(rid)
    assert pc_client.post(f"/api/undo/{rid}").status_code == 400
    with pytest.raises(RuntimeError, match="cannot be undone"):
        engine.undo(rid)
