"""AdbTransport parsing, with run()/shell() replaced by canned strings. No real adb is called."""
from __future__ import annotations

import pytest

from phonevault.transports.adb import AdbError, AdbTransport

FIND_STAT_OUTPUT = (
    "1048576\t1700000000\t/sdcard/DCIM/Camera/IMG_0001.jpg\n"
    "2048\t1700000100\t/sdcard/DCIM/My Photos/IMG 1 (copy).jpg\n"
    "4096\t1700000200\t/sdcard/DCIM/صور/صورة الباب.jpg\n"
    "find: /sdcard/DCIM/Private: Permission denied\n"
    "notanumber\t1700000300\t/sdcard/DCIM/bad.jpg\n"
    "\n"
    "77\t1700000400\tDCIM/already-relative.jpg\r\n"
)

DEVICES_OUTPUT = (
    "List of devices attached\n"
    "abc123def      device product:px model:Phone_X device:px transport_id:1\n"
    "192.168.1.20:5555      unauthorized transport_id:2\n"
    "\n"
)


@pytest.fixture
def adb(tmp_path):
    fake_exe = tmp_path / "adb.exe"
    fake_exe.write_bytes(b"")
    return AdbTransport(str(fake_exe))


def test_list_tree_parses_find_stat_output(adb, monkeypatch):
    sent: list[tuple] = []

    def fake_run(self, *args, timeout=120, check=True, binary=False):
        sent.append(args)
        return FIND_STAT_OUTPUT  # goes through the real shell(), which normalises CRLF

    monkeypatch.setattr(AdbTransport, "run", fake_run)
    files = adb.list_tree("DCIM")
    assert sent and sent[0][0] == "shell"
    assert sent[0][1].startswith("find /sdcard/DCIM -type f -exec stat")
    paths = [f.path for f in files]
    assert paths == [
        "DCIM/Camera/IMG_0001.jpg",
        "DCIM/My Photos/IMG 1 (copy).jpg",
        "DCIM/صور/صورة الباب.jpg",
        "DCIM/already-relative.jpg",
    ]
    first = files[0]
    assert (first.name, first.size, first.mtime, first.is_dir) == ("IMG_0001.jpg", 1048576, 1700000000.0, False)
    assert first.parent == "DCIM/Camera" and first.top == "DCIM"
    spaced = files[1]
    assert spaced.name == "IMG 1 (copy).jpg" and spaced.parent == "DCIM/My Photos"
    arabic = files[2]
    assert arabic.name == "صورة الباب.jpg"
    assert arabic.parent == "DCIM/صور"
    assert files[3].size == 77


def test_list_tree_quotes_roots_with_spaces(adb, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(AdbTransport, "shell", lambda self, c, timeout=120: sent.append(c) or "")
    assert adb.list_tree("My Folder/sub dir") == []
    assert "'/sdcard/My Folder/sub dir'" in sent[0]


def test_list_dir_parses_ls_output(adb, monkeypatch):
    monkeypatch.setattr(AdbTransport, "shell", lambda self, c, timeout=120: "Camera/\nScreenshots/\nnotes.txt\n\n")
    items = adb.list_dir("DCIM")
    assert [(i.path, i.name, i.is_dir) for i in items] == [
        ("DCIM/Camera", "Camera", True), ("DCIM/Screenshots", "Screenshots", True), ("DCIM/notes.txt", "notes.txt", False)]
    monkeypatch.setattr(AdbTransport, "shell", lambda self, c, timeout=120: "DCIM/\nDownload/\nfile\n")
    assert adb.top_folders() == ["DCIM", "Download"]


def test_devices_parses_usb_and_wifi_entries(adb, monkeypatch):
    calls: list[tuple] = []

    def fake_run(self, *args, timeout=120, check=True, binary=False):
        calls.append(args)
        return DEVICES_OUTPUT

    monkeypatch.setattr(AdbTransport, "run", fake_run)
    devs = adb.devices()
    assert calls == [("devices", "-l")]
    assert len(devs) == 2
    usb, wifi = devs
    assert usb["serial"] == "abc123def" and usb["state"] == "device" and usb["wifi"] is False
    assert usb["model"] == "Phone_X" and usb["product"] == "px" and usb["transport_id"] == "1"
    assert wifi["serial"] == "192.168.1.20:5555" and wifi["state"] == "unauthorized" and wifi["wifi"] is True
    assert adb.unauthorized() is True


def test_devices_returns_empty_when_adb_fails(adb, monkeypatch):
    def boom(self, *args, **kw):
        raise AdbError("no adb")

    monkeypatch.setattr(AdbTransport, "run", boom)
    assert adb.devices() == []
    assert adb.available() is False


def test_available_picks_usb_device_and_describes_it(adb, monkeypatch):
    monkeypatch.setattr(AdbTransport, "run", lambda self, *a, **k: DEVICES_OUTPUT)
    assert adb.available() is True
    assert adb.serial == "abc123def"
    assert adb.describe() == "adb USB (Phone_X)"
    assert adb.has_shell is True


def test_available_false_when_adb_binary_missing(tmp_path, monkeypatch):
    t = AdbTransport(str(tmp_path / "missing-adb.exe"))
    monkeypatch.setattr(AdbTransport, "run", lambda self, *a, **k: pytest.fail("must not run adb"))
    assert t.available() is False
    assert t.describe() == "adb"


def test_available_tries_wifi_connect_when_no_device(adb, monkeypatch):
    calls: list[tuple] = []
    answers = iter(["List of devices attached\n\n", "", "List of devices attached\n192.168.1.20:5555 device model:Phone_X\n"])

    def fake_run(self, *args, timeout=120, check=True, binary=False):
        calls.append(args)
        return next(answers)

    monkeypatch.setattr(AdbTransport, "run", fake_run)
    adb.wifi_address = "192.168.1.20:5555"
    assert adb.available() is True
    assert ("connect", "192.168.1.20:5555") in calls
    assert adb.describe() == "adb Wi-Fi (Phone_X)"


def test_props_and_ip_address_parsing(adb, monkeypatch):
    monkeypatch.setattr(AdbTransport, "shell",
                        lambda self, c, timeout=120: "[ro.product.model]: [Phone X]\n[persist.sys.locale]: [en-GB]\n")
    assert adb.props() == {"ro.product.model": "Phone X", "persist.sys.locale": "en-GB"}
    monkeypatch.setattr(AdbTransport, "shell",
                        lambda self, c, timeout=120: "7: wlan0: <UP>\n    inet 192.168.1.20/24 brd 192.168.1.255\n")
    assert adb.ip_address() == "192.168.1.20"
    monkeypatch.setattr(AdbTransport, "shell", lambda self, c, timeout=120: "default via 10.0.0.1 dev wlan0 src 10.0.0.7\n")
    assert adb.ip_address() == "10.0.0.7"
    monkeypatch.setattr(AdbTransport, "shell", lambda self, c, timeout=120: "")
    assert adb.ip_address() is None


def test_base_command_includes_serial(adb):
    assert adb._base() == [adb.adb]
    adb.serial = "abc"
    assert adb._base() == [adb.adb, "-s", "abc"]


def test_parse_mdns_skips_pairing_and_dedupes():
    from phonevault.transports.adb import parse_mdns
    out = ("List of discovered mdns services\r\n"
           "adb-abc123-AbCdEf\t_adb-tls-connect._tcp\t192.168.1.20:37099\r\n"
           "adb-abc123-AbCdEf\t_adb-tls-pairing._tcp\t192.168.1.20:41000\r\n"
           "adb-abc123-AbCdEf\t_adb._tcp\t192.168.1.20:5555\r\n"
           "adb-abc123-AbCdEf\t_adb._tcp\t192.168.1.20:5555\r\n")
    assert parse_mdns(out) == ["192.168.1.20:37099", "192.168.1.20:5555"]
    assert parse_mdns("") == []



def test_granted_permissions_parses_dumpsys():
    from phonevault.modules.apps import granted_permissions

    class FakeAdb:
        def shell(self, cmd, timeout=120):
            return ("    runtime permissions:\n"
                    "      android.permission.READ_EXTERNAL_STORAGE: granted=true, flags=[ USER_SET|GRANTED_BY_DEFAULT]\n"
                    "      android.permission.CAMERA: granted=false, flags=[ USER_SET]\n"
                    "      android.permission.ACCESS_FINE_LOCATION: granted=true, flags=[ USER_SET]\n")

    assert granted_permissions(FakeAdb(), "x") == ["android.permission.ACCESS_FINE_LOCATION", "android.permission.READ_EXTERNAL_STORAGE"]



EMULATOR_AND_PHONE = (
    "List of devices attached\n"
    "emulator-5554          device product:sdk_gphone64_x86_64 model:sdk_gphone64_x86_64 device:emu64xa transport_id:37\n"
    "100.64.0.7:5555     device product:px_eu model:Phone_X device:px transport_id:30\n"
)


def test_an_emulator_is_never_mistaken_for_the_phone(adb, monkeypatch):
    """emulator-5554 once sorted as 'USB' and its empty settings were saved as the phone's."""
    monkeypatch.setattr(AdbTransport, "run", lambda self, *a, **k: EMULATOR_AND_PHONE)
    assert adb.available() is True
    assert adb.serial == "100.64.0.7:5555"
    assert "Phone_X" in adb.describe()


def test_only_an_emulator_means_no_phone(adb, monkeypatch):
    only_emu = "List of devices attached\nemulator-5554          device product:sdk_gphone64_x86_64 model:sdk_gphone64_x86_64 transport_id:37\n"
    monkeypatch.setattr(AdbTransport, "run", lambda self, *a, **k: only_emu)
    assert adb.available() is False


def test_a_pinned_emulator_is_allowed(monkeypatch):
    only_emu = "List of devices attached\nemulator-5554          device product:sdk_gphone64_x86_64 model:sdk_gphone64_x86_64 transport_id:37\n"
    monkeypatch.setattr(AdbTransport, "run", lambda self, *a, **k: only_emu)
    pinned = AdbTransport("adb", serial="emulator-5554")
    monkeypatch.setattr("pathlib.Path.exists", lambda self: True)
    assert pinned.available() is True


TWO_PHONES = ("List of devices attached\n"
              "aaa111      device product:px model:Phone_A transport_id:1\n"
              "bbb222      device product:py model:Phone_B transport_id:2\n")


def test_two_phones_are_never_guessed(adb, monkeypatch):
    monkeypatch.setattr(AdbTransport, "run", lambda self, *a, **k: TWO_PHONES)
    assert adb.available() is False and "More than one phone" in adb.problem
    pinned = AdbTransport(adb.adb, serial="bbb222")
    assert pinned.available() is True and pinned.serial == "bbb222"


def test_a_pinned_phone_that_is_away_is_not_swapped_for_another(adb, monkeypatch):
    monkeypatch.setattr(AdbTransport, "run", lambda self, *a, **k: DEVICES_OUTPUT)
    pinned = AdbTransport(adb.adb, serial="my-own-phone")
    assert pinned.available() is False and "my-own-phone" in pinned.problem


def test_phone_side_hash(adb, monkeypatch):
    monkeypatch.setattr(AdbTransport, "shell", lambda self, c, timeout=120: "ab" * 32 + "  /sdcard/DCIM/a.jpg\n")
    assert adb.sha256("DCIM/a.jpg") == "ab" * 32
    monkeypatch.setattr(AdbTransport, "shell", lambda self, c, timeout=120: "")
    assert adb.sha256("DCIM/a.jpg") is None


def test_odd_package_names_are_dropped(monkeypatch):
    from phonevault.modules.apps import apk_name, list_apps

    class FakeAdb:
        def shell(self, cmd, timeout=120):
            return "package:/data/app/x/base.apk=org.example.ok versionCode:3\npackage:/data/app/y/base.apk=.. versionCode:1\n"

    assert [a["package"] for a in list_apps(FakeAdb())] == ["org.example.ok"]
    assert apk_name("/data/app/..\\..\\evil.apk") == "evil.apk" and apk_name("/data/app/x/..") == "_.apk"


def test_no_devices_switch_wins(monkeypatch, cfg):
    from phonevault.transports import adb_for, pick_transport, probe
    assert probe(cfg)["adb"] is False
    for fn in (adb_for, pick_transport):
        with pytest.raises(RuntimeError, match="switched off"):
            fn(cfg)
