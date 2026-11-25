"""Phone settings without root: the three Android settings tables, device facts,
installed-app list, and a ready-to-run restore script.

What comes back on a new phone: display, sound, accessibility, input, developer
toggles, most system switches. What does NOT: Wi-Fi passwords, accounts, app
sign-ins, launcher layout. Those sit in Google's own backup (Settings > Google >
Backup), so keep that on too.
"""
from __future__ import annotations

import json
import re
import shlex
import time
from pathlib import Path

from ..core.engine import Engine
from ..transports.adb import AdbTransport
from .apps import device_card

# Keys that must never be copied to another phone (identity, radios, hardware calibration).
NEVER_RESTORE_PREFIXES = (
    "android_id", "bluetooth_address", "bluetooth_name", "device_name", "wifi_", "adb_", "mock_location",
    "lock_screen", "lockscreen", "user_setup_complete", "device_provisioned", "advertising_id",
    "unknown_sources", "install_non_market", "assisted_gps", "cdma_", "preferred_network_mode", "mobile_data",
    "data_roaming", "sim_", "subscription_", "multi_sim", "miui_", "key_miui", "oem_", "ota_",
)
NAMESPACES = ("system", "secure", "global")


def _parse(out: str) -> dict[str, str]:
    d = {}
    for line in out.split("\n"):
        if "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.rstrip("\r")
    return d


def backup_settings(engine: Engine, adb: AdbTransport) -> Path:
    ev = engine.events
    ev.reset("settings", adb.describe())
    ev.set(phase="copying", total_files=6, message="Reading settings tables")
    root = engine.cfg.root / engine.cfg.settings_subfolder
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = root / stamp
    engine.ready_to_write()
    engine.safe_dir(out_dir)
    rid = engine.journal.start_run("settings", note=str(out_dir))

    tables: dict[str, dict[str, str]] = {}
    for n, ns in enumerate(NAMESPACES, 1):
        ev.set(current=f"settings {ns}", done_files=n - 1)
        tables[ns] = _parse(adb.shell(f"settings list {ns}"))
    extras = {
        "props": adb.props(),
        "packages_all": [l.split(":", 1)[1] for l in adb.shell("pm list packages").split("\n") if l.startswith("package:")],
        "packages_user": [l.split(":", 1)[1] for l in adb.shell("pm list packages -3").split("\n") if l.startswith("package:")],
        "disabled_packages": [l.split(":", 1)[1] for l in adb.shell("pm list packages -d").split("\n") if l.startswith("package:")],
        "default_launcher": adb.shell("cmd shortcut get-default-launcher 2>/dev/null").strip(),
        "default_sms": adb.shell("settings get secure sms_default_application").strip(),
        "accounts_types": sorted(set(l.strip() for l in adb.shell("dumpsys account 2>/dev/null | grep -o 'type=[^,}]*' | sort -u").split("\n") if l.strip())),
        "locale": adb.shell("getprop persist.sys.locale").strip(),
        "timezone": adb.shell("getprop persist.sys.timezone").strip(),
        "display": adb.shell("wm size; wm density").strip(),
        "notification_policy": adb.shell("cmd notification allow_listener 2>/dev/null; settings get secure enabled_notification_listeners").strip(),
    }
    ev.set(current="device facts", done_files=4)
    snapshot = {"device": device_card(adb), "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"), "settings": tables, "extras": extras}
    json_path = out_dir / "settings.json"
    json_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    engine.journal.record(rid, "copy", "settings", str(json_path), json_path.stat().st_size)

    ev.set(current="restore script", done_files=5)
    script = write_restore_script(snapshot, out_dir / "restore-settings.cmd", adb.adb)
    engine.journal.record(rid, "copy", "restore script", str(script), script.stat().st_size)

    # Wallpaper + ringtones + fonts live as files; the files sync covers Ringtones/ and Fonts/.
    wall = adb.shell("dumpsys wallpaper 2>/dev/null | head -40")
    (out_dir / "wallpaper-info.txt").write_text(wall, encoding="utf-8")

    n = sum(len(t) for t in tables.values())
    if n > 0:
        (root / "LATEST.txt").write_text(stamp, encoding="utf-8")   # an empty read never replaces a good one
    engine.journal.finish_run(rid, "done" if n > 0 else "failed")
    ev.set(phase="done", finished=time.time(), done_files=6, current="", run_id=rid,
           message=f"{n} settings saved to {out_dir}")
    ev.emit("run_done", run_id=rid, status="done")
    return out_dir


def restorable(ns: str, key: str) -> bool:
    k = key.lower()
    return not any(k.startswith(p) for p in NEVER_RESTORE_PREFIXES)


# What may go into a .cmd file. A setting's value comes from the phone, and any app allowed to write settings
# can choose it: a quote, $ or ` in it must never become a command on the PC or on the new phone.
_SCRIPT_KEY = re.compile(r"[A-Za-z0-9_.\-]+")
_SCRIPT_VALUE = re.compile(r"[\w ./:,@+=%;()\[\]{}#~*?\-]+", re.ASCII)


def write_restore_script(snapshot: dict, path: Path, adb_path: str) -> Path:
    lines = ["@echo off", "rem PhoneVault settings restore. Run with the NEW phone on USB debugging.",
             "rem Only safe keys are included. Identity, radio and Wi-Fi keys are skipped on purpose.",
             f'set ADB="{adb_path or "adb"}"', "%ADB% wait-for-device", ""]
    n = skipped = 0
    for ns in NAMESPACES:
        for k, v in snapshot["settings"].get(ns, {}).items():
            if not restorable(ns, k) or v in ("null", ""):
                continue
            if not _SCRIPT_KEY.fullmatch(k) or not _SCRIPT_VALUE.fullmatch(v):
                skipped += 1
                continue
            safe = v.replace("%", "%%")
            lines.append(f"%ADB% shell \"settings put {ns} {k} '{safe}'\"")
            n += 1
    lines += ["", f"echo Restored {n} settings. {skipped} with special characters left out: phonevault restore settings", "pause"]
    path.write_bytes("\r\n".join(lines).encode("utf-8"))
    return path


def apply_settings(adb: AdbTransport, snapshot_json: Path, namespaces: tuple[str, ...] = NAMESPACES) -> dict:
    snap = json.loads(Path(snapshot_json).read_text(encoding="utf-8"))
    res = {"applied": 0, "skipped": 0, "failed": {}}
    for ns in namespaces:
        for k, v in snap["settings"].get(ns, {}).items():
            if not restorable(ns, k) or v in ("null", ""):
                res["skipped"] += 1
                continue
            try:
                if not _SCRIPT_KEY.fullmatch(k):
                    raise ValueError("odd key name")
                adb.shell(f"settings put {ns} {k} {shlex.quote(v)}")
                res["applied"] += 1
            except Exception as e:  # noqa: BLE001
                res["failed"][f"{ns}/{k}"] = str(e)
    return res
