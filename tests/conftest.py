"""Shared fixtures.

- a fake phone: a tmp folder tree served by LocalTransport
- a tmp PC root with the album folders the mapper is expected to find
- a Config pointing at both, trash under tmp
- PHONEVAULT_HOME pointed at a tmp dir so index.db / journal.db / album-map.json
  never touch the real %LOCALAPPDATA%\\PhoneVault
- no real phone, ever: PHONEVAULT_NO_DEVICES is on and starting adb, PowerShell or winget fails the test
"""
from __future__ import annotations

import os
os.environ["PHONEVAULT_DRIVECHECK"] = "off"  # tests never depend on this machine's disks
os.environ["PHONEVAULT_NO_DEVICES"] = "1"    # and never look for a phone
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# PHONEVAULT_HOME must be harmless before anything in phonevault can call app_dir().
# Every test then overrides it with its own tmp dir (see `home`).
_SESSION_HOME = tempfile.mkdtemp(prefix="phonevault-tests-")
os.environ["PHONEVAULT_HOME"] = _SESSION_HOME

from phonevault.core.config import Config  # noqa: E402
from phonevault.transports.local import LocalTransport  # noqa: E402

PHONE_MTIME = 1_600_000_000.0  # 2020-09-13, far from "now" so a preserved mtime is provable

# The fake phone. Albums were chosen to hit every mapping rule:
#   Camera        -> alias  -> "Phone camera"
#   Screenshots   -> alias  -> "Phone screenshots"
#   garden plans   -> exact  -> "PROJECTS/garden plans" (nested category folder)
#   kitchen renovaton sketches -> fuzzy -> "PROJECTS/kitchen renovation sketches"
#   Random Stuff  -> new    -> "_New from phone/Random Stuff"
PHONE_TREE: dict[str, bytes] = {
    "DCIM/Camera/IMG_0001.jpg": b"camera-one" * 100,
    "DCIM/Camera/IMG_0002.jpg": b"camera-two" * 100,
    "DCIM/Camera/VID_0001.mp4": b"video-bytes" * 300,
    "DCIM/.thumbnails/1.jpg": b"thumb",
    "Pictures/Screenshots/Screenshot_1.png": b"shot-one" * 50,
    "Pictures/garden plans/logo.png": b"logo-bytes" * 20,
    "Pictures/kitchen renovaton sketches/d1.jpg": b"d-one" * 40,
    "Pictures/Random Stuff/r1.jpg": b"random-one" * 30,
    "Pictures/Notes/todo.txt": b"not media at all",
    "Pictures/.nomedia": b"",
    "Download/docs/report.pdf": b"%PDF-fake" * 10,
    "Download/readme.txt": b"hello",
}
GALLERY_COPY_COUNT = 8  # IMG_0001, IMG_0002, VID_0001, Screenshot_1, logo, d1, r1 + Notes/todo.txt mirrored under Phone files

ALIASES = {"camera": "Phone camera", "screenshots": "Phone screenshots"}
PC_FOLDERS = [
    "Phone camera",
    "Phone screenshots",
    "PROJECTS/garden plans",
    "PROJECTS/kitchen renovation sketches",
    "photo set 1",
    "photo set 2",
]


def write(path: Path, content: bytes | str = b"x", mtime: float | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode("utf-8")
    path.write_bytes(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    shutil.rmtree(_SESSION_HOME, ignore_errors=True)


@pytest.fixture(autouse=True)
def no_real_programs(monkeypatch):
    """adb, PowerShell and friends must never start from a test."""
    real_run, real_popen = subprocess.run, subprocess.Popen

    def check(cmd):
        exe = Path(cmd[0] if isinstance(cmd, (list, tuple)) else str(cmd).split()[0]).stem.lower()
        if exe in ("adb", "powershell", "pwsh", "winget", "tailscale", "schtasks"):
            raise AssertionError(f"a test tried to start {cmd}")

    class Popen(real_popen):
        def __init__(self, cmd, *a, **k):
            check(cmd)
            super().__init__(cmd, *a, **k)

    def run(cmd, *a, **k):
        check(cmd)
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", Popen)
    monkeypatch.setattr(subprocess, "run", run)


@pytest.fixture
def mkfile():
    """write(path, content=b'x', mtime=None) -> Path"""
    return write


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("PHONEVAULT_HOME", str(h))
    return h


@pytest.fixture
def pc(tmp_path) -> Path:
    root = tmp_path / "pc"
    for f in PC_FOLDERS:
        (root / f).mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def phone_root(tmp_path) -> Path:
    root = tmp_path / "phone"
    for rel, content in PHONE_TREE.items():
        write(root / rel, content, PHONE_MTIME)
    return root


@pytest.fixture
def phone(phone_root) -> LocalTransport:
    return LocalTransport(phone_root)


@pytest.fixture
def trash(tmp_path) -> Path:
    return tmp_path / "trash"


@pytest.fixture
def cfg(pc, trash, home) -> Config:
    return Config(pc_root=str(pc), trash_dir=str(trash), album_aliases=ALIASES)


@pytest.fixture
def engine(cfg, home):
    # Engine is imported here, after PHONEVAULT_HOME points at this test's tmp dir.
    from phonevault.core.engine import Engine

    e = Engine(cfg)
    yield e
    e.close()
