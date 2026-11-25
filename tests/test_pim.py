from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from phonevault.modules.pim import parse_rows, to_sms_xml, to_vcf

TWO_ROWS = (
    "Row: 0 _id=1, address=+1 555 555 1234, body=Hi there, how are you?, type=1, date=1700000000000\n"
    "Row: 1 _id=2, address=12345, body=Code: 9981, valid for 10 min, type=2, date=1700000001000\n"
)


def test_parse_rows_two_rows_with_commas_inside_values():
    rows = parse_rows(TWO_ROWS)
    assert len(rows) == 2
    assert rows[0] == {"_id": "1", "address": "+1 555 555 1234", "body": "Hi there, how are you?",
                       "type": "1", "date": "1700000000000"}
    assert rows[1]["body"] == "Code: 9981, valid for 10 min"
    assert rows[1]["type"] == "2"


def test_parse_rows_handles_crlf_and_null_values():
    rows = parse_rows("Row: 0 _id=7, name=NULL, body=x\r\n")
    assert rows == [{"_id": "7", "name": "NULL", "body": "x"}]


def test_parse_rows_ignores_non_row_lines_and_empty_output():
    assert parse_rows("") == []
    assert parse_rows("No result found.\n") == []
    rows = parse_rows("junk header\nRow: 0 a=1\n")
    assert rows == [{"a": "1"}]


def test_parse_rows_body_containing_a_newline():
    out = ("Row: 0 _id=1, address=+1 555 555, body=first line\nsecond line, type=1\n"
           "Row: 1 _id=2, address=12, body=plain, type=2\n")
    rows = parse_rows(out)
    assert len(rows) == 2
    assert rows[0]["body"] == "first line\nsecond line"
    assert rows[0]["type"] == "1"
    assert rows[1]["body"] == "plain"


PEOPLE = [
    {"id": "1", "name": "Front desk", "phones": ["+1 555555", "+1 555666"], "emails": ["a@example.com"],
     "addresses": ["1 Main St"], "orgs": ["ACME"], "notes": ["note one"], "account": "com.google"},
    {"id": "2", "name": "", "phones": [], "emails": ["ghost@example.com"], "addresses": [], "orgs": [],
     "notes": [], "account": ""},
    {"id": "3", "name": "Only Name", "phones": [], "emails": [], "addresses": [], "orgs": [], "notes": [],
     "account": ""},
]


def test_to_vcf_begin_end_blocks_with_tel_and_email():
    vcf = to_vcf(PEOPLE)
    assert vcf.count("BEGIN:VCARD") == 2  # the nameless, phoneless one is dropped
    assert vcf.count("END:VCARD") == 2
    blocks = vcf.strip().split("END:VCARD")
    desk = blocks[0]
    assert "VERSION:3.0" in desk
    assert "FN:Front desk" in desk and "N:Front desk;;;;" in desk
    assert "TEL;TYPE=CELL:+1 555555" in desk and "TEL;TYPE=CELL:+1 555666" in desk
    assert "EMAIL:a@example.com" in desk
    assert "ADR:;;1 Main St;;;;" in desk and "ORG:ACME" in desk and "NOTE:note one" in desk
    assert "FN:Only Name" in blocks[1]
    assert vcf.endswith("END:VCARD\n")


def test_to_vcf_empty_list():
    assert to_vcf([]) == "\n"


MESSAGES = [
    {"address": "+1 555555", "date": "1700000000000", "type": "1", "body": 'He said "hi" & left <3', "read": "1",
     "service_center": "+1 55500", "date_sent": "1700000000000"},
    {"address": "12345", "date": "1700000001000", "type": "2", "body": "plain", "read": "", "service_center": "",
     "date_sent": ""},
]


def test_to_sms_xml_escapes_quotes_and_ampersands():
    xml = to_sms_xml(MESSAGES)
    assert 'body="He said &quot;hi&quot; &amp; left &lt;3"' in xml
    assert '"hi"' not in xml.split("\n")[2]  # raw quote never lands inside the attribute


def test_to_sms_xml_count_attribute_and_well_formed():
    xml = to_sms_xml(MESSAGES)
    assert 'count="2"' in xml
    root = ET.fromstring(xml)
    assert root.tag == "smses" and root.attrib["count"] == "2"
    smses = root.findall("sms")
    assert len(smses) == 2
    assert smses[0].attrib["body"] == 'He said "hi" & left <3'
    assert smses[0].attrib["address"] == "+1 555555"
    # defaults for blank read / date_sent
    assert smses[1].attrib["read"] == "1" and smses[1].attrib["date_sent"] == "0"


def test_to_sms_xml_empty():
    xml = to_sms_xml([])
    assert 'count="0"' in xml
    assert ET.fromstring(xml).findall("sms") == []



def test_restore_calls_builds_binds_and_counts(tmp_path):
    import json
    from phonevault.modules import pim

    class FakeAdb:
        def __init__(self):
            self.cmds = []
        def shell(self, cmd, timeout=120):
            self.cmds.append(cmd)
            return "" if "+1 555" in cmd else "Error: boom"

    j = tmp_path / "calls.json"
    j.write_text(json.dumps([{"number": "+1 555555", "date": "1700000000000", "duration": "12", "type": "1", "name": "Front desk"},
                             {"number": "0770", "date": "1700000001000", "duration": "0", "type": "3"},
                             {"name": "NULL"}]), encoding="utf-8")
    adb = FakeAdb()
    res = pim.restore_calls(adb, j)
    assert res["inserted"] == 1 and res["failed"] == 1
    assert "--bind number:s:'+1 555555'" in adb.cmds[0] and "--bind date:l:'1700000000000'" in adb.cmds[0]
    assert len(adb.cmds) == 2  # the empty row is skipped


def test_a_message_that_looks_like_a_column_is_not_cut():
    out = "Row: 0 _id=1, address=+1 555 0103, body=The bike is done, total, parts=40, work=25., read=0, service_center=NULL"
    cols = "_id:address:body:service_center".split(":")
    row = parse_rows(out, cols)[0]
    assert row["body"] == "The bike is done, total, parts=40, work=25., read=0"
    assert row["service_center"] == "NULL" and list(row) == cols


def test_a_contact_cannot_write_vcard_lines_of_its_own():
    vcf = to_vcf([{"name": "Shop\nTEL:+1 555 9999", "phones": ["+1 555 0100"], "emails": [], "addresses": [], "orgs": [], "notes": []}])
    assert vcf.count("\nTEL") == 1 and "FN:Shop\\nTEL:+1 555 9999" in vcf
