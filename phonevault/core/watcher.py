"""The PC-side watcher: runs inside `phonevault serve --watch`.

Every `interval` minutes it looks for the phone on the cable or on the saved Wi-Fi address. When the phone is there and nothing else is running, it does a quiet incremental
pass: new apps? new top-level folders? then a full `all` sync if anything looks new or the last
full run is older than `full_every_hours`.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .config import Config, app_dir


class Watcher:
    def __init__(self, engine, cfg: Config, run_job, interval_min: int = 15, full_every_hours: int = 6):
        self.engine = engine
        self.cfg = cfg
        self.run_job = run_job            # callable(kind) that blocks until the job ends (holds engine.busy)
        self.interval = max(5, interval_min) * 60
        self.full_every = full_every_hours * 3600
        self.state_path = app_dir() / "whatsnew.json"
        self.state: dict[str, Any] = self._load()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- state -----------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {"known_packages": [], "known_folders": [], "new_apps": [], "new_folders": [],
                    "last_check": None, "last_full_backup": None, "last_link": "", "phone_seen_at": None,
                    "pending_apps_backup": False, "drive_ok": True, "notes": []}

    def _save(self) -> None:
        self.state_path.write_text(json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8")

    def whatsnew(self) -> dict[str, Any]:
        s = dict(self.state)
        s.pop("known_packages", None)
        s.pop("known_folders", None)
        s["watching"] = self._thread is not None and self._thread.is_alive()
        s["interval_min"] = self.interval // 60
        return s

    # ---- loop -------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="phonevault-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        time.sleep(20)   # let the server come up
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                self.engine.events.log_line(f"watcher: {e}", "warn")
            self._stop.wait(self.interval)

    def tick(self) -> dict[str, Any]:
        from ..transports import adb_for
        from .drivecheck import check_drive
        ev = self.engine.events
        self.state["last_check"] = time.time()
        dc = check_drive(self.cfg.root)
        self.state["drive_ok"] = bool(dc["ok"])
        if not dc["ok"]:
            self.state["notes"] = [f"target drive {dc['letter']}: {dc['reason']}"]
            self._save()
            return self.state
        if self.engine.busy.locked():
            self._save()
            return self.state
        adb = adb_for(self.cfg)
        if not adb.available():
            self._save()
            return self.state
        self.state["phone_seen_at"] = time.time()
        self.state["last_link"] = adb.describe()

        # what is new on the phone?
        pkgs = sorted(l.split(":", 1)[1] for l in adb.shell("pm list packages -3").split("\n") if l.startswith("package:"))
        known = set(self.state.get("known_packages") or [])
        if known:
            self.state["new_apps"] = sorted(set(pkgs) - known)
        self.state["known_packages"] = pkgs
        tops = sorted(f.name for f in adb.list_dir("") if f.is_dir and not f.name.startswith(".")
                      and f.name not in ("Android", "LOST.DIR"))
        known_f = set(self.state.get("known_folders") or [])
        if known_f:
            self.state["new_folders"] = sorted(set(tops) - known_f)
        self.state["known_folders"] = tops
        self.state["pending_apps_backup"] = bool(self.state["new_apps"])

        last_full = self.state.get("last_full_backup") or 0
        due = (time.time() - last_full) > self.full_every
        if due or self.state["new_apps"] or self.state["new_folders"]:
            ev.log_line(f"watcher: phone on {adb.describe()}; running a sync"
                        + (f" ({len(self.state['new_apps'])} new apps)" if self.state["new_apps"] else ""))
            self._save()
            self.run_job("all")
            self.state["last_full_backup"] = time.time()
            self.state["new_apps"] = []
            self.state["new_folders"] = []
            self.state["pending_apps_backup"] = False
        self._save()
        return self.state
