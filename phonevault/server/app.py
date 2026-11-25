"""The local dashboard. One Engine, one worker thread at a time, one SSE stream per browser tab.

It listens on 127.0.0.1 only, unless you start it with --remote. Every request needs the token from
config.json, from this PC too: another program, another user or a web page cannot use it just because
it can reach the port. Nothing here talks to the internet; the page is one self-contained HTML file.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import queue
import re
import shutil
import socket
import tempfile
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Iterator

from fastapi import Body, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse

from ..core import thumbs
from ..core.config import Config, MEDIA_EXT, SKIP_NAMES
from ..core.engine import Engine
from ..core.names import inside, pc_name
from ..transports import RemoteFile, pick_transport, probe

STATIC = Path(__file__).parent / "static"
JOB_KINDS = ("scan", "sync", "apps", "appdata", "extdata", "settings", "pim", "files", "all", "dedupe", "restore-all")
RUNNING_PHASES = {"indexing", "scanning", "planning", "copying", "undoing"}

app = FastAPI(title="PhoneVault", docs_url=None, redoc_url=None)
engine: Engine | None = None          # set by serve()
cfg: Config | None = None
last_plan: dict[str, Any] = {}        # summary of the last scan, served by GET /api/plan
_probe = {"t": 0.0, "v": {}, "lock": threading.Lock()}


# ---- token gate -------------------------------------------------------------
TOKEN_HEADER = "x-phonevault-token"
TOKEN_COOKIE = "phonevault_t"         # serve() adds the port: two dashboards on one PC keep their own cookie
LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
FORWARDED = {b"x-forwarded-for", b"forwarded", b"x-real-ip", b"tailscale-user-login", b"cf-connecting-ip"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
LOCKED_PAGE = (b"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'><title>PhoneVault</title>"
               b"<body style='font:16px system-ui;padding:24px;max-width:34em'><h1>Locked</h1><p>Open the full link that "
               b"<code>phonevault serve</code> printed. It ends with <code>?t=</code> and a token.</p>")


def _page_csp() -> str:
    """Only the script that ships in index.html may run."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = html[html.index("<script>") + 8:html.rindex("</script>")]
    digest = base64.b64encode(hashlib.sha256(js.encode("utf-8")).digest()).decode()
    return (f"default-src 'self'; script-src 'sha256-{digest}'; style-src 'unsafe-inline'; img-src 'self' data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'")


def _header(scope, name: bytes) -> str:
    for k, v in scope.get("headers") or []:
        if k == name:
            return v.decode("latin-1").strip()
    return ""


def _request_token(scope) -> str:
    """The token a request carries: the header, else the cookie."""
    from http.cookies import SimpleCookie
    got = _header(scope, TOKEN_HEADER.encode())
    if got:
        return got
    cookie = _header(scope, b"cookie")
    if cookie:
        try:
            c = SimpleCookie()
            c.load(cookie)
            if TOKEN_COOKIE in c:
                return c[TOKEN_COOKIE].value.strip()
        except Exception:  # noqa: BLE001
            pass
    return ""


def _is_pc(scope) -> bool:
    """The request comes straight from this PC: a loopback client, and not through a proxy or tunnel."""
    client = (scope.get("client") or ("", 0))[0] or ""
    return client in LOOPBACK and not any(k in FORWARDED for k, _ in scope.get("headers") or [])


def _cross_site(scope) -> bool:
    """A browser says this call was made by some other web page."""
    origin = _header(scope, b"origin")
    if origin:
        host = _header(scope, b"host")
        return origin not in (f"http://{host}", f"https://{host}")
    return _header(scope, b"sec-fetch-site") in ("cross-site", "same-site")


class TokenGate:
    """Every request must carry cfg.api_token. No token set = nothing is served.
    GET /?t=<token> swaps the token for a cookie and drops it from the address bar."""

    def __init__(self, app):
        self.inner = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "lifespan":
            return await self.inner(scope, receive, send)
        if scope.get("type") != "http":
            return await send({"type": "websocket.close", "code": 4401})
        want = getattr(cfg, "api_token", "") if cfg is not None else ""
        path = scope.get("path") or "/"

        async def secure_send(msg):
            if msg["type"] == "http.response.start":
                have = {k.lower() for k, _ in msg.get("headers") or []}
                extra = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
                         (b"x-frame-options", b"DENY"), (b"cross-origin-resource-policy", b"same-origin"),
                         (b"cross-origin-opener-policy", b"same-origin"), (b"cache-control", b"no-store")]
                msg = {**msg, "headers": list(msg.get("headers") or []) + [h for h in extra if h[0] not in have]}
            await send(msg)

        async def answer(status: int, body: bytes, ctype: bytes, headers=()):
            await secure_send({"type": "http.response.start", "status": status, "headers": [
                (b"content-type", ctype), (b"content-length", str(len(body)).encode()), *headers]})
            await secure_send({"type": "http.response.body", "body": body})

        def ok(got: str) -> bool:
            return bool(want) and bool(got) and hmac.compare_digest(got.encode("utf-8", "replace"), want.encode())

        if path == "/" and scope["method"] == "GET":
            from urllib.parse import parse_qs
            q = parse_qs((scope.get("query_string") or b"").decode("latin-1")).get("t")
            if q and ok(q[0].strip()):
                cookie = f"{TOKEN_COOKIE}={want}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax"
                return await answer(303, b"", b"text/plain", [(b"location", b"/"), (b"set-cookie", cookie.encode())])
        if not ok(_request_token(scope)):
            if path == "/":
                return await answer(401, LOCKED_PAGE, b"text/html; charset=utf-8")
            return await answer(401, b'{"detail": "token required"}', b"application/json")
        if path == "/api/upload" and scope["method"] == "POST":
            # the form is spooled to the temp folder before the route runs: check its size first
            n = _header(scope, b"content-length")
            if not n.isdigit():
                return await answer(411, b'{"detail": "Content-Length is required"}', b"application/json")
            if shutil.disk_usage(tempfile.gettempdir()).free - int(n) < MIN_FREE:
                return await answer(507, b'{"detail": "The PC disk is almost full."}', b"application/json")
        if scope["method"] not in SAFE_METHODS and _cross_site(scope):
            return await answer(403, b'{"detail": "refused: this call came from another web page"}', b"application/json")
        return await self.inner(scope, receive, secure_send)


def _pc_only(request: Request, what: str) -> None:
    if not _is_pc(request.scope):
        raise HTTPException(403, f"{what} works on the PC itself, not from another device.")


app.add_middleware(TokenGate)


def _link() -> dict:
    """probe() can block ~10 s when adb hunts for a Wi-Fi phone; answer from an 8 s cache instead."""
    assert cfg is not None
    with _probe["lock"]:
        if engine is not None and engine.busy.locked() and _probe["v"]:
            return _probe["v"]          # never open a second MTP client while a job uses the phone
        if time.time() - _probe["t"] > 8:
            info = probe(cfg)
            if not info.get("adb") and cfg.phone_name_hint:     # probe() only asks adb; MTP answers in ~0.4 s
                try:
                    from ..transports.mtp import MtpTransport
                    mtp = MtpTransport(cfg.phone_name_hint)
                    info["mtp"] = mtp.available()
                    info["mtp_desc"] = mtp.describe() if info["mtp"] else ""
                except Exception as e:  # noqa: BLE001
                    info["mtp_error"] = str(e)
            _probe["v"] = info
            _probe["t"] = time.time()
        return _probe["v"]


# ---- worker -----------------------------------------------------------------
def _start_worker(fn, *args) -> None:
    """Run fn in a thread while holding engine.busy. 409 if a job is already running."""
    assert engine is not None
    if not engine.busy.acquire(blocking=False):
        raise HTTPException(409, "A job is already running. Wait for it or press Stop.")
    ev = engine.events

    def run() -> None:
        try:
            ev.emit("job", running=True)
            fn(*args)
        except Exception as e:  # noqa: BLE001
            ev.set(phase="failed", finished=time.time(), current="", message=str(e))
            ev.log_line(str(e), "error")
        finally:
            engine.busy.release()
            ev.emit("job", running=False)

    threading.Thread(target=run, daemon=True, name="phonevault-job").start()


def _need_adb(tr):
    if not getattr(tr, "has_shell", False):
        raise RuntimeError("Apps, settings and contacts need adb (USB debugging). MTP only carries files.")
    return tr


def _run_job(kind: str, dry_run: bool, link: str) -> None:
    """The body of every job. Runs inside the worker thread."""
    global last_plan
    from ..modules import apps, media, pim, settings
    assert engine is not None and cfg is not None
    ev = engine.events
    ev.reset(kind)
    if kind == "dedupe":                       # PC only, no phone needed
        from ..modules import dedupe
        res = dedupe.sweep(engine, dry_run=dry_run)
        ev.log_line(f"Dedupe: {res['duplicates']} duplicates, {res['bytes'] / 1e6:.0f} MB")
        return
    if dry_run and kind in ("apps", "appdata", "settings", "pim", "restore-all"):
        raise RuntimeError("This one has no dry run. Switch Dry run off first.")
    ev.set(phase="scanning", message=f"Looking for the phone ({link})")
    try:
        tr = pick_transport(cfg, link)
    except Exception as e:  # noqa: BLE001
        ev.set(phase="failed", finished=time.time(), message=str(e))
        ev.log_line(str(e), "error")
        return
    ev.set(transport=tr.describe())
    ev.log_line(f"Link: {tr.describe()}")
    if getattr(tr, "demo", False) and kind in ("appdata", "restore-all"):
        raise RuntimeError("Not in the demo: this one needs a real phone.")

    if kind == "scan":
        plan = media.plan_gallery(engine, tr)
        last_plan = plan.summary()
        ev.set(phase="done", finished=time.time())
        for a in last_plan["albums"]:
            ev.album_bump(a["album"], "planned", a["copy"] + a["check"])
        ev.emit("state", albums=ev.snapshot()["albums"])
        ev.log_line(f"Scan: {last_plan['copy']} new files, {last_plan['check']} to compare, {last_plan['bytes'] / 1e6:.0f} MB, {len(last_plan['albums'])} albums")
        return
    if kind in ("sync", "files", "extdata"):
        media.sync(engine, tr, "gallery" if kind == "sync" else kind, dry_run=dry_run)
        return
    if kind == "apps":
        apps.backup_apks(engine, _need_adb(tr), with_data=False)
        return
    if kind == "restore-all":
        from ..modules import restore_all
        restore_all.restore_everything(engine, _need_adb(tr))
        return
    if kind == "appdata":
        out = apps.adb_backup(engine, _need_adb(tr), include_apk=False)
        ev.log_line(f"App data archive saved: {out}")
        return
    if kind == "settings":
        settings.backup_settings(engine, _need_adb(tr))
        return
    if kind == "pim":
        pim.backup_pim(engine, _need_adb(tr))
        return

    # kind == "all": every step, one failure never stops the next one
    steps = [
        ("gallery", lambda: media.sync(engine, tr, "gallery", dry_run=dry_run)),
        ("files", lambda: media.sync(engine, tr, "files", dry_run=dry_run)),
        ("app data folders", lambda: media.sync(engine, tr, "extdata", dry_run=dry_run)),
    ] + ([] if dry_run else [                    # these three have no dry run: a dry run skips them
        ("apps", lambda: apps.backup_apks(engine, _need_adb(tr), with_data=False)),
        ("settings", lambda: settings.backup_settings(engine, _need_adb(tr))),
        ("contacts+sms", lambda: pim.backup_pim(engine, _need_adb(tr))),
    ])
    results: list[str] = []
    albums: dict[str, dict] = {}                # every step starts with a clean table: keep the sum for the end
    sums = dict.fromkeys(("total_files", "done_files", "dups", "failed", "total_bytes", "done_bytes"), 0)
    for name, step in steps:
        if ev.state.get("phase") == "cancelled":
            ev.log_line("Stopped before " + name, "warn")
            break
        ev.log_line(f"Step: {name}")
        try:
            step()
            results.append(f"{name}: {ev.state.get('message', 'ok')}")
            if name in ("gallery", "files", "app data folders"):
                snap = ev.snapshot()
                for k in sums:
                    sums[k] += snap.get(k) or 0
                for k, v in snap["albums"].items():
                    if any(v.values()):
                        t = albums.setdefault(k, dict.fromkeys(v, 0))
                        for x, y in v.items():
                            t[x] = t.get(x, 0) + y
        except Exception as e:  # noqa: BLE001
            results.append(f"{name}: failed")
            ev.log_line(f"{name}: {e}", "error")
    ev.set(phase="done", finished=time.time(), current="", kind="all", message=" | ".join(results) or "Nothing ran",
           albums=albums, **sums)


# ---- routes -----------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"), headers={"Content-Security-Policy": _page_csp()})


@app.get("/api/state")
def api_state() -> dict:
    assert engine is not None and cfg is not None
    s = engine.events.snapshot()
    s["busy"] = engine.busy.locked()
    s["link"] = _link()
    s["config"] = cfg.to_dict()
    s["runs"] = [r.to_dict() for r in engine.journal.runs(30)]
    s["trash_dir"] = str(cfg.trash)
    s["demo"] = bool(s["link"].get("demo"))
    return s


@app.get("/events")
def events_stream() -> StreamingResponse:
    assert engine is not None
    ev = engine.events
    q = ev.subscribe()

    def gen() -> Iterator[str]:
        try:
            first = ev.snapshot()
            first["type"] = "state"
            first["busy"] = engine.busy.locked()
            yield "data: " + json.dumps(first, ensure_ascii=False, default=str) + "\n\n"
            while True:
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield "data: " + json.dumps(item, ensure_ascii=False, default=str) + "\n\n"
        finally:
            ev.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/job")
def api_job(request: Request, body: dict = Body(default={})) -> dict:
    kind = str(body.get("kind", "scan"))
    link = str(body.get("link", "auto"))
    if kind not in JOB_KINDS:
        raise HTTPException(400, f"kind must be one of {', '.join(JOB_KINDS)}")
    if link not in ("auto", "adb", "mtp"):
        raise HTTPException(400, "link must be auto, adb or mtp")
    if kind == "restore-all":
        _pc_only(request, "Setting up a phone")
    _start_worker(_run_job, kind, bool(body.get("dry_run", False)), link)
    return {"ok": True, "kind": kind, "dry_run": bool(body.get("dry_run", False)), "link": link}


@app.get("/api/plan")
def api_plan() -> dict:
    return last_plan


@app.post("/api/cancel")
def api_cancel() -> dict:
    assert engine is not None
    engine.cancel()
    engine.pause(False)
    engine.events.log_line("Stop requested", "warn")
    return {"ok": True}


@app.post("/api/pause")
def api_pause(body: dict = Body(default={})) -> dict:
    assert engine is not None
    on = bool(body.get("on", True))
    engine.pause(on)
    engine.events.set(paused=on)
    engine.events.log_line("Paused" if on else "Resumed", "warn")
    return {"ok": True, "paused": on}


def _undo_job(run_id: str) -> str:
    """engine.undo, then re-point the album map and the index for every folder/file the undo moved back."""
    assert engine is not None and cfg is not None
    original = engine.journal.ops(run_id)
    urid = engine.undo(run_id)
    for o in engine.journal.ops(urid):
        if o["op"] == "move" and o["status"] == "ok" and o["src"] and o["dst"]:
            _after_move(Path(o["src"]), Path(o["dst"]))
    # a merged album (its files moved one by one, its empty folder removed) is back: point its map entries at it again
    for o in original:
        if o["op"] == "rmdir" and o["status"] == "ok" and o["src"] and o["dst"] and o["album"] and Path(o["dst"]).is_dir():
            _repoint_album(o["album"], Path(o["src"]), Path(o["dst"]))
    return urid


def _repoint_album(album: str, from_folder: Path, to_folder: Path) -> None:
    """Map entries of this phone album (key = album name or a phone path ending in it) that point at from_folder -> to_folder."""
    assert engine is not None
    fn = os.path.normcase(str(from_folder))
    changed = False
    for k, v in list(engine.mapper.saved.items()):
        leaf = str(k).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        if os.path.normcase(str(v)) == fn and (k == album or leaf == album):
            engine.mapper.saved[k] = str(to_folder)
            changed = True
    if changed:
        engine.mapper.save()
    engine.mapper.invalidate()


@app.post("/api/undo/{run_id}")
def api_undo(run_id: str) -> dict:
    assert engine is not None
    r = engine.journal.run(run_id)
    if not r:
        raise HTTPException(404, f"No run {run_id}")
    if r.undone:
        raise HTTPException(400, f"Run {run_id} was already undone")
    if r.kind == "restore":
        raise HTTPException(400, "That run changed the phone, not the PC. It cannot be undone from here.")
    _start_worker(_undo_job, run_id)
    return {"ok": True, "run_id": run_id}


@app.get("/api/runs/{run_id}")
def api_run_ops(run_id: str) -> list[dict]:
    assert engine is not None
    return engine.journal.ops(run_id)


@app.get("/api/albums")
def api_albums() -> dict:
    assert engine is not None
    return {"saved": dict(engine.mapper.saved), "folders": [p for _, p in engine.mapper.folders][:4000]}


@app.put("/api/albums")
def api_albums_put(body: dict = Body(default={})) -> dict:
    assert engine is not None and cfg is not None
    key = str(body.get("key", "")).strip()
    if not key:
        raise HTTPException(400, "key is required")
    folder = str(body.get("pc_folder", "")).strip()
    if folder and (not Path(folder).is_absolute() or not inside(folder, cfg.root) or inside(folder, cfg.state_dir)):
        raise HTTPException(400, f"Pick a folder inside {cfg.pc_root}.")
    engine.mapper.set(key, folder)
    return {"ok": True, "saved": dict(engine.mapper.saved)}


HTTP_CONFIG = ("pc_root", "wifi_address", "dashboard_port")     # everything else is set with `phonevault config`


@app.post("/api/config")
def api_config(request: Request, body: dict = Body(default={})) -> dict:
    """Three settings, from this PC only. adb_path and the like stay out: they name programs to start."""
    assert engine is not None and cfg is not None
    _pc_only(request, "Changing settings")
    new: dict[str, Any] = {}
    for k, v in body.items():
        if k not in HTTP_CONFIG:
            raise HTTPException(400, f"{k} cannot be changed here. Use: phonevault config --set")
        v = str(v).strip()
        if k == "pc_root" and (not Path(v).is_absolute() or any(ord(c) < 32 for c in v)):
            raise HTTPException(400, "PC folder: give the full path")
        if k == "wifi_address" and v and not re.fullmatch(r"[A-Za-z0-9.\-]{1,253}:\d{1,5}", v):
            raise HTTPException(400, "Wi-Fi address looks like 192.168.1.20:5555")
        if k == "dashboard_port":
            if not v.isdigit() or not 1024 <= int(v) <= 65535:
                raise HTTPException(400, "Port: a number from 1024 to 65535")
            v = int(v)
        new[k] = v
    if not engine.busy.acquire(blocking=False):
        raise HTTPException(409, "A job is running. Wait for it or press Stop.")
    try:
        for k, v in new.items():
            setattr(cfg, k, v)
        cfg.save()
        engine.follow_root()                  # album map, index and trash go to the new folder
    finally:
        engine.busy.release()
    return cfg.to_dict()


@app.get("/api/trash")
def api_trash() -> dict:
    assert cfg is not None
    root = cfg.trash
    out: list[dict] = []
    if root.exists():
        for dirpath, _, names in os.walk(root):
            for n in names:
                p = Path(dirpath) / n
                try:
                    st = p.stat()
                except OSError:
                    continue
                out.append({"path": str(p.relative_to(root)), "size": st.st_size, "mtime": st.st_mtime})
                if len(out) >= 500:
                    break
            if len(out) >= 500:
                break
    return {"dir": str(root), "files": out, "truncated": len(out) >= 500}


def _merge_upload_burst(rid: str, window_s: int = 300) -> str:
    """Phone uploads arrive one file per request. Fold this run into the previous 'upload' run when that one
    finished less than window_s ago, so history shows one run per burst (and undo reverts the whole burst)."""
    assert engine is not None
    con = engine.journal.con
    prev = con.execute("SELECT id, finished FROM runs WHERE kind='upload' AND id<>? AND undone=0 "
                       "ORDER BY started DESC LIMIT 1", (rid,)).fetchone()
    if not prev or not prev[1] or time.time() - prev[1] > window_s:
        return rid
    pid = prev[0]
    maxseq = con.execute("SELECT COALESCE(MAX(seq),0) FROM ops WHERE run_id=?", (pid,)).fetchone()[0]
    con.execute("UPDATE ops SET run_id=?, seq=seq+? WHERE run_id=?", (pid, maxseq, rid))
    con.execute("DELETE FROM runs WHERE id=?", (rid,))
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM ops WHERE run_id=? AND status='ok' AND op='copy'", (pid,)).fetchone()[0]
    engine.journal.finish_run(pid, "done", note=f"phone upload: {n} files")
    return pid


MIN_FREE = 2 * 1024 ** 3


@app.post("/api/upload")
def api_upload(file: UploadFile, path: str = Form(...), mtime: str = Form("")) -> JSONResponse:
    """The phone pushes one file. Same dedupe, mapping and journal as a sync.

    200 = copied, 208 = already on the PC (duplicate), 409 = a job is running, 400 = bad path, 507 = disk full.
    """
    assert engine is not None and cfg is not None
    parts = [s for s in path.replace("\\", "/").split("/") if s]
    if not parts or any(s.strip(" .") == "" for s in parts):
        raise HTTPException(400, "bad path")
    rel = "/".join(pc_name(s) for s in parts)        # no drives, no streams, no reserved names
    ts = None
    if mtime.strip():
        try:
            ts = float(mtime)
        except ValueError:
            raise HTTPException(400, "bad mtime")
        if not 0 <= ts <= 4102444800:                # also refuses nan and inf; 2100-01-01 is far enough
            raise HTTPException(400, "bad mtime")
    if any(len(s) > 200 for s in parts) or len(rel) > 1000:
        raise HTTPException(400, "path too long")
    top = rel.split("/", 1)[0]
    if not engine.busy.acquire(blocking=False):
        raise HTTPException(409, "A job is running; try again in a minute.")
    tmp_root = None
    try:
        engine.ready_to_write()
        tmp_root = engine.staging / "upload" / f"{int(time.time() * 1000)}"
        local = tmp_root / Path(*rel.split("/"))
        local.parent.mkdir(parents=True, exist_ok=True)
        with local.open("wb") as f:
            while chunk := file.file.read(1024 * 1024):
                if shutil.disk_usage(tmp_root).free < MIN_FREE:
                    raise HTTPException(507, "The PC disk is almost full.")
                f.write(chunk)
        if ts is not None:
            os.utime(local, (ts, ts))
        from ..transports.local import LocalTransport
        tr = LocalTransport(tmp_root)
        st = local.stat()       # not a tree listing: a file sent without a folder would list nothing and look like a duplicate
        files = [RemoteFile(rel, rel.rsplit("/", 1)[-1], st.st_size, st.st_mtime)]
        is_media = Path(rel).suffix.lower() in MEDIA_EXT
        if top in cfg.gallery_roots and is_media:
            plan = engine.planner.plan_gallery(files)
        else:
            plan = engine.planner.plan_files(files)   # documents keep the phone's folder tree under "Phone files"
        plan.kind = "upload"
        for i in plan.items:
            if i.action == "skip-exists":       # the bytes are here anyway: compare them
                i.action = "check"
        if not plan.to_copy:
            return JSONResponse({"status": "ignored", "reason": plan.items[0].reason if plan.items else "not a file PhoneVault keeps"}, status_code=208)
        rid = engine.execute(plan, tr, note=f"upload {rel}")
        if rid:
            rid = _merge_upload_burst(rid)
        ops = engine.journal.ops(rid) if rid else []
        last = ops[-1] if ops else {}
        if last.get("status") == "skipped-dup":
            return JSONResponse({"status": "duplicate", "of": last.get("dst"), "run_id": rid}, status_code=208)
        if last.get("status") == "failed":
            raise HTTPException(500, last.get("note") or "copy failed")
        return JSONResponse({"status": "copied", "dst": last.get("dst"), "run_id": rid}, status_code=200)
    except (OSError, RuntimeError) as e:
        raise HTTPException(409, str(e))
    finally:
        engine.busy.release()
        try:
            for p in sorted(tmp_root.rglob("*"), reverse=True):
                p.unlink(missing_ok=True) if p.is_file() else p.rmdir()
            tmp_root.rmdir()
        except (OSError, AttributeError):
            pass


# ---- review: file "_New from phone" albums into the category folders ----------
LOOSE = "(loose files)"      # pseudo-album for files sitting directly in _New from phone
REVIEW_SAMPLE = 12


def _review_root() -> Path:
    assert cfg is not None
    return cfg.root / cfg.new_albums_subfolder


def _tool_folders() -> set[str]:
    assert cfg is not None
    names = {cfg.new_albums_subfolder, cfg.files_subfolder, cfg.apps_subfolder, cfg.settings_subfolder,
             cfg.pim_subfolder, cfg.extdata_subfolder}
    try:
        if cfg.trash.resolve().parent == cfg.root.resolve():
            names.add(cfg.trash.name)
    except OSError:
        pass
    return {os.path.normcase(n) for n in names}


def _categories() -> list[str]:
    """Top-level folders of pc_root that are yours, not the tool's."""
    assert cfg is not None
    root = cfg.root
    if not root.is_dir():
        return []
    skip = _tool_folders()
    out = []
    for p in root.iterdir():
        try:
            if not p.is_dir():
                continue
        except OSError:
            continue
        n = p.name
        if n.startswith(".") or n in SKIP_NAMES or os.path.normcase(n) in skip:
            continue
        out.append(n)
    return sorted(out, key=str.casefold)


def _safe_name(name: str, what: str = "name") -> str:
    """One folder/file name: no separators, no '..', no reserved characters, not empty."""
    n = str(name or "").strip().strip(".")
    if not n or n in (".", "..") or any(c in n for c in '\\/:*?"<>|') or any(ord(c) < 32 for c in n):
        raise HTTPException(400, f"bad {what}: {name!r}")
    return n


def _rel_path(rel: str) -> Path:
    """A path relative to pc_root -> absolute file. 404 for anything outside the root."""
    assert cfg is not None
    r = str(rel or "").replace("\\", "/").strip()
    parts = [s for s in r.split("/") if s]
    if not r or r.startswith("/") or not parts or any(s in (".", "..") or ":" in s for s in parts):
        raise HTTPException(404, "not found")
    root = cfg.root.resolve()
    try:
        p = (root / Path(*parts)).resolve()
        p.relative_to(root)
    except (ValueError, OSError):
        raise HTTPException(404, "not found")
    if not p.is_file():
        raise HTTPException(404, "not found")
    return p


def _rel(p: Path) -> str:
    assert cfg is not None
    try:
        return p.relative_to(cfg.root).as_posix()
    except ValueError:
        return p.resolve().relative_to(cfg.root.resolve()).as_posix()


def _album_files(folder: Path, loose: bool = False) -> list[dict]:
    """Files of one album, newest first. loose = only the files directly in the folder."""
    out: list[dict] = []
    if loose:
        try:
            it = ((folder, [], [p.name for p in folder.iterdir() if p.is_file()]),) if folder.is_dir() else ()
        except OSError:
            it = ()
    else:
        it = os.walk(folder)
    for dirpath, dirnames, names in it:
        if not loose:
            dirnames[:] = [d for d in dirnames if d not in SKIP_NAMES and not d.startswith(".")]
        for n in names:
            if n in SKIP_NAMES or n.startswith("."):
                continue
            p = Path(dirpath) / n
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"name": n, "rel": _rel(p), "size": st.st_size, "mtime": st.st_mtime,
                        "is_video": thumbs.is_video(p), "_p": p})
    out.sort(key=lambda f: (-f["mtime"], f["name"].casefold()))
    return out


def _album_folder(album: str) -> tuple[Path, bool]:
    """(folder, is_loose) for an album name from the review page."""
    if album == LOOSE:
        return _review_root(), True
    folder = _review_root() / _safe_name(album, "album")
    if not folder.is_dir():
        raise HTTPException(404, f"No album {album!r} in {cfg.new_albums_subfolder}")
    return folder, False


def _strip(files: list[dict]) -> list[dict]:
    return [{k: v for k, v in f.items() if k != "_p"} for f in files]


@app.get("/api/review")
def api_review() -> dict:
    assert cfg is not None
    root = _review_root()
    albums: list[dict] = []
    if root.is_dir():
        for p in sorted(root.iterdir(), key=lambda x: x.name.casefold()):
            if not p.is_dir() or p.name in SKIP_NAMES or p.name.startswith("."):
                continue
            files = _album_files(p)
            albums.append({"name": p.name, "path": str(p), "files": len(files), "bytes": sum(f["size"] for f in files),
                           "newest_mtime": max((f["mtime"] for f in files), default=None),
                           "sample": [f["rel"] for f in files[:REVIEW_SAMPLE]],
                           "sample_files": _strip(files[:REVIEW_SAMPLE])})
        loose = _album_files(root, loose=True)
        if loose:
            albums.append({"name": LOOSE, "path": str(root), "files": len(loose), "bytes": sum(f["size"] for f in loose),
                           "newest_mtime": max(f["mtime"] for f in loose), "loose": True,
                           "sample": [f["rel"] for f in loose[:REVIEW_SAMPLE]],
                           "sample_files": _strip(loose[:REVIEW_SAMPLE])})
    return {"categories": _categories(), "new_folder": cfg.new_albums_subfolder, "root": str(root), "albums": albums}


@app.get("/api/review/files")
def api_review_files(album: str, offset: int = 0, limit: int = 60) -> dict:
    folder, loose = _album_folder(album)
    files = _album_files(folder, loose=loose)
    offset, limit = max(0, int(offset)), max(1, min(500, int(limit)))
    page = files[offset:offset + limit]
    return {"album": album, "total": len(files), "offset": offset, "limit": limit, "files": _strip(page)}


@app.get("/thumb")
def api_thumb(rel: str, size: int = 256) -> Response:
    p = _rel_path(rel)
    size = max(32, min(1024, int(size)))
    return Response(content=thumbs.thumb(p, size), media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"})


@app.get("/file")
def api_file(rel: str) -> FileResponse:
    """Photos and videos open in the page. Anything else (HTML, SVG, ...) is a download and can never run here."""
    p = _rel_path(rel)
    show = p.suffix.lower() in MEDIA_EXT
    return FileResponse(str(p), filename=p.name, content_disposition_type="inline" if show else "attachment",
                        media_type=None if show else "application/octet-stream",
                        headers={"Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; media-src 'self'"})


def _after_move(old: Path, new: Path) -> None:
    """A file or folder went from old to new: fix the album map and the PC index."""
    assert engine is not None
    old_s, new_s = str(old), str(new)
    old_n = os.path.normcase(old_s)
    sep = os.sep
    # 1. album map: every phone album that pointed at the old folder (or inside it) follows it
    changed = False
    for k, v in list(engine.mapper.saved.items()):
        vn = os.path.normcase(str(v))
        if vn == old_n:
            engine.mapper.saved[k] = new_s
            changed = True
        elif vn.startswith(old_n + sep):
            engine.mapper.saved[k] = new_s + str(v)[len(old_s):]
            changed = True
    if changed:
        engine.mapper.save()
    engine.mapper.invalidate()
    # 2. index: carry the rows (and their hashes) over to the new paths, then pick up anything missing
    con = engine.index.con
    n = len(old_s)
    rows = con.execute("SELECT path,name,size,mtime,quick,sha FROM files WHERE path=? OR substr(path,1,?)=?",
                       (old_s, n + 1, old_s + sep)).fetchall()
    for r in rows:
        np_ = new_s + r[0][n:]
        con.execute("DELETE FROM files WHERE path=?", (r[0],))
        if Path(np_).is_file():
            con.execute("INSERT OR REPLACE INTO files(path,name,size,mtime,quick,sha,seen) VALUES(?,?,?,?,?,?,?)",
                        (np_, r[1], r[2], r[3], r[4], r[5], time.time()))
    con.execute("UPDATE phone_seen SET resolved_to=? || substr(resolved_to, ?) WHERE resolved_to=? OR substr(resolved_to,1,?)=?",
                (new_s, n + 1, old_s, n + 1, old_s + sep))
    con.commit()
    try:
        if new.is_dir():
            engine.index.refresh(new)
        elif new.is_file() and not rows:
            engine.index.add_file(new)
    except OSError:
        pass


def _prune_empty(folder: Path, stop_at: Path) -> bool:
    """Remove folder if nothing but empty folders is left in it. True when it is gone."""
    try:
        if folder.resolve() == stop_at.resolve() or not folder.is_dir():
            return False
        for dirpath, dirnames, names in os.walk(folder, topdown=False):
            if names:
                return False
            for d in dirnames:
                try:
                    (Path(dirpath) / d).rmdir()
                except OSError:
                    return False
        folder.rmdir()
        return True
    except OSError:
        return False


def _target_folder(body: dict, album: str, loose: bool) -> tuple[str, str, Path]:
    assert cfg is not None
    category = str(body.get("category", "")).strip()
    new_name = str(body.get("new_name") or "").strip()
    if category != cfg.new_albums_subfolder and category not in _categories():
        raise HTTPException(400, f"Unknown folder {category!r}. Pick one of the category folders.")
    if loose and not new_name:
        raise HTTPException(400, "Loose files need a folder name: fill in 'rename to'.")
    name = _safe_name(new_name or album, "new name")
    return category, name, cfg.root / category / name


def _review_lock():
    assert engine is not None
    if not engine.busy.acquire(blocking=False):
        raise HTTPException(409, "A job is running. Wait for it or press Stop, then file the album.")


@app.post("/api/review/move")
def api_review_move(body: dict = Body(default={})) -> dict:
    """Move one album folder to <pc_root>/<category>/<new_name or album>. One journaled run, undo-able."""
    assert engine is not None and cfg is not None
    album = str(body.get("album", "")).strip()
    src, loose = _album_folder(album)
    category, name, target = _target_folder(body, album, loose)
    merge = bool(body.get("merge", False))
    if not loose and os.path.normcase(str(target)) == os.path.normcase(str(src)):
        raise HTTPException(400, "That is where it already is. Pick a folder or type a new name.")
    _review_lock()
    rid = engine.journal.start_run("review", note=f"{album} → {category}/{name}")
    try:
        moved = 0
        if loose or (merge and target.is_dir()):
            files = _album_files(src, loose=loose)
            for f in files:
                dst = target / f["_p"].relative_to(src)
                landed = engine.journal.move_path(rid, f["_p"], dst, album)
                _after_move(f["_p"], landed)
                moved += 1
            if not loose and _prune_empty(src, _review_root()):
                # src = where the files went, dst = the folder that is gone; undo uses it to re-point the album map
                engine.journal.record(rid, "rmdir", str(target), str(src), album=album)
                _repoint_album(album, src, target)      # the album map follows the files into the merged folder
            dst_final = target
        else:
            dst_final = engine.journal.move_path(rid, src, target, album)
            _after_move(src, dst_final)
            moved = sum(1 for _ in _album_files(dst_final))
        engine.journal.finish_run(rid, "done")
        engine.events.log_line(f"Filed {album} → {_rel(dst_final)} ({moved} files)")
        return {"ok": True, "run_id": rid, "moved": moved, "dst": str(dst_final), "rel": _rel(dst_final),
                "merged": bool(loose or (merge and dst_final == target))}
    except Exception as e:  # noqa: BLE001
        engine.journal.finish_run(rid, "failed", note=str(e))
        raise
    finally:
        engine.busy.release()


@app.post("/api/review/move-files")
def api_review_move_files(body: dict = Body(default={})) -> dict:
    """Move chosen files of one album to <pc_root>/<category>/<new_name or album>/. Files always merge."""
    assert engine is not None and cfg is not None
    album = str(body.get("album", "")).strip()
    src, loose = _album_folder(album)
    rels = body.get("files") or []
    if not isinstance(rels, list) or not rels:
        raise HTTPException(400, "files: pick at least one file")
    category, name, target = _target_folder(body, album, loose)
    picks: list[Path] = []
    src_r = src.resolve()
    for r in rels:
        p = _rel_path(str(r))
        try:
            p.relative_to(src_r)
        except ValueError:
            raise HTTPException(400, f"{r} is not in album {album!r}")
        if loose and p.parent != src_r:
            raise HTTPException(400, f"{r} is not a loose file")
        picks.append(p)
    _review_lock()
    rid = engine.journal.start_run("review", note=f"{len(picks)} files of {album} → {category}/{name}")
    try:
        moved = 0
        for p in picks:
            dst = target / p.relative_to(src_r)
            landed = engine.journal.move_path(rid, p, dst, album)
            _after_move(p, landed)
            moved += 1
        emptied = False
        if not loose and _prune_empty(src, _review_root()):
            emptied = True
            engine.journal.record(rid, "rmdir", str(target), str(src), album=album)
            _repoint_album(album, src, target)
        engine.journal.finish_run(rid, "done")
        engine.events.log_line(f"Filed {moved} files of {album} → {_rel(target)}")
        return {"ok": True, "run_id": rid, "moved": moved, "dst": str(target), "rel": _rel(target), "album_emptied": emptied}
    except Exception as e:  # noqa: BLE001
        engine.journal.finish_run(rid, "failed", note=str(e))
        raise
    finally:
        engine.busy.release()


@app.get("/api/runs/{run_id}/failed")
def api_run_failed(run_id: str) -> list[dict]:
    assert engine is not None
    return [o for o in engine.journal.ops(run_id) if o["status"] == "failed"]


@app.get("/api/health")
def api_health() -> dict:
    from ..core.health import check
    assert cfg is not None
    rows = check(cfg)
    worst = max((0 if r["status"] == "ok" else 1 if r["status"] == "warn" else 2) for r in rows) if rows else 0
    return {"rows": rows, "worst": ["ok", "warn", "missing"][worst]}


@app.exception_handler(Exception)
async def _any_error(_, exc: Exception) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=500)


# ---- entry ------------------------------------------------------------------
def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))         # no packet is sent; only picks the outbound interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


watcher = None
_server: dict = {}


@app.post("/api/shutdown")
def api_shutdown(request: Request) -> dict:
    _pc_only(request, "Stopping the dashboard")
    srv = _server.get("srv")
    if srv is not None:
        srv.should_exit = True
    return {"ok": True}


def _run_job_blocking(kind: str) -> None:
    """Used by the watcher: run a job and wait for it, holding engine.busy like the API does."""
    assert engine is not None
    if not engine.busy.acquire(blocking=False):
        return
    ev = engine.events
    try:
        ev.emit("job", running=True)
        _run_job(kind, False, "auto")
    except Exception as e:  # noqa: BLE001
        ev.set(phase="failed", finished=time.time(), current="", message=str(e))
        ev.log_line(str(e), "error")
    finally:
        engine.busy.release()
        ev.emit("job", running=False)


@app.post("/api/watch/tick")
def api_watch_tick() -> dict:
    """Run one watcher pass now. `phonevault backup` calls this when the dashboard already holds the journal."""
    if watcher is None:
        raise HTTPException(503, "watcher not running")
    threading.Thread(target=watcher.tick, daemon=True).start()
    return {"ok": True}


def port_taken(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def serve(config: Config, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True, watch: bool = False) -> None:
    global engine, cfg, watcher, TOKEN_COOKIE
    import sys
    import uvicorn
    from ..core.config import app_dir, ensure_api_token
    from ..core.watcher import Watcher
    if sys.stdout is None or sys.stderr is None:   # pythonw: no console, print() would crash the server
        logf = open(app_dir() / "dashboard.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = logf
    if port_taken(port):
        raise RuntimeError(f"Port {port} is in use. Try: phonevault serve --port {port + 1}")
    cfg = config
    token = ensure_api_token(cfg)
    TOKEN_COOKIE = f"phonevault_t{port}"
    engine = Engine(cfg)
    if engine.read_only:
        raise RuntimeError("Another PhoneVault command is running. Wait for it to finish, then try again.")
    watcher = Watcher(engine, cfg, _run_job_blocking, interval_min=cfg.watch_interval_min, full_every_hours=cfg.watch_full_every_hours)
    local = f"http://127.0.0.1:{port}/?t={token}"
    print("PhoneVault dashboard")
    print(f"  on this PC : {local}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  remote     : http://{lan_ip()}:{port}/?t={token}")
        print("  Remote is ON. Anyone with that link can see your backup. The link is plain http:")
        print("  use it on a network you trust, or better, inside a VPN such as Tailscale or WireGuard.")
    if watch or cfg.watch_enabled:
        watcher.start()
        print(f"  watcher    : every {watcher.interval // 60} min, full sync every {watcher.full_every // 3600} h when the phone answers")
    print("  Ctrl+C stops it.", flush=True)
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(local)).start()
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning", server_header=False,
                                           proxy_headers=False, timeout_graceful_shutdown=2))
    _server["srv"] = server
    server.run()
