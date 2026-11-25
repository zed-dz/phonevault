"""phonevault <command>. Every command prints a plain-words result and a bar while it works."""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .core.config import Config
from .core.engine import Engine, Plan
from .core.events import Events
from .transports import adb_for, pick_transport, probe
from .transports.adb import AdbTransport


def _rich():
    try:
        from rich.console import Console
        from rich.progress import (BarColumn, DownloadColumn, Progress, TextColumn, TimeRemainingColumn,
                                   TransferSpeedColumn)
        from rich.table import Table
        return Console(), Progress, Table, (BarColumn, DownloadColumn, TextColumn, TimeRemainingColumn, TransferSpeedColumn)
    except ImportError:
        return None, None, None, None


def live_bar(events: Events, stop: threading.Event) -> None:
    """A rich progress bar fed by the event bus. Falls back to plain lines."""
    console, Progress, _, cols = _rich()
    if not Progress:
        last = ""
        while not stop.is_set():
            s = events.state
            line = f"{s['phase']}: {s['done_files']}/{s['total_files']} {s['current'][-60:]}"
            if line != last:
                print(line)
                last = line
            time.sleep(0.5)
        return
    BarColumn, DownloadColumn, TextColumn, TimeRemainingColumn, TransferSpeedColumn = cols
    with Progress(TextColumn("[bold]{task.fields[phase]}"), BarColumn(bar_width=30),
                  TextColumn("{task.completed}/{task.total} files"), DownloadColumn(), TransferSpeedColumn(),
                  TimeRemainingColumn(), TextColumn("{task.fields[cur]}"), console=console, transient=False) as pr:
        task = pr.add_task("run", total=1, phase="start", cur="")
        bytes_task = pr.add_task("bytes", total=1, phase="bytes", cur="", visible=False)
        while not stop.is_set():
            s = events.state
            pr.update(task, total=max(s["total_files"], 1), completed=s["done_files"], phase=s["phase"],
                      cur=(s["current"] or s["message"])[-70:])
            pr.update(bytes_task, total=max(s["total_bytes"], 1), completed=s["done_bytes"] + s.get("file_done_bytes", 0),
                      visible=s["total_bytes"] > 0)
            time.sleep(0.3)
        s = events.state
        pr.update(task, total=max(s["total_files"], 1), completed=s["done_files"], phase=s["phase"], cur=s["message"])


def with_bar(engine: Engine, fn):
    stop = threading.Event()
    t = threading.Thread(target=live_bar, args=(engine.events, stop), daemon=True)
    t.start()
    try:
        return fn()
    finally:
        stop.set()
        t.join(timeout=2)


def print_plan(plan: Plan) -> None:
    console, _, Table, _ = _rich()
    s = plan.summary()
    if Table and console:
        t = Table(title=f"{plan.kind}: {s['copy']} new files, {s['bytes'] / 1e6:.0f} MB, {s['check']} to compare")
        for c in ("album", "new", "already here", "to compare", "goes to", "match"):
            t.add_column(c)
        for a in s["albums"]:
            t.add_row(a["album"], str(a["copy"]), str(a["exists"]), str(a["check"]), a["dst"], a["how"])
        console.print(t)
    else:
        print(json.dumps(s, indent=1, ensure_ascii=False))


# ---- commands ------------------------------------------------------------
def cmd_status(args, cfg: Config) -> int:
    info = probe(cfg)
    print(f"PC folder : {cfg.pc_root}  ({'exists' if cfg.root.exists() else 'MISSING'})")
    print(f"adb       : {'OK ' + info['adb_desc'] if info['adb'] else 'no device'}"
          + ("  <- phone says UNAUTHORIZED: tap Allow on the phone" if info.get("adb_unauthorized") else ""))
    for d in info["adb_devices"]:
        print(f"            {d['serial']}  {d['state']}  {d.get('model', '')}")
    if info.get("adb_error"):
        print(f"            {info['adb_error']}")
    if not cfg.adb:
        print("            adb is not installed. Windows: winget install Google.PlatformTools")
    if args.mtp:
        from .transports.mtp import MtpTransport
        print(f"MTP       : {'OK' if MtpTransport(cfg.phone_name_hint).available() else 'off (set phone_name_hint)' if not cfg.phone_name_hint else 'not visible'}")
    print(f"Wi-Fi addr: {cfg.wifi_address or '(not set, run: phonevault wifi)'}")
    e = Engine(cfg)
    print(f"PC index  : {e.index.count()} files known")
    runs = e.journal.runs(5)
    if runs:
        print("Last runs :")
        for r in runs:
            print(f"  {r.id}  {r.kind:8} {r.status:9} {r.files} files  {r.bytes / 1e6:.0f} MB" + ("  (undone)" if r.undone else ""))
    return 0


def cmd_scan(args, cfg: Config) -> int:
    from .modules import media
    engine = Engine(cfg)
    tr = pick_transport(cfg, args.link)
    print(f"Link: {tr.describe()}")
    plan = with_bar(engine, lambda: media.plan_gallery(engine, tr, args.roots) if args.what == "gallery"
                    else media.plan_files(engine, tr, args.roots) if args.what == "files"
                    else media.plan_extdata(engine, tr))
    print_plan(plan)
    if args.json:
        Path(args.json).write_text(json.dumps([i.to_dict() for i in plan.items], indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"Plan written to {args.json}")
    return 0


def cmd_sync(args, cfg: Config) -> int:
    from .modules import media
    engine = Engine(cfg)
    tr = pick_transport(cfg, args.link)
    print(f"Link: {tr.describe()}")
    whats = (["gallery", "files"] + (["extdata"] if getattr(tr, "has_shell", False) else [])) if args.what == "all" else [args.what]
    for what in whats:
        plan, rid = with_bar(engine, lambda: media.sync(engine, tr, what, dry_run=args.dry_run, roots=args.roots))
        print_plan(plan)
        s = engine.events.state
        print(f"{what}: {s['message']}" + (f"   run id: {rid}   (undo with: phonevault undo {rid})" if rid else ""))
    return 0


def _adb(cfg: Config) -> AdbTransport:
    tr = pick_transport(cfg, "adb")
    if not getattr(tr, "has_shell", False):
        raise RuntimeError("This needs adb (USB debugging).")
    return tr  # type: ignore[return-value]


def cmd_apps(args, cfg: Config) -> int:
    from .modules import apps
    engine = Engine(cfg)
    adb = _adb(cfg)
    if args.data:
        print("Look at the phone: it asks 'Back up my data?' once per app. Tap it each time (no password needed).")
    res = with_bar(engine, lambda: apps.backup_apks(engine, adb, args.only, with_data=args.data))
    print(f"Apps: {res['pulled']} saved, {res['unchanged']} unchanged, {res['failed']} failed -> {res['manifest']}")
    if args.data:
        print(f"App data: {res.get('data_saved', 0)} saved, {res.get('data_failed', 0)} refused/failed. "
              "Apps that refuse any data backup are listed in Phone apps\\apps-refusing-data-backup.txt")
    if getattr(args, "all_in_one", False):
        print("Also one big Android archive (tap 'Back up my data' once more):")
        out = apps.adb_backup(engine, adb, include_apk=False)
        print(f"Android full data backup: {out}")
    return 0


def cmd_settings(args, cfg: Config) -> int:
    from .modules import settings
    engine = Engine(cfg)
    adb = _adb(cfg)
    out = with_bar(engine, lambda: settings.backup_settings(engine, adb))
    print(f"Settings saved to {out}")
    return 0


def cmd_pim(args, cfg: Config) -> int:
    from .modules import pim
    engine = Engine(cfg)
    adb = _adb(cfg)
    res = with_bar(engine, lambda: pim.backup_pim(engine, adb))
    print(f"Saved: {res['counts']}  errors: {res['errors'] or 'none'} -> {res['dir']}")
    return 0


def cmd_autorun(args, cfg: Config) -> int:
    """Wait until the phone is plugged in AND you tapped Allow, then do the full backup by itself."""
    adb = adb_for(cfg)
    print("Waiting for the phone. Plug the cable, unlock, tap Allow on 'USB debugging'. Ctrl+C to stop waiting.")
    told_unauth = False
    while True:
        if adb.available():
            break
        if adb.unauthorized() and not told_unauth:
            print("Phone found but not allowed yet: tap Allow on the phone.")
            told_unauth = True
        time.sleep(args.every)
    print(f"Phone ready: {adb.describe()}. Starting the full backup.")
    if args.wifi:
        try:
            addr = adb.enable_wifi()
            cfg.wifi_address = addr
            cfg.save()
            print(f"Wi-Fi link set: {addr}")
        except Exception as e:  # noqa: BLE001
            print(f"Wi-Fi link skipped: {e}")
    ns = argparse.Namespace(**vars(args))
    ns.link = "adb"
    ns.what = "all"
    ns.dry_run = False
    ns.roots = None
    ns.data = args.data
    return cmd_backup(ns, cfg)


def cmd_backup(args, cfg: Config) -> int:
    """Everything in one go. If the dashboard is running it owns the journal: hand the job to it instead
    of running a second engine on the same files."""
    from .core.config import devices_off
    engine = Engine(cfg)
    busy = engine.read_only
    engine.close()
    if busy and not devices_off():
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{cfg.dashboard_port}/api/watch/tick", method="POST",
                                     headers={"X-PhoneVault-Token": cfg.api_token})
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:  # noqa: BLE001
            print(f"Another PhoneVault is running and did not take the job ({e}). Wait for it, then try again.")
            return 1
        print("The dashboard is running: asked it to sync now.")
        return 0
    rc = 0
    for name, fn in (("sync", cmd_sync), ("apps", cmd_apps), ("settings", cmd_settings), ("pim", cmd_pim)):
        try:
            ns = argparse.Namespace(**vars(args))
            ns.what = "all"
            ns.only = None
            ns.data = args.data
            rc |= fn(ns, cfg)
        except Exception as e:  # noqa: BLE001
            print(f"{name}: {e}")
            rc = 1
    return rc


def cmd_undo(args, cfg: Config) -> int:
    engine = Engine(cfg)
    rid = args.run_id
    if rid == "last":
        runs = [r for r in engine.journal.runs(50) if r.kind not in ("undo", "restore") and not r.undone and r.status != "running"]
        if not runs:
            print("Nothing to undo.")
            return 1
        rid = runs[0].id
    r = engine.journal.run(rid)
    if not r:
        print(f"No run {rid}")
        return 1
    if r.undone:
        print(f"Run {rid} was already undone.")
        return 1
    print(f"Undoing {rid} ({r.kind}, {r.files} files). Files go to {cfg.trash}, nothing is deleted.")
    urid = with_bar(engine, lambda: engine.undo(rid))
    print(f"Done. Undo run id: {urid} (undo the undo with: phonevault undo {urid})")
    return 0


def cmd_runs(args, cfg: Config) -> int:
    engine = Engine(cfg)
    for r in engine.journal.runs(args.n):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.started))
        print(f"{r.id}  {when}  {r.kind:8} {r.status:9} {r.files:5} files {r.bytes / 1e6:8.0f} MB"
              + ("  undone" if r.undone else "") + (f"  {r.note}" if r.note else ""))
    if args.show:
        for op in engine.journal.ops(args.show):
            print(f"  {op['seq']:5} {op['op']:10} {op['status']:11} {op['src']} -> {op['dst']}")
    return 0


def cmd_wifi(args, cfg: Config) -> int:
    adb = adb_for(cfg)
    if not adb.available():
        print("Plug the USB cable with USB debugging ON first.")
        return 1
    addr = adb.enable_wifi(args.port)
    cfg.wifi_address = addr
    cfg.save()
    print(f"Wi-Fi link ready: {addr}. You can unplug the cable. Phone and PC must share the same Wi-Fi.")
    print("Note: a phone reboot turns this off. Plug the cable and run `phonevault wifi` again.")
    return 0


def cmd_connect(args, cfg: Config) -> int:
    """Find the phone with no cable: saved address, then Wi-Fi, then Tailscale. Saves what works."""
    adb = adb_for(cfg)
    usb = [d for d in adb.devices() if d["state"] == "device" and not d["wifi"]]
    if usb:
        print(f"Phone is on the cable ({usb[0].get('model', usb[0]['serial'])}). Pinning the network link too.")
        try:
            addr = adb.enable_wifi(args.port)
            cfg.wifi_address = addr
            cfg.save()
            print(f"Wi-Fi link ready: {addr}")
        except Exception as e:  # noqa: BLE001
            print(f"Wi-Fi link not set: {e}")
        ts = adb.tailscale_android_ips()
        if ts:
            print(f"Tailscale sees the phone at {ts[0]}: from anywhere use {ts[0]}:{args.port}")
        return 0
    print("Looking for the phone on the network...")
    addr = adb.discover(cfg.wifi_address, args.port)
    if not addr:
        print("Not found. Phone and PC on the same Wi-Fi? Tailscale ON on both? After a reboot, plug the cable once and run: phonevault wifi")
        return 1
    cfg.wifi_address = addr
    cfg.save()
    print(f"Connected without a cable: {addr} (saved). You can run: phonevault backup")
    return 0


def cmd_restore_all(args, cfg: Config) -> int:
    from .modules import restore_all
    engine = Engine(cfg)
    adb = _adb(cfg)
    print(f"Setting up {adb.describe()} from the vault. When the phone shows 'Restore my data', tap it.")
    rep = with_bar(engine, lambda: restore_all.restore_everything(engine, adb, set(args.skip or [])))
    for k, v in rep["steps"].items():
        print(f"  {k:12} {v}")
    return 0


def cmd_restore(args, cfg: Config) -> int:
    from .modules import apps, pim, settings
    adb = _adb(cfg)
    if args.what == "apps":
        res = apps.restore_apks(adb, cfg.root / cfg.apps_subfolder, args.only)
        print(f"Installed {len(res['installed'])} apps. Failed: {res['failed'] or 'none'}")
    elif args.what == "data":
        if args.file:
            print(f"Restoring {args.file}. Tap 'Restore my data' on the phone.")
            apps.adb_restore(adb, Path(args.file))
        else:
            print("Restoring each app's data. Tap 'Restore my data' on the phone once per app.")
            res = apps.restore_app_data(adb, cfg.root / cfg.apps_subfolder, args.only)
            print(f"Restored {len(res['restored'])}, no archive for {len(res['skipped'])}, failed: {res['failed'] or 'none'}")
    elif args.what == "settings":
        base = cfg.root / cfg.settings_subfolder
        stamp = args.file or (base / "LATEST.txt").read_text(encoding="utf-8").strip()
        res = settings.apply_settings(adb, base / stamp / "settings.json")
        print(f"Applied {res['applied']} settings, skipped {res['skipped']} (identity/radio keys), failed {len(res['failed'])}")
    elif args.what == "contacts":
        base = cfg.root / cfg.pim_subfolder
        stamp = args.file or (base / "LATEST.txt").read_text(encoding="utf-8").strip()
        pim.restore_contacts(adb, base / stamp / "contacts.vcf")
        print("Contacts file opened on the phone. Pick where to save them.")
    elif args.what == "calls":
        base = cfg.root / cfg.pim_subfolder
        stamp = args.file or (base / "LATEST.txt").read_text(encoding="utf-8").strip()
        res = pim.restore_calls(adb, base / stamp / "calls.json")
        print(f"Call log: {res['inserted']} inserted, {res['failed']} failed" + (f" ({res['error']})" if res["error"] else ""))
    elif args.what == "permissions":
        res = apps.restore_permissions(adb, cfg.root / cfg.apps_subfolder, args.only)
        print(f"Permissions: {res['granted']} granted, {res['failed']} failed")
    return 0


def cmd_serve(args, cfg: Config) -> int:
    from .server.app import serve
    host = "0.0.0.0" if args.remote else "127.0.0.1"
    serve(cfg, host=host, port=args.port or cfg.dashboard_port, open_browser=not args.no_open, watch=args.watch)
    return 0


def cmd_demo(args, cfg=None) -> int:
    """The real dashboard, a made-up phone, one temp folder."""
    from . import demo
    from .server.app import serve
    print("Building a sample phone ...", flush=True)
    cfg, _ = demo.make(Path(args.dir) if args.dir else None, delay=args.slow)
    print(f"Demo files: {Path(cfg.pc_root).parent}\nNothing outside that folder is touched. Press 'Sync everything' in the page.")
    serve(cfg, host="127.0.0.1", port=args.port or demo.free_port(), open_browser=not args.no_open)
    return 0


def cmd_dedupe(args, cfg: Config) -> int:
    from .modules import dedupe
    engine = Engine(cfg)
    inside = [Path(x) for x in args.inside] if args.inside else None
    res = with_bar(engine, lambda: dedupe.sweep(engine, inside, dry_run=args.dry_run))
    print(f"{res['duplicates']} duplicates, {res['bytes'] / 1e6:.0f} MB" + (" (dry run, nothing moved)" if args.dry_run else f"  -> trash. Undo: phonevault undo {res['run_id']}" if res["run_id"] else ""))
    return 0


def cmd_health(args, cfg: Config) -> int:
    from .core.health import check
    rows = check(cfg)
    worst = 0
    for r in rows:
        mark = {"ok": "OK  ", "warn": "WARN", "missing": "MISS"}[r["status"]]
        print(f"{mark}  {r['name']:34} {r['detail']}" + (f"   -> {r['fix']}" if r["status"] != "ok" and r.get("fix") else ""))
        worst = max(worst, {"ok": 0, "warn": 1, "missing": 2}[r["status"]])
    return worst


def cmd_config(args, cfg: Config) -> int:
    if args.set:
        from .core import lock
        from .core.config import app_dir
        free, _ = lock.acquire(app_dir() / "engine.lock")
        if not free:
            print("The dashboard or another PhoneVault command is running. Close it first, then change settings.")
            return 1
        lock.release(app_dir() / "engine.lock")
        for kv in args.set:
            k, v = kv.split("=", 1)
            if not hasattr(cfg, k):
                print(f"unknown key {k}")
                return 1
            cur = getattr(cfg, k)
            if k not in Config.__dataclass_fields__ or k == "api_token":
                print(f"unknown key {k}")
                return 1
            if isinstance(cur, bool):
                new = v.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(cur, list):
                new = [x.strip() for x in v.split(",") if x.strip()]
            elif isinstance(cur, dict):
                new = json.loads(v)
            else:
                new = type(cur)(v)
            setattr(cfg, k, new)
        cfg.save()
    print(json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False))
    print(f"(file: {Config.path()})")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="phonevault", description="Back up an Android phone to your PC. No root. No phone here? phonevault demo")
    p.add_argument("--version", action="version", version=f"phonevault {__version__}")
    p.add_argument("--link", choices=["auto", "adb", "mtp"], default="auto", help="how to reach the phone")
    sp = p.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("status", help="what is connected, last runs"); s.add_argument("--mtp", action="store_true"); s.set_defaults(fn=cmd_status)
    s = sp.add_parser("scan", help="list new files per album, copy nothing")
    s.add_argument("what", nargs="?", choices=["gallery", "files", "extdata"], default="gallery")
    s.add_argument("--roots", nargs="*"); s.add_argument("--json"); s.set_defaults(fn=cmd_scan)
    s = sp.add_parser("sync", help="copy new photos/videos/files to the PC")
    s.add_argument("what", nargs="?", choices=["gallery", "files", "extdata", "all"], default="gallery")
    s.add_argument("--dry-run", action="store_true"); s.add_argument("--roots", nargs="*")
    s.set_defaults(fn=cmd_sync)
    s = sp.add_parser("apps", help="save every app's APK (+ Android data backup with --data)")
    s.add_argument("--only", nargs="*"); s.add_argument("--data", action="store_true", help="also save each app's data (one tap per app)")
    s.add_argument("--all-in-one", action="store_true", help="also one big adb backup archive of everything"); s.set_defaults(fn=cmd_apps)
    s = sp.add_parser("settings", help="save the phone settings + restore script"); s.set_defaults(fn=cmd_settings)
    s = sp.add_parser("pim", help="save contacts, SMS, call log"); s.set_defaults(fn=cmd_pim)
    s = sp.add_parser("backup", help="everything: sync all + apps + settings + pim")
    s.add_argument("--data", action="store_true"); s.add_argument("--dry-run", action="store_true"); s.add_argument("--roots", nargs="*")
    s.set_defaults(fn=cmd_backup)
    s = sp.add_parser("autorun", help="wait for the phone (cable + Allow), then run the full backup by itself")
    s.add_argument("--every", type=int, default=10, help="seconds between checks"); s.add_argument("--data", action="store_true")
    s.add_argument("--wifi", action="store_true", help="also switch adb to Wi-Fi once connected"); s.set_defaults(fn=cmd_autorun)
    s = sp.add_parser("undo", help="reverse a run (files go to the trash folder)"); s.add_argument("run_id", nargs="?", default="last"); s.set_defaults(fn=cmd_undo)
    s = sp.add_parser("runs", help="run history"); s.add_argument("-n", type=int, default=20); s.add_argument("--show"); s.set_defaults(fn=cmd_runs)
    s = sp.add_parser("wifi", help="switch adb to Wi-Fi so the cable can go"); s.add_argument("--port", type=int, default=5555); s.set_defaults(fn=cmd_wifi)
    s = sp.add_parser("connect", help="find the phone with no cable (Wi-Fi, then Tailscale) and save the link")
    s.add_argument("--port", type=int, default=5555); s.set_defaults(fn=cmd_connect)
    s = sp.add_parser("restore-all", help="new phone: apps, data, settings, permissions, contacts in one guided run")
    s.add_argument("--skip", nargs="*", choices=["apps", "appdata", "settings", "permissions", "contacts"]); s.set_defaults(fn=cmd_restore_all)
    s = sp.add_parser("restore", help="put apps / data / settings / contacts on a phone")
    s.add_argument("what", choices=["apps", "data", "settings", "contacts", "calls", "permissions"]); s.add_argument("--file"); s.add_argument("--only", nargs="*"); s.set_defaults(fn=cmd_restore)
    s = sp.add_parser("serve", help="open the dashboard"); s.add_argument("--remote", action="store_true", help="also answer other devices on your network (token needed, plain http)")
    s.add_argument("--watch", action="store_true", help="look for the phone every few minutes and back it up by itself")
    s.add_argument("--port", type=int); s.add_argument("--no-open", action="store_true"); s.set_defaults(fn=cmd_serve)
    s = sp.add_parser("demo", help="try the dashboard with a sample phone (no phone needed)")
    s.add_argument("--port", type=int); s.add_argument("--no-open", action="store_true"); s.add_argument("--dir", help="folder to build the demo in (default: a new temp folder)")
    s.add_argument("--slow", type=float, default=0.12, help="seconds per file"); s.set_defaults(fn=cmd_demo)
    s = sp.add_parser("health", help="is every part of the backup fresh? one line per part"); s.set_defaults(fn=cmd_health)
    s = sp.add_parser("dedupe", help="move files in _New from phone that already exist elsewhere in the vault to the trash")
    s.add_argument("--inside", nargs="*", help="folders to sweep (default: _New from phone)"); s.add_argument("--dry-run", action="store_true"); s.set_defaults(fn=cmd_dedupe)
    s = sp.add_parser("config", help="show or set config"); s.add_argument("--set", nargs="*", metavar="key=value"); s.set_defaults(fn=cmd_config)

    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):     # album names in any language must not crash an old Windows console
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    try:
        return args.fn(args, None if args.cmd == "demo" else Config.load())
    except KeyboardInterrupt:
        print("\nStopped. Nothing half-written stays: unfinished files are discarded, finished ones are in the journal.")
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
