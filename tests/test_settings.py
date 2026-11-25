from __future__ import annotations

import pytest

from phonevault.modules.settings import NEVER_RESTORE_PREFIXES, _parse, restorable, write_restore_script


@pytest.mark.parametrize("key", ["android_id", "wifi_x", "wifi_sleep_policy", "bluetooth_address",
                                 "ANDROID_ID", "lock_screen_owner_info", "sim_1_x", "miui_foo"])
def test_restorable_blocks_identity_and_radio_keys(key):
    assert restorable("secure", key) is False


@pytest.mark.parametrize("key", ["screen_brightness", "screen_off_timeout", "font_scale", "haptic_feedback_enabled",
                                 "accessibility_enabled"])
def test_restorable_allows_plain_settings(key):
    assert restorable("system", key) is True


def test_never_restore_prefixes_cover_the_three_named_keys():
    for k in ("android_id", "wifi_x", "bluetooth_address"):
        assert any(k.startswith(p) for p in NEVER_RESTORE_PREFIXES)


def test_parse_settings_list_output():
    out = "screen_brightness=120\nfont_scale=1.15\nweird=a=b\r\nno equals here\n"
    assert _parse(out) == {"screen_brightness": "120", "font_scale": "1.15", "weird": "a=b"}


SNAPSHOT = {
    "settings": {
        "system": {"screen_brightness": "120", "font_scale": "1.15", "empty": "", "nothing": "null",
                   "quoted": 'say "hi"', "percent": "50%"},
        "secure": {"android_id": "abcdef0123456789", "accessibility_enabled": "1"},
        "global": {"wifi_x": "1", "bluetooth_address": "AA:BB", "airplane_mode_on": "0"},
    }
}


def test_write_restore_script_contains_safe_keys_only(tmp_path):
    path = write_restore_script(SNAPSHOT, tmp_path / "restore-settings.cmd", r"C:\tools\adb.exe")
    text = path.read_bytes().decode("utf-8")  # bytes: keep the CRLF the .cmd file must have
    assert "shell \"settings put system screen_brightness '120'\"" in text
    assert "shell \"settings put secure accessibility_enabled '1'\"" in text
    assert "shell \"settings put global airplane_mode_on '0'\"" in text
    assert "android_id" not in text
    assert "wifi_x" not in text
    assert "bluetooth_address" not in text
    # blanks and nulls are not restored
    assert "put system empty" not in text and "put system nothing" not in text
    assert "put system percent '50%%'" in text
    assert "quoted" not in text          # a quote could end the command: left out, and counted
    assert text.startswith("@echo off")
    assert 'set ADB="C:\\tools\\adb.exe"' in text
    assert "echo Restored 5 settings. 1 with special characters left out" in text
    assert text.endswith("pause")


def test_write_restore_script_uses_exact_crlf_line_endings(tmp_path):
    path = write_restore_script(SNAPSHOT, tmp_path / "restore-settings.cmd", r"C:\tools\adb.exe")
    raw = path.read_bytes()
    assert b"\r\r\n" not in raw
    assert raw.startswith(b"@echo off\r\nrem ")


@pytest.mark.parametrize("value", ['x" & calc & "', "a'; reboot; '", "$(reboot)", "`reboot`", "a|b", "a>b", "a^b", "!x!", "a\\b", "line\nbreak", "a&b"])
def test_a_hostile_setting_never_becomes_a_command(tmp_path, value):
    """Any app that may write settings can choose a value. It must not run on the PC or on the new phone."""
    snap = {"settings": {"system": {"screen_brightness": "120", "evil": value, "a b": "1"}, "secure": {}, "global": {}}}
    text = write_restore_script(snap, tmp_path / "r.cmd", "adb").read_text("utf-8")
    assert "evil" not in text and "put system a b" not in text
    assert "Restored 1 settings. 2 with" in text


def test_restore_over_adb_quotes_every_value(tmp_path):
    import json
    from phonevault.modules.settings import apply_settings
    sent = []

    class Adb:
        def shell(self, cmd, timeout=120):
            sent.append(cmd)
            return ""

    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"settings": {"system": {"evil": "$(reboot) `id` 'x", "x;reboot": "1"}}}), "utf-8")
    res = apply_settings(Adb(), p)
    assert sent == ["settings put system evil '$(reboot) `id` '\"'\"'x'"]
    assert res["applied"] == 1 and "system/x;reboot" in res["failed"]
