import json
import time
from pathlib import Path
from types import SimpleNamespace

from phonevault.modules import pim


class CalendarAdb:
    serial = "test-phone"
    contacts = True
    events = []

    def describe(self):
        return "test phone"

    def shell(self, command, timeout=120):
        uri = command.split()[3]
        if uri == "content://com.android.calendar/events":
            rows = self.events
        elif uri == "content://com.android.contacts/data" and self.contacts:
            rows = [{"contact_id": "1", "display_name": "Alice"}]
        else:
            rows = []
        columns = command.split("--projection ", 1)[1].split()[0].split(":")
        return "\n".join(
            f"Row: {index} " + ", ".join(f"{key}={row.get(key, 'NULL')}" for key in columns)
            for index, row in enumerate(rows)
        ) if rows else "No result found.\n"


def test_calendar_backup_keeps_last_nonempty_export(engine, monkeypatch):
    monkeypatch.setattr(pim, "time", SimpleNamespace(strftime=lambda _: "20260101-000000", time=time.time))
    adb = CalendarAdb()
    previous = Path(pim.backup_pim(engine, adb)["dir"])
    latest = previous.parent / "LATEST.txt"
    calendar_latest = previous.parent / "LATEST-calendar.txt"

    adb.contacts = False
    description = "é" * 40 + "\nLOCATION:still text\\here"
    adb.events = [
        {"_id": "1", "title": "Review, plan; notes", "description": description,
         "dtstart": "1700000000000", "dtend": "1700003600000", "allDay": "0", "deleted": "0"},
        {"_id": "2", "title": "Day off", "dtstart": "1700006400000",
         "dtend": "1700092800000", "allDay": "1", "deleted": "0"},
    ]
    result = pim.backup_pim(engine, adb)
    assert result["counts"].get("calendar") == 2
    snapshot = Path(result["dir"])
    raw = (snapshot / "calendar.json").read_bytes()
    ics = (snapshot / "calendar.ics").read_bytes()
    assert json.loads(raw)[0]["description"] == description
    assert max(map(len, ics.split(b"\r\n"))) <= 75
    unfolded = ics.decode().replace("\r\n ", "")
    assert unfolded.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n")
    assert unfolded.count("BEGIN:VEVENT\r\n") == 2
    assert "DTSTART:20231114T221320Z\r\nDTEND:20231114T231320Z\r\n" in unfolded
    assert "DTSTART;VALUE=DATE:20231115\r\nDTEND;VALUE=DATE:20231116\r\n" in unfolded
    assert r"SUMMARY:Review\, plan\; notes" in unfolded
    assert r"\nLOCATION:still text\\here" in unfolded and "\r\nLOCATION:" not in unfolded
    assert latest.read_text() == previous.name
    assert calendar_latest.read_text() == snapshot.name

    adb.contacts = True
    adb.events = []
    empty = pim.backup_pim(engine, adb)
    assert empty["counts"]["calendar"] == 0
    assert latest.read_text() == Path(empty["dir"]).name
    assert calendar_latest.read_text() == snapshot.name
    assert (snapshot / "calendar.json").read_bytes() == raw
    assert (snapshot / "calendar.ics").read_bytes() == ics
