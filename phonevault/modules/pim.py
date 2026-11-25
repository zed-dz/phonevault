"""Contacts, SMS and call log over adb's content provider. No root, no app.

Saved as JSON (full rows) + contacts.vcf (import on any phone) + sms.xml in the
"SMS Backup & Restore" shape, which most restore apps read.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from xml.sax.saxutils import escape

from ..core.engine import Engine
from ..transports.adb import AdbTransport

_ROW = re.compile(r"^Row: \d+ (.*)$", re.DOTALL)
_KV = re.compile(r"(?:^|, )(\w+)=")


def parse_rows(out: str, columns: list[str] | None = None) -> list[dict[str, str]]:
    """Parse `content query` output. Values may hold commas; split on ', key=' boundaries.
    columns = the projection, in order: then a message that says "total, x=5" is not cut in two."""
    rows: list[dict[str, str]] = []
    want = re.compile(r"(?:^|, )(" + "|".join(map(re.escape, columns)) + ")=") if columns else _KV
    buf: list[str] = []

    def flush() -> None:
        if not buf:
            return
        text = "\n".join(buf)
        m = _ROW.match(text)
        if not m:
            buf.clear()
            return
        body = m.group(1).rstrip("\n")
        keys = list(want.finditer(body))
        if columns:                     # keep the matches that follow the projection's order
            kept, at = [], -1
            for km in keys:
                i = columns.index(km.group(1))
                if i == at + 1:
                    kept.append(km)
                    at = i
            keys = kept
        row: dict[str, str] = {}
        for i, km in enumerate(keys):
            start = km.end()
            end = keys[i + 1].start() if i + 1 < len(keys) else len(body)
            row[km.group(1)] = body[start:end]
        rows.append(row)
        buf.clear()

    for line in out.split("\n"):
        if line.startswith("Row: "):
            flush()
            buf.append(line.rstrip("\r"))
        elif buf:
            buf.append(line.rstrip("\r"))  # a value with a newline (SMS body)
    flush()
    return rows


def query(adb: AdbTransport, uri: str, projection: str | None = None, sort: str | None = None) -> list[dict[str, str]]:
    cmd = f"content query --uri {uri}"
    if projection:
        cmd += f" --projection {projection}"
    if sort:
        cmd += f" --sort '{sort}'"
    out = adb.shell(cmd + " 2>&1", timeout=600)   # Android prints provider errors on stderr; bring them back
    head = out[:400]
    if "Permission Denial" in head or "SecurityException" in head:
        raise RuntimeError("the phone refuses this read without extra permission (some phones block the call log for adb)")
    if "Error" in head[:200] or "Exception" in head[:200]:
        raise RuntimeError(out.strip().split("\n")[0])
    return parse_rows(out, projection.split(":") if projection else None)


def contacts(adb: AdbTransport) -> list[dict]:
    rows = query(adb, "content://com.android.contacts/data",
                 "contact_id:display_name:mimetype:data1:data2:data3:account_type")
    people: dict[str, dict] = {}
    for r in rows:
        cid = r.get("contact_id", "")
        p = people.setdefault(cid, {"id": cid, "name": r.get("display_name", ""), "phones": [], "emails": [],
                                    "addresses": [], "orgs": [], "notes": [], "account": r.get("account_type", "")})
        mt = r.get("mimetype", "")
        v = r.get("data1", "")
        if not v or v == "NULL":
            continue
        if mt.endswith("/phone_v2"):
            if v not in p["phones"]:
                p["phones"].append(v)
        elif mt.endswith("/email_v2"):
            if v not in p["emails"]:
                p["emails"].append(v)
        elif mt.endswith("/postal-address_v2"):
            p["addresses"].append(v)
        elif mt.endswith("/organization"):
            p["orgs"].append(v)
        elif mt.endswith("/note"):
            p["notes"].append(v)
    return sorted(people.values(), key=lambda p: p["name"].casefold())


def sms(adb: AdbTransport) -> list[dict[str, str]]:
    return query(adb, "content://sms", "_id:thread_id:address:date:date_sent:type:read:body:service_center", "date ASC")


def mms(adb: AdbTransport) -> list[dict[str, str]]:
    """MMS headers (picture messages). Parts (the images/text) come from content://mms/part."""
    rows = query(adb, "content://mms", "_id:thread_id:date:date_sent:msg_box:read:sub:ct_t", "date ASC")
    if not rows:
        return rows
    try:
        parts = query(adb, "content://mms/part", "_id:mid:ct:name:text:_data")
    except Exception:  # noqa: BLE001
        parts = []
    by_mid: dict[str, list] = {}
    for part in parts:
        by_mid.setdefault(part.get("mid", ""), []).append(part)
    for r in rows:
        r["parts"] = by_mid.get(r.get("_id", ""), [])
    return rows


def restore_calls(adb: AdbTransport, calls_json: Path) -> dict:
    """Put a saved call log back with content insert. Works on phones that let adb write the call log."""
    import json as _json
    rows = _json.loads(Path(calls_json).read_text(encoding="utf-8"))
    res = {"inserted": 0, "failed": 0, "error": ""}
    for r in rows:
        binds = []
        for key, typ in (("number", "s"), ("date", "l"), ("duration", "l"), ("type", "i"), ("name", "s")):
            v = r.get(key)
            if v in (None, "", "NULL"):
                continue
            v = str(v).replace("'", "")
            if typ != "s" and not re.fullmatch(r"-?\d+", v):
                continue
            binds.append(f"--bind {key}:{typ}:'{v}'")
        if not binds:
            continue
        try:
            out = adb.shell("content insert --uri content://call_log/calls " + " ".join(binds) + " 2>&1")
            if "Error" in out[:200] or "Exception" in out[:200] or "Permission" in out[:300]:
                res["failed"] += 1
                res["error"] = out.strip().split("\n")[0][:200]
            else:
                res["inserted"] += 1
        except Exception as e:  # noqa: BLE001
            res["failed"] += 1
            res["error"] = str(e)
    return res


def calls(adb: AdbTransport) -> list[dict[str, str]]:
    return query(adb, "content://call_log/calls", "_id:number:date:duration:type:name:countryiso", "date ASC")


def _vcf(v: str) -> str:
    """vCard 3.0 text: no raw line breaks, so one contact cannot write lines of its own."""
    return str(v).replace("\\", "\\\\").replace("\r", "").replace("\n", "\\n")


def to_vcf(people: list[dict]) -> str:
    out = []
    for p in people:
        if not p["name"] and not p["phones"]:
            continue
        out.append("BEGIN:VCARD\nVERSION:3.0")
        out.append(f"FN:{_vcf(p['name'])}")
        out.append(f"N:{_vcf(p['name'])};;;;")
        for ph in p["phones"]:
            out.append(f"TEL;TYPE=CELL:{_vcf(ph)}")
        for em in p["emails"]:
            out.append(f"EMAIL:{_vcf(em)}")
        for ad in p["addresses"]:
            out.append(f"ADR:;;{_vcf(ad)};;;;")
        for o in p["orgs"]:
            out.append(f"ORG:{_vcf(o)}")
        for n in p["notes"]:
            out.append(f"NOTE:{_vcf(n)}")
        out.append("END:VCARD")
    return "\n".join(out) + "\n"


def to_sms_xml(messages: list[dict[str, str]]) -> str:
    """Same shape as the popular 'SMS Backup & Restore' app, so it can import this."""
    lines = ['<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>',
             f'<smses count="{len(messages)}" backup_set="phonevault" backup_date="{int(time.time()*1000)}">']
    for m in messages:
        attrs = {k: escape(m.get(k, "") or "", {'"': "&quot;"}) for k in
                 ("address", "date", "type", "body", "read", "service_center", "date_sent")}
        lines.append(f'  <sms protocol="0" address="{attrs["address"]}" date="{attrs["date"]}" type="{attrs["type"]}" '
                     f'subject="null" body="{attrs["body"]}" toa="null" sc_toa="null" service_center="{attrs["service_center"]}" '
                     f'read="{attrs["read"] or "1"}" status="-1" locked="0" date_sent="{attrs["date_sent"] or "0"}" />')
    lines.append("</smses>")
    return "\n".join(lines) + "\n"


def backup_pim(engine: Engine, adb: AdbTransport) -> dict:
    ev = engine.events
    ev.reset("pim", adb.describe())
    ev.set(phase="copying", total_files=4, message="Reading contacts, SMS, MMS, call log")
    root = engine.cfg.root / engine.cfg.pim_subfolder
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = root / stamp
    engine.ready_to_write()
    engine.safe_dir(out_dir)
    rid = engine.journal.start_run("pim", note=str(out_dir))
    result: dict = {"dir": str(out_dir), "counts": {}, "errors": {}}
    jobs = (("contacts", contacts), ("sms", sms), ("mms", mms), ("calls", calls))
    for n, (name, fn) in enumerate(jobs, 1):
        ev.set(current=name, done_files=n - 1)
        try:
            data = fn(adb)
            p = out_dir / f"{name}.json"
            p.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
            engine.journal.record(rid, "copy", f"content://{name}", str(p), p.stat().st_size)
            result["counts"][name] = len(data)
            if name == "contacts":
                v = out_dir / "contacts.vcf"
                v.write_text(to_vcf(data), encoding="utf-8")
                engine.journal.record(rid, "copy", "contacts.vcf", str(v), v.stat().st_size)
            if name == "sms":
                x = out_dir / "sms.xml"
                x.write_text(to_sms_xml(data), encoding="utf-8")
                engine.journal.record(rid, "copy", "sms.xml", str(x), x.stat().st_size)
            ev.emit("file_done", path=name, status="ok", dst=str(p), album="pim")
        except Exception as e:  # noqa: BLE001
            result["errors"][name] = str(e)
            engine.journal.record(rid, "copy", name, None, status="failed", note=str(e))
            ev.emit("file_failed", path=name, error=str(e), album="pim")
    c = result["counts"]
    if c.get("contacts", 0) > 0 or c.get("sms", 0) > 0:
        (root / "LATEST.txt").write_text(stamp, encoding="utf-8")   # an empty read never replaces a good one (some phones block the call log)
    else:
        engine.events.log_line("Nothing read from the phone: LATEST.txt left pointing at the last good save", "warn")
    engine.journal.finish_run(rid, "done" if sum(c.values()) > 0 else "failed")
    ev.set(phase="done", finished=time.time(), done_files=4, current="", run_id=rid,
           message=f"{c.get('contacts', 0)} contacts, {c.get('sms', 0)} SMS, {c.get('mms', 0)} MMS, {c.get('calls', 0)} calls saved"
                   + (f" (blocked: {', '.join(result['errors'])})" if result["errors"] else ""))
    ev.emit("run_done", run_id=rid, status="done")
    result["run_id"] = rid
    return result


def restore_contacts(adb: AdbTransport, vcf: Path) -> None:
    """Push the VCF and open it with the Contacts app. The phone asks where to save."""
    adb.push(vcf, "Download/phonevault-contacts.vcf")
    adb.shell("am start -a android.intent.action.VIEW -d file:///sdcard/Download/phonevault-contacts.vcf -t text/x-vcard")
