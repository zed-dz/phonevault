"""Apps without root: the installers (APK + splits) for every app, plus the
Android `adb backup` archive for app data where the app allows it.

What this gives you on a new phone:
- every app back, same version, with `phonevault restore apps` (adb install-multiple)
- app data for apps that set allowBackup=true (adb restore). Banks, WhatsApp and
  most big apps opt out; those need their own cloud backup (WhatsApp -> Google Drive).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from ..core.engine import Engine
from ..core.journal import unique_path
from ..core.names import pc_name
from ..transports.adb import AdbTransport, _NO_WINDOW

_PKG = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)*")
_PERM = re.compile(r"[A-Za-z0-9_.]+")


def apk_name(s: str) -> str:
    """The last part of a phone path as one safe file name ending in .apk."""
    n = pc_name(str(s).replace("\\", "/").rsplit("/", 1)[-1])
    return n if n.lower().endswith(".apk") else n + ".apk"


def list_apps(adb: AdbTransport, third_party_only: bool = True) -> list[dict]:
    flag = "-3" if third_party_only else ""
    out = adb.shell(f"pm list packages {flag} -f --show-versioncode 2>/dev/null || pm list packages {flag} -f")
    apps = []
    for line in out.split("\n"):
        m = re.match(r"package:(.+?)=([\w\.]+)(?: versionCode:(\d+))?", line.strip())
        if not m or not _PKG.fullmatch(m.group(2)):      # the name becomes a folder and a shell word
            continue
        apps.append({"package": m.group(2), "base_apk": m.group(1), "version_code": m.group(3) or ""})
    return apps


def app_details(adb: AdbTransport, pkg: str) -> dict:
    out = adb.shell(f"dumpsys package {pkg} 2>/dev/null")
    d = {"package": pkg}
    for key, pat in (("version_name", r"versionName=(\S+)"), ("version_code", r"versionCode=(\d+)"),
                     ("first_install", r"firstInstallTime=(.+)"), ("last_update", r"lastUpdateTime=(.+)"),
                     ("installer", r"installerPackageName=(\S+)"), ("data_dir", r"dataDir=(\S+)")):
        m = re.search(pat, out)
        if m:
            d[key] = m.group(1).strip()
    d["allow_backup"] = "ALLOW_BACKUP" in out or "allowBackup" in out
    d["flags"] = re.search(r"pkgFlags=\[(.*?)\]", out).group(1).strip() if re.search(r"pkgFlags=\[(.*?)\]", out) else ""
    d["allow_backup"] = "ALLOW_BACKUP" in d["flags"]
    return d


def granted_permissions(adb: AdbTransport, pkg: str) -> list[str]:
    """Runtime permissions the owner granted to this app (dumpsys 'granted=true' lines)."""
    out = adb.shell(f"dumpsys package {pkg} 2>/dev/null")
    perms = []
    for line in out.split("\n"):
        line = line.strip()
        if ": granted=true" in line and line.startswith("android.permission."):
            perms.append(line.split(":", 1)[0])
    return sorted(set(perms))


def restore_permissions(adb: AdbTransport, apps_root: Path, packages: list[str] | None = None) -> dict:
    """pm grant every saved runtime permission. Apps must be installed first."""
    manifest = json.loads((apps_root / "apps.json").read_text(encoding="utf-8"))
    res = {"granted": 0, "failed": 0}
    for a in manifest["apps"]:
        pkg = a["package"]
        if packages and pkg not in packages:
            continue
        if not _PKG.fullmatch(pkg):
            continue
        for perm in a.get("permissions", []):
            if not _PERM.fullmatch(str(perm)):
                continue
            try:
                adb.shell(f"pm grant {pkg} {perm} 2>/dev/null")
                res["granted"] += 1
            except Exception:  # noqa: BLE001
                res["failed"] += 1
    return res


def apk_paths(adb: AdbTransport, pkg: str) -> list[str]:
    out = adb.shell(f"pm path {pkg}")
    return [line.split(":", 1)[1].strip() for line in out.split("\n") if line.startswith("package:")]


def backup_app_data(adb: AdbTransport, pkg: str, out: Path) -> Path:
    """One app's data as its own .ab archive (adb backup). The phone shows 'Back up my data': tap it.
    Only apps with allow_backup=true answer with real data; others produce an empty 41-byte header."""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = adb._base() + ["backup", "-f", str(out), "-noapk", "-obb", pkg]
    subprocess.run(cmd, capture_output=True, timeout=3600, creationflags=_NO_WINDOW)
    if not out.exists() or out.stat().st_size <= 41:
        out.unlink(missing_ok=True)
        raise RuntimeError("no data came back (app refuses backup, or 'Back up my data' was not tapped)")
    return out


def backup_apks(engine: Engine, adb: AdbTransport, only: list[str] | None = None, with_data: bool = False) -> dict:
    """Pull every third-party app's APK set into <pc_root>/Phone apps/<pkg>/. Skips unchanged versions.
    with_data=True also saves <pkg>/data-<stamp>.ab for every app that allows backup (one phone tap per app)."""
    ev = engine.events
    engine.ready_to_write()
    root = engine.safe_dir(engine.cfg.root / engine.cfg.apps_subfolder)
    manifest_path = root / "apps.json"
    old: dict[str, dict] = {}
    if manifest_path.exists():
        try:
            old = {a["package"]: a for a in json.loads(manifest_path.read_text(encoding="utf-8")).get("apps", [])}
        except Exception:
            old = {}
    apps = list_apps(adb)
    if only:
        apps = [a for a in apps if a["package"] in only]
    ev.reset("apps", adb.describe())
    ev.set(phase="copying", total_files=len(apps), message=f"{len(apps)} apps")
    rid = engine.journal.start_run("apps", note=f"{len(apps)} apps")
    ev.set(run_id=rid)
    result = {"pulled": 0, "unchanged": 0, "failed": 0, "apps": []}
    for n, a in enumerate(apps, 1):
        pkg = a["package"]
        ev.set(current=pkg, done_files=n - 1)
        try:
            det = app_details(adb, pkg)
            a.update(det)
            try:
                a["permissions"] = granted_permissions(adb, pkg)
            except Exception:  # noqa: BLE001
                a["permissions"] = []
            prev = old.get(pkg)
            dest = root / pkg
            engine.journal.guard(dest)
            if prev and prev.get("complete") and prev.get("files") and prev.get("version_code") == a.get("version_code") \
                    and all((dest / pc_name(x)).is_file() for x in prev["files"]):
                a["files"], a["complete"] = prev["files"], True
                result["unchanged"] += 1
                ev.album_bump("unchanged", "copied")
            else:
                # Pull the whole set aside first. One missing split and the app is not saved, and the old set stays.
                hold = engine.safe_dir(engine.staging / rid / pkg)
                got = []
                try:
                    paths = apk_paths(adb, pkg)
                    if not paths:
                        raise RuntimeError("the phone lists no installer for this app")
                    for ap in paths:
                        local = unique_path(hold / apk_name(ap))
                        adb.pull_abs(ap, local)
                        if not local.is_file() or not local.stat().st_size:
                            raise RuntimeError(f"{apk_name(ap)} did not arrive")
                        got.append((ap, local))
                    if dest.exists():
                        for stale in dest.glob("*.apk"):
                            q = engine.journal.move_to_trash(stale)
                            engine.journal.record(rid, "quarantine", str(stale), str(q), album=pkg)
                    dest.mkdir(parents=True, exist_ok=True)
                    for ap, local in got:
                        os.replace(local, dest / local.name)
                        engine.journal.record(rid, "copy", ap, str(dest / local.name), (dest / local.name).stat().st_size, album=pkg)
                finally:
                    for x in hold.iterdir():        # half downloads; the phone still has them
                        x.unlink(missing_ok=True)
                    hold.rmdir()
                a["files"], a["complete"] = [local.name for _, local in got], True
                result["pulled"] += 1
                ev.emit("file_done", path=pkg, status="ok", dst=str(dest), album="apps")
            if with_data and a.get("allow_backup"):
                ev.set(current=f"{pkg} data: tap 'Back up my data' on the phone")
                try:
                    stamp = time.strftime("%Y%m%d-%H%M%S")
                    ab = backup_app_data(adb, pkg, dest / f"data-{stamp}.ab")
                    for old_ab in dest.glob("data-*.ab"):
                        if old_ab != ab:
                            q = engine.journal.move_to_trash(old_ab)
                            engine.journal.record(rid, "quarantine", str(old_ab), str(q), album=pkg)
                    a["data_file"] = ab.name
                    a["data_bytes"] = ab.stat().st_size
                    engine.journal.record(rid, "copy", f"adb backup {pkg}", str(ab), ab.stat().st_size, album=pkg)
                    result["data_saved"] = result.get("data_saved", 0) + 1
                except Exception as e:  # noqa: BLE001
                    a["data_error"] = str(e)
                    result["data_failed"] = result.get("data_failed", 0) + 1
            elif prev and prev.get("data_file") and (dest / prev["data_file"]).exists():
                a["data_file"] = prev["data_file"]
                a["data_bytes"] = prev.get("data_bytes")
            result["apps"].append(a)
        except Exception as e:  # noqa: BLE001
            result["failed"] += 1
            keep = dict(old[pkg]) if pkg in old else {**a, "files": []}     # the last good set, if there is one
            keep.pop("removed_seen", None)                                  # it is on the phone now
            keep.update(complete=False, error=str(e))                       # so the next run tries again
            result["apps"].append(keep)
            engine.journal.record(rid, "copy", pkg, None, status="failed", note=str(e), album=pkg)
            ev.emit("file_failed", path=pkg, error=str(e), album="apps")
        ev.set(done_files=n)
    # "backup before uninstall": apps that were in the last manifest and are gone now keep their APK folder,
    # and get listed, so a removed app can always be put back.
    now_pkgs = {a["package"] for a in result["apps"]}
    removed = sorted(p for p in old if p not in now_pkgs)
    if only:
        # A partial run ("save these 3 new apps") knows nothing about the rest: carry the old entries over
        # untouched and never call them removed.
        for p in removed:
            result["apps"].append(dict(old[p]))
        removed = []
    elif old and len(old) >= 20 and len(removed) > len(old) * 0.3:
        # Mass-vanish valve: a listing that lost a third of the apps is a bad listing, not a hundred uninstalls.
        ev.log_line(f"apps: {len(removed)} of {len(old)} apps missing from the listing; keeping them (not marked removed)", "warn")
        for p in removed:
            result["apps"].append(dict(old[p]))
        removed = []
    if old and not only:
        stamp = time.strftime("%Y-%m-%d %H:%M")
        added = sorted(p for p in now_pkgs if p not in old)
        changed = sorted(p for p in now_pkgs if p in old and old[p].get("version_code") != next(a for a in result["apps"] if a["package"] == p).get("version_code"))
        with (root / "apps-changelog.txt").open("a", encoding="utf-8") as f:
            f.write(f"{stamp}  added={len(added)} removed={len(removed)} updated={len(changed)}\n")
            for p in added:
                f.write(f"  + {p}\n")
            for p in removed:
                f.write(f"  - {p}  (APK kept in Phone apps\\{p})\n")
            for p in changed:
                f.write(f"  ~ {p}\n")
        # Always rewritten, so a stale list from an earlier run cannot outlive the apps that came back.
        (root / "apps-removed.txt").write_text(
            "Apps no longer on the phone. Their installers are still in this folder; restore with: phonevault restore apps --only <package>\n\n"
            + "\n".join(removed), encoding="utf-8")
        for p in removed:   # keep the old entry so the APK stays restorable
            entry = dict(old[p]); entry["removed_seen"] = stamp
            result["apps"].append(entry)
    manifest = {"device": device_card(adb), "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"), "apps": result["apps"]}
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    (root / "apps-list.txt").write_text(
        "\n".join(f"{a['package']}\t{a.get('version_name','')}\t"
                  f"{'data saved' if a.get('data_file') else ('data possible' if a.get('allow_backup') else 'app refuses data backup')}"
                  for a in result["apps"]), encoding="utf-8")
    refusing = [a["package"] for a in result["apps"] if not a.get("allow_backup")]
    (root / "apps-refusing-data-backup.txt").write_text(
        "These apps set allowBackup=false. No tool without root can copy their data. "
        "Use each app's own export/cloud backup (WhatsApp > Chats > Chat backup, etc.).\n\n" + "\n".join(refusing),
        encoding="utf-8")
    try:
        (engine.staging / rid).rmdir()
    except OSError:
        pass
    engine.journal.finish_run(rid, "partial" if result["failed"] else "done")
    ev.set(phase="done", finished=time.time(), current="", run_id=rid,
           message=f"{result['pulled']} apps saved, {result['unchanged']} unchanged, {result['failed']} failed"
                   + (" (tried again next time)" if result["failed"] else "")
                   + (f", data for {result.get('data_saved', 0)} apps" if with_data else ""))
    ev.emit("run_done", run_id=rid, status="done")
    result["run_id"] = rid
    result["manifest"] = str(manifest_path)
    return result


def adb_backup(engine: Engine, adb: AdbTransport, include_apk: bool = False, shared: bool = False) -> Path:
    """Android's own full backup. The phone shows a screen: 'Back up my data'. Tap it."""
    engine.ready_to_write()
    root = engine.safe_dir(engine.cfg.root / engine.cfg.apps_subfolder)
    out = root / f"android-backup-{time.strftime('%Y%m%d-%H%M%S')}.ab"
    args = ["backup", "-f", str(out), "-all", "-obb"]
    args.append("-apk" if include_apk else "-noapk")
    if shared:
        args.append("-shared")
    engine.events.set(phase="copying", message="Tap 'Back up my data' on the phone", current=str(out))
    cmd = adb._base() + args
    p = subprocess.run(cmd, capture_output=True, timeout=4 * 3600, creationflags=_NO_WINDOW)
    if not out.exists() or out.stat().st_size < 100:
        raise RuntimeError("adb backup made no file. Did you tap 'Back up my data' on the phone? "
                           + p.stderr.decode("utf-8", "replace").strip())
    rid = engine.journal.start_run("adb-backup", note=str(out))
    engine.journal.record(rid, "copy", "adb backup -all", str(out), out.stat().st_size)
    engine.journal.finish_run(rid)
    engine.events.set(phase="done", message=f"Android backup saved: {out.name} ({out.stat().st_size // 1_000_000} MB)")
    return out


def restore_apks(adb: AdbTransport, apps_root: Path, packages: list[str] | None = None) -> dict:
    manifest = json.loads((apps_root / "apps.json").read_text(encoding="utf-8"))
    res = {"installed": [], "failed": {}}
    for a in manifest["apps"]:
        pkg = a["package"]
        if packages and pkg not in packages:
            continue
        if not _PKG.fullmatch(pkg):
            continue
        files = [str(apps_root / pkg / apk_name(f)) for f in a.get("files", [])]
        if not files:
            continue
        try:
            if len(files) == 1:
                adb.run("install", "-r", files[0], timeout=600)
            else:
                adb.run("install-multiple", "-r", *files, timeout=600)
            res["installed"].append(pkg)
        except Exception as e:  # noqa: BLE001
            res["failed"][pkg] = str(e)
    return res


def restore_app_data(adb: AdbTransport, apps_root: Path, packages: list[str] | None = None) -> dict:
    """Per-app restore from <pkg>/data-*.ab. The phone shows 'Restore my data' once per app."""
    manifest = json.loads((apps_root / "apps.json").read_text(encoding="utf-8"))
    res = {"restored": [], "skipped": [], "failed": {}}
    for a in manifest["apps"]:
        pkg = a["package"]
        if packages and pkg not in packages:
            continue
        ab = apps_root / pkg / pc_name(a["data_file"]) if a.get("data_file") and _PKG.fullmatch(pkg) else None
        if not ab or not ab.exists():
            res["skipped"].append(pkg)
            continue
        try:
            adb.run("restore", str(ab), timeout=3600)
            res["restored"].append(pkg)
        except Exception as e:  # noqa: BLE001
            res["failed"][pkg] = str(e)
    return res


def adb_restore(adb: AdbTransport, ab_file: Path) -> None:
    """Phone shows 'Restore my data'. Tap it."""
    adb.run("restore", str(ab_file), timeout=4 * 3600)


def device_card(adb: AdbTransport) -> dict:
    p = adb.props()
    return {
        "model": p.get("ro.product.model", ""), "brand": p.get("ro.product.brand", ""),
        "device": p.get("ro.product.device", ""), "android": p.get("ro.build.version.release", ""),
        "sdk": p.get("ro.build.version.sdk", ""),
        "build": p.get("ro.build.display.id", ""), "serial": adb.serial,
    }
