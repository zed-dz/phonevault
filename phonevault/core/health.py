"""One line per part of the backup: is it there, and how old is it?

Everything here is read from the PC folder and the journal. Nothing touches the phone.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .config import Config, app_dir

DAY = 86400


def _age(ts: float | None) -> str:
    if not ts:
        return "never"
    d = (time.time() - ts) / DAY
    if d < 1:
        return "today"
    if d < 2:
        return "yesterday"
    return f"{d:.0f} days ago"


def _newest(root: Path, pattern: str = "*") -> float | None:
    best = None
    if not root.exists():
        return None
    for p in root.rglob(pattern):
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if best is None or m > best:
            best = m
    return best


def _row(name: str, ok: bool | None, detail: str, fix: str = "", warn_only: bool = False) -> dict:
    status = "ok" if ok else ("warn" if (warn_only or ok is None) else "missing")
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def _last_run(journal_db: Path, kind: str) -> dict | None:
    import sqlite3
    if not journal_db.exists():
        return None
    con = sqlite3.connect(str(journal_db))
    try:
        r = con.execute("SELECT id, started, status, files, bytes FROM runs WHERE kind=? AND status IN ('done','cancelled') "
                        "AND undone=0 ORDER BY started DESC LIMIT 1", (kind,)).fetchone()
    finally:
        con.close()
    return {"id": r[0], "started": r[1], "status": r[2], "files": r[3], "bytes": r[4]} if r else None


def check(cfg: Config) -> list[dict]:
    root = cfg.root
    rows: list[dict] = []
    jdb = app_dir() / "journal.db"

    rows.append(_row("PC backup folder", root.exists(), str(root), "Plug the drive in or change pc_root"))
    from .drivecheck import check_drive
    dc = check_drive(root)
    if dc["health"] not in ("n/a", "skipped"):
        rows.append(_row("Target drive health", dc["ok"],
                         f"{dc['letter']}: health={dc['health']}, status={dc['status']}, free={dc['free_gb']} GB" + (f"; {dc['reason']}" if dc["reason"] else ""),
                         "stop writing to it; change pc_root to a healthy drive"))

    for kind, label, fix in (("gallery", "Gallery albums", "run: phonevault sync"),
                             ("files", "Phone files", "run: phonevault sync files")):
        r = _last_run(jdb, kind)
        if r:
            fresh = time.time() - r["started"] < 7 * DAY
            rows.append(_row(label, fresh, f"last run {_age(r['started'])}: {r['files']} files, {r['bytes'] / 1e6:.0f} MB ({r['status']})",
                             fix, warn_only=True))
        else:
            rows.append(_row(label, False, "never run", fix))

    apps_json = root / cfg.apps_subfolder / "apps.json"
    if apps_json.exists():
        try:
            m = json.loads(apps_json.read_text(encoding="utf-8"))
            apps = m.get("apps", [])
            with_data = sum(1 for a in apps if a.get("data_file"))
            no_data = [a["package"] for a in apps if not a.get("allow_backup")]
            fresh = time.time() - apps_json.stat().st_mtime < 30 * DAY
            rows.append(_row("App installers (APK)", fresh, f"{len(apps)} apps, saved {_age(apps_json.stat().st_mtime)}",
                             "run: phonevault apps", warn_only=True))
            abs_ = sorted((root / cfg.apps_subfolder).glob("android-backup-*.ab"), key=lambda q: q.stat().st_mtime)
            big = abs_[-1] if abs_ else None
            big_ok = bool(big) and big.stat().st_size > 1_000_000 and time.time() - big.stat().st_mtime < 30 * DAY
            rows.append(_row("App data archives", big_ok or with_data > 0,
                             (f"all-in-one archive {big.stat().st_size / 1e6:.0f} MB from {_age(big.stat().st_mtime)}; " if big else "")
                             + f"{with_data} per-app archives; {len(no_data)} apps refuse backup (Android rule)",
                             "dashboard > App data (one tap on the phone)", warn_only=True))
        except Exception as e:  # noqa: BLE001
            rows.append(_row("App installers (APK)", False, f"apps.json unreadable: {e}", "run: phonevault apps"))
    else:
        rows.append(_row("App installers (APK)", False, "never saved", "needs USB debugging, then: phonevault apps"))
        rows.append(_row("App data archives", False, "never saved", "needs USB debugging, then: phonevault apps --data"))

    removed = root / cfg.apps_subfolder / "apps-removed.txt"
    if removed.exists():
        n = len([l for l in removed.read_text(encoding="utf-8").splitlines()[2:] if l.strip()])
        rows.append(_row("Apps removed from the phone", True, f"{n} apps gone since the last run; installers kept",
                         "see Phone apps\\apps-removed.txt", warn_only=True))
    for sub, label, cmd in ((cfg.settings_subfolder, "Phone settings", "phonevault settings"),
                            (cfg.pim_subfolder, "Contacts, SMS, call log", "phonevault pim")):
        latest = root / sub / "LATEST.txt"
        if latest.exists():
            stamp = latest.read_text(encoding="utf-8").strip()
            d = root / sub / stamp
            m = d.stat().st_mtime if d.exists() else None
            rows.append(_row(label, bool(m) and time.time() - m < 30 * DAY, f"saved {_age(m)} ({stamp})", f"run: {cmd}", warn_only=True))
        else:
            rows.append(_row(label, False, "never saved", f"needs USB debugging, then: {cmd}"))

    new_albums = root / cfg.new_albums_subfolder
    n = sum(1 for p in new_albums.iterdir() if p.is_dir()) if new_albums.exists() else 0
    rows.append(_row("New albums waiting for review", n == 0, f"{n} albums in {cfg.new_albums_subfolder}",
                     "open the dashboard > Review new albums", warn_only=True))

    trash = cfg.trash
    tb = sum(p.stat().st_size for p in trash.rglob("*") if p.is_file()) if trash.exists() else 0
    rows.append(_row("Trash folder", tb < 5e9, f"{tb / 1e6:.0f} MB in {trash}", "empty it by hand when you are sure", warn_only=True))

    rows.append(_row("Wi-Fi link", bool(cfg.wifi_address), cfg.wifi_address or "not set",
                     "needs USB debugging once, then: phonevault wifi", warn_only=True))
    return rows
