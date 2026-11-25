"""restore-all: every step, in order, with a fake adb."""
from __future__ import annotations

import json

from phonevault.core.engine import Engine
from phonevault.modules import restore_all


class FakeAdb:
    name = "adb"
    has_shell = True
    def __init__(self):
        self.calls = []
    def describe(self):
        return "fake phone"
    def shell(self, cmd, timeout=120):
        self.calls.append(("shell", cmd))
        if cmd.startswith("pm list packages"):
            return "package:com.already\n"
        return ""
    def run(self, *args, timeout=120, check=True, binary=False):
        self.calls.append(("run", args))
        return ""
    def push(self, local, remote):
        self.calls.append(("push", remote))


def test_restore_everything_runs_all_steps_in_order(engine: Engine, monkeypatch):
    cfg = engine.cfg
    apps_root = cfg.root / cfg.apps_subfolder
    (apps_root / "com.new").mkdir(parents=True)
    (apps_root / "com.new" / "base.apk").write_bytes(b"apk")
    (apps_root / "apps.json").write_text(json.dumps({"apps": [
        {"package": "com.new", "files": ["base.apk"], "permissions": ["android.permission.CAMERA"]},
        {"package": "com.already", "files": ["base.apk"], "permissions": []}]}), encoding="utf-8")
    (apps_root / "android-backup-20260101-000000.ab").write_bytes(b"x" * 100)
    sdir = cfg.root / cfg.settings_subfolder / "20260101-000000"; sdir.mkdir(parents=True)
    (sdir / "settings.json").write_text(json.dumps({"settings": {"system": {"screen_brightness": "120", "android_id": "x"}}}), encoding="utf-8")
    (cfg.root / cfg.settings_subfolder / "LATEST.txt").write_text("20260101-000000")
    pdir = cfg.root / cfg.pim_subfolder / "20260101-000000"; pdir.mkdir(parents=True)
    (pdir / "contacts.vcf").write_text("BEGIN:VCARD\nEND:VCARD\n")
    (cfg.root / cfg.pim_subfolder / "LATEST.txt").write_text("20260101-000000")
    adb = FakeAdb()
    rep = restore_all.restore_everything(engine, adb)
    assert list(rep["steps"]) == ["apps", "appdata", "settings", "permissions", "contacts"]
    assert rep["steps"]["apps"]["installed"] == 1 and rep["steps"]["apps"]["already"] == 1
    assert rep["steps"]["appdata"]["all_in_one"].startswith("android-backup-")
    assert rep["steps"]["settings"]["applied"] == 1 and rep["steps"]["settings"]["skipped"] == 1
    assert rep["steps"]["permissions"]["granted"] == 1
    assert any(c[0] == "push" for c in adb.calls)
    run = engine.journal.run(rep["run_id"])
    assert run.kind == "restore" and run.status == "done"
