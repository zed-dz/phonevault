"""Set up a new phone: everything back in one guided run.

Order matters: installers first (apps must exist before their data), then the all-in-one data
archive, then per-app archives, then settings, then permissions, then the contacts import dialog.
Each step is journaled as a 'restore' run and reported through the event bus so the dashboard and
the command line show progress. Tap "Restore my data" on the phone when it asks.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..core.engine import Engine
from ..transports.adb import AdbTransport
from . import apps, pim, settings


def restore_everything(engine: Engine, adb: AdbTransport, skip: set[str] | None = None) -> dict:
    cfg = engine.cfg
    ev = engine.events
    skip = skip or set()
    ev.reset("restore-all", adb.describe())
    steps = ["apps", "appdata", "settings", "permissions", "contacts"]
    ev.set(phase="copying", total_files=len(steps), message="Setting up this phone from the vault")
    rid = engine.journal.start_run("restore", note=f"new phone: {adb.describe()}")
    ev.set(run_id=rid)
    report: dict = {"run_id": rid, "steps": {}}
    apps_root = cfg.root / cfg.apps_subfolder

    def step(n: int, name: str, fn):
        if name in skip:
            report["steps"][name] = "skipped"
            return
        ev.set(done_files=n, current=name, message=f"Step {n + 1}/{len(steps)}: {name}")
        ev.log_line(f"restore: {name}")
        try:
            report["steps"][name] = fn()
            engine.journal.record(rid, "restore", name, None, status="ok", note=json.dumps(report["steps"][name])[:400])
        except Exception as e:  # noqa: BLE001
            report["steps"][name] = {"error": str(e)}
            engine.journal.record(rid, "restore", name, None, status="failed", note=str(e)[:400])
            ev.log_line(f"restore {name} failed: {e}", "error")

    # 1. installers
    def do_apps():
        if not (apps_root / "apps.json").exists():
            raise RuntimeError("no apps.json in the vault; run `phonevault apps` on the old phone first")
        manifest = json.loads((apps_root / "apps.json").read_text(encoding="utf-8"))
        installed = {l.split(":", 1)[1] for l in adb.shell("pm list packages").split("\n") if l.startswith("package:")}
        todo = [a["package"] for a in manifest["apps"] if a.get("files") and a["package"] not in installed and "removed_seen" not in a]
        ev.set(message=f"Installing {len(todo)} apps")
        res = apps.restore_apks(adb, apps_root, todo) if todo else {"installed": [], "failed": {}}
        return {"installed": len(res["installed"]), "failed": len(res["failed"]), "already": len(manifest["apps"]) - len(todo)}
    step(0, "apps", do_apps)

    # 2. data: the newest all-in-one archive, then any per-app archives
    def do_appdata():
        abs_ = sorted(apps_root.glob("android-backup-*.ab"), key=lambda q: q.stat().st_mtime)
        out = {"all_in_one": None, "per_app": None}
        if abs_:
            ev.set(message=f"Restoring app data archive {abs_[-1].name}: tap 'Restore my data' on the phone")
            apps.adb_restore(adb, abs_[-1])
            out["all_in_one"] = abs_[-1].name
        per = apps.restore_app_data(adb, apps_root)
        out["per_app"] = {"restored": len(per["restored"]), "skipped": len(per["skipped"]), "failed": len(per["failed"])}
        return out
    step(1, "appdata", do_appdata)

    # 3. settings
    def do_settings():
        base = cfg.root / cfg.settings_subfolder
        stamp = (base / "LATEST.txt").read_text(encoding="utf-8").strip()
        return settings.apply_settings(adb, base / stamp / "settings.json")
    step(2, "settings", do_settings)

    # 4. permissions
    step(3, "permissions", lambda: apps.restore_permissions(adb, apps_root))

    # 5. contacts: opens the import chooser on the phone
    def do_contacts():
        base = cfg.root / cfg.pim_subfolder
        stamp = (base / "LATEST.txt").read_text(encoding="utf-8").strip()
        pim.restore_contacts(adb, base / stamp / "contacts.vcf")
        return {"opened": "contacts import dialog; pick Contacts on the phone"}
    step(4, "contacts", do_contacts)

    engine.journal.finish_run(rid, "done")
    failed = [k for k, v in report["steps"].items() if isinstance(v, dict) and "error" in v]
    ev.set(phase="done", finished=time.time(), done_files=len(steps), current="",
           message="New phone set up: " + ", ".join(f"{k} {'failed' if k in failed else 'ok'}" for k in steps if k not in skip)
           + ". SMS: import sms.xml from the contacts folder with an SMS restore app.")
    ev.emit("run_done", run_id=rid, status="done")
    return report
