"""A partial apps run (only=[...]) must never mark the other apps as removed, and a listing that lost a
third of the apps is treated as a bad listing."""
from __future__ import annotations

import json

import pytest

from phonevault.modules import apps as apps_mod


class FakeAdb:
    """Answers pm/dumpsys for a fixed set of packages; pulls write a 1-byte apk."""
    serial = "fake"

    def __init__(self, pkgs: list[str]):
        self.pkgs = pkgs

    def shell(self, command: str, timeout: int = 120) -> str:  # noqa: ARG002
        if command.startswith("pm list packages"):
            return "\n".join(f"package:/data/app/{p}-1/base.apk={p} versionCode:1" for p in self.pkgs)
        if command.startswith("pm path"):
            pkg = command.split()[-1]
            return f"package:/data/app/{pkg}-1/base.apk"
        return ""

    def describe(self) -> str:
        return "fake"

    def props(self) -> dict:
        return {}

    def pull(self, remote_path: str, local_path) -> None:  # noqa: ARG002
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(b"x")


@pytest.fixture
def quiet_details(monkeypatch):
    monkeypatch.setattr(apps_mod, "app_details", lambda adb, pkg: {"version_name": "1", "allow_backup": False})
    monkeypatch.setattr(apps_mod, "granted_permissions", lambda adb, pkg: [])


def _manifest(engine) -> dict:
    return json.loads((engine.cfg.root / engine.cfg.apps_subfolder / "apps.json").read_text(encoding="utf-8"))


def test_partial_run_keeps_the_other_apps(engine, quiet_details):
    all_pkgs = [f"com.app{i}" for i in range(30)]
    apps_mod.backup_apks(engine, FakeAdb(all_pkgs))
    assert len(_manifest(engine)["apps"]) == 30

    # "save these 2 new apps" must not touch the other 30
    apps_mod.backup_apks(engine, FakeAdb(all_pkgs + ["com.new1", "com.new2"]), only=["com.new1", "com.new2"])
    m = _manifest(engine)
    assert len(m["apps"]) == 32
    assert not any(a.get("removed_seen") for a in m["apps"])
    removed = (engine.cfg.root / engine.cfg.apps_subfolder / "apps-removed.txt")
    assert not removed.exists() or not [l for l in removed.read_text(encoding="utf-8").splitlines()[2:] if l.strip()]


def test_mass_vanish_is_a_bad_listing_not_many_uninstalls(engine, quiet_details):
    all_pkgs = [f"com.app{i}" for i in range(30)]
    apps_mod.backup_apks(engine, FakeAdb(all_pkgs))
    apps_mod.backup_apks(engine, FakeAdb(all_pkgs[:3]))          # listing lost 27 of 30
    m = _manifest(engine)
    assert len(m["apps"]) == 30
    assert not any(a.get("removed_seen") for a in m["apps"])


def test_a_few_real_uninstalls_are_still_recorded_and_the_list_is_rewritten(engine, quiet_details):
    all_pkgs = [f"com.app{i}" for i in range(30)]
    apps_mod.backup_apks(engine, FakeAdb(all_pkgs))
    apps_mod.backup_apks(engine, FakeAdb(all_pkgs[:-2]))        # 2 gone
    removed = engine.cfg.root / engine.cfg.apps_subfolder / "apps-removed.txt"
    assert "com.app28" in removed.read_text(encoding="utf-8") and "com.app29" in removed.read_text(encoding="utf-8")
    assert sum(1 for a in _manifest(engine)["apps"] if a.get("removed_seen")) == 2
    apps_mod.backup_apks(engine, FakeAdb(all_pkgs))             # they came back
    assert not [l for l in removed.read_text(encoding="utf-8").splitlines()[2:] if l.strip()]
    assert not any(a.get("removed_seen") for a in _manifest(engine)["apps"])
