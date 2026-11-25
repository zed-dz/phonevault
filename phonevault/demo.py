"""`phonevault demo`: the real dashboard with a made-up phone.

Everything lives in one new temp folder: the sample phone, the PC side and PhoneVault's own files. The
config, backups and phones you really have are not opened. All names and numbers are invented, and the
pictures are plain shapes drawn on the spot.
"""
from __future__ import annotations

import math
import os
import random
import tempfile
import time
from pathlib import Path

from .core.config import Config
from .transports.local import LocalTransport

APPS = {
    "org.example.notes": ("2.4.1", 241, True),
    "org.example.timer": ("1.9", 19, True),
    "org.example.recipes": ("5.0.3", 503, True),
    "org.example.transit": ("12.1", 1210, False),
    "org.example.paint": ("3.3", 330, True),
    "org.example.bank": ("8.0", 800, False),
}
CONTACTS = [("Front desk", "+1 555 0100"), ("Plumber", "+1 555 0101"), ("Library", "+1 555 0102"),
            ("Bike workshop", "+1 555 0103"), ("Landlord office", "+1 555 0104"), ("Pharmacy", "+1 555 0105")]
SMS = [("+1 555 0105", "Your order is ready to collect."), ("+1 555 0103", "The bike is done, total, parts=40, work=25."),
       ("+1 555 0102", "2 books are due on Friday."), ("+1 555 0100", "A parcel is waiting at the front desk.")]
SETTINGS = {"system": {"screen_brightness": "120", "font_scale": "1.1", "screen_off_timeout": "60000"},
            "secure": {"accessibility_enabled": "0"}, "global": {"window_animation_scale": "0.5"}}


class DemoPhone(LocalTransport):
    """A folder that also answers the handful of shell questions the apps, settings and contacts jobs ask."""
    name = "demo"
    demo = True
    adb = ""
    serial = "demo-phone"

    def __init__(self, root: Path, delay: float = 0.0):
        super().__init__(root)
        self.delay = delay      # seconds per file, so the progress bar has something to show

    def describe(self) -> str:
        return "demo phone (sample data)"

    @property
    def has_shell(self) -> bool:
        return True

    def _base(self) -> list[str]:
        raise RuntimeError("Not in the demo: this one needs a real phone.")

    def run(self, *args: str, **_: object) -> str:
        raise RuntimeError("Not in the demo: this one needs a real phone.")

    def pull(self, remote_path: str, local_path: Path) -> None:
        time.sleep(self.delay)
        super().pull(remote_path, local_path)

    def pull_abs(self, remote_abs: str, local_path: Path) -> None:
        time.sleep(self.delay)
        super().pull(remote_abs.lstrip("/"), local_path)

    def props(self) -> dict[str, str]:
        return {"ro.product.model": "Demo Phone", "ro.product.brand": "Example", "ro.build.version.release": "14",
                "ro.build.version.sdk": "34"}

    def shell(self, command: str, timeout: int = 120) -> str:
        c = command.split(" 2>")[0].strip()
        if c.startswith("pm list packages"):
            show = "--show-versioncode" in c
            return "\n".join(f"package:/data/app/{p}/base.apk={p} versionCode:{v[1]}" if show else f"package:{p}" for p, v in APPS.items())
        if c.startswith("pm path "):
            return f"package:/data/app/{c.split()[-1]}/base.apk"
        if c.startswith("dumpsys package "):
            v = APPS.get(c.split()[-1])
            if not v:
                return ""
            flag = "ALLOW_BACKUP " if v[2] else ""
            return (f"versionName={v[0]}\nversionCode={v[1]} minSdk=26\npkgFlags=[ HAS_CODE {flag}]\n"
                    "      android.permission.CAMERA: granted=true, flags=[ USER_SET]\n")
        if c.startswith("settings list "):
            return "\n".join(f"{k}={v}" for k, v in SETTINGS.get(c.split()[-1], {}).items())
        if "com.android.contacts/data" in c:
            return "\n".join(f"Row: {i} contact_id={i}, display_name={n}, mimetype=vnd.android.cursor.item/phone_v2, "
                             f"data1={p}, data2=2, data3=NULL, account_type=demo" for i, (n, p) in enumerate(CONTACTS))
        if "content://sms" in c:
            return "\n".join(f"Row: {i} _id={i}, thread_id={i}, address={a}, date={1767000000000 + i * 3600000}, date_sent=0, "
                             f"type=1, read=1, body={b}, service_center=NULL" for i, (a, b) in enumerate(SMS))
        if "content://call_log" in c:
            return (f"Row: 0 _id=0, number={CONTACTS[1][1]}, date=1767000000000, duration=62, type=2, name={CONTACTS[1][0]}, countryiso=US")
        return ""


# ---- sample files ----------------------------------------------------------
PALETTE = [(204, 32, 39), (27, 26, 23), (228, 168, 74), (64, 110, 142), (106, 141, 115), (186, 96, 70), (120, 96, 140)]
PAPER = (250, 246, 239)


def _shapes(path: Path, seed: int, size=(960, 720), when: float = 0.0) -> None:
    """An abstract picture: blocks, rings and lines. No people, no animals."""
    from PIL import Image, ImageDraw
    r = random.Random(seed)
    im = Image.new("RGB", size, PAPER)
    d = ImageDraw.Draw(im)
    w, h = size
    cols = r.sample(PALETTE, 4)
    kind = seed % 4
    if kind == 0:
        for i in range(8):
            x = i * w // 8
            d.rectangle([x, h - r.randint(h // 6, h - 60), x + w // 8 - 10, h], fill=cols[i % 4])
    elif kind == 1:
        for i in range(6, 0, -1):
            q = i * min(w, h) // 13
            d.ellipse([w // 2 - q, h // 2 - q, w // 2 + q, h // 2 + q], fill=cols[i % 4])
    elif kind == 2:
        for y in range(0, h, 120):
            for x in range(0, w, 120):
                if (x + y) // 120 % 2:
                    d.rectangle([x, y, x + 119, y + 119], fill=cols[r.randrange(4)])
                else:
                    d.polygon([(x, y), (x + 119, y), (x, y + 119)], fill=cols[r.randrange(4)])
    else:
        for i in range(5):
            pts = [(x, h * (i + 1) / 6 + 50 * math.sin(x / 120 + i + seed)) for x in range(0, w + 8, 8)]
            d.line(pts, fill=cols[i % 4], width=26, joint="curve")
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, quality=88)
    os.utime(path, (when, when))


def _screen(path: Path, seed: int, when: float) -> None:
    """A made-up app screen: a title bar and a few rows."""
    from PIL import Image, ImageDraw
    r = random.Random(seed)
    im = Image.new("RGB", (540, 1080), (245, 245, 247))
    d = ImageDraw.Draw(im)
    c = PALETTE[seed % len(PALETTE)]
    d.rectangle([0, 0, 540, 130], fill=c)
    d.rounded_rectangle([36, 60, 260, 92], 12, fill=(255, 255, 255))
    for i in range(6):
        y = 170 + i * 140
        d.rounded_rectangle([28, y, 512, y + 116], 18, fill=(255, 255, 255))
        d.rounded_rectangle([48, y + 24, 116, y + 92], 16, fill=PALETTE[r.randrange(len(PALETTE))])
        d.rounded_rectangle([140, y + 30, 140 + r.randint(180, 330), y + 52], 8, fill=(60, 60, 66))
        d.rounded_rectangle([140, y + 68, 140 + r.randint(120, 280), y + 84], 8, fill=(190, 190, 198))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)
    os.utime(path, (when, when))


def build_phone(root: Path) -> None:
    """The sample phone. Same content every time."""
    t0 = 1767000000.0      # a fixed date, so a kept date is easy to see
    n = 0
    for i in range(14):
        n += 1
        _shapes(root / "DCIM" / "Camera" / f"IMG_{20260110 + i}_0{i % 9 + 1}1500.jpg", n, when=t0 + i * 86400)
    for i in range(6):
        _screen(root / "Pictures" / "Screenshots" / f"Screenshot_{i + 1:02d}.png", i, t0 + i * 7200)
    for i in range(5):
        n += 1
        _shapes(root / "Pictures" / "Garden plans" / f"plan_{i + 1}.jpg", n, (720, 720), t0 + i * 3600)
    for i in range(4):
        n += 1
        _shapes(root / "Pictures" / "Tile samples" / f"tile_{i + 1}.jpg", n * 4 + 2, (720, 720), t0 + i * 60)
    # the same picture under another name: the tool must notice
    dup = root / "Pictures" / "Garden plans" / "copy of IMG.jpg"
    dup.write_bytes((root / "DCIM" / "Camera" / "IMG_20260110_011500.jpg").read_bytes())
    os.utime(dup, (t0 + 99, t0 + 99))
    docs = root / "Download"
    (docs / "Manuals").mkdir(parents=True, exist_ok=True)
    (docs / "shopping list.txt").write_text("nails\npaint\nlight bulbs\n", encoding="utf-8")
    (docs / "Manuals" / "washing machine.txt").write_text("Press start. Wait.\n" * 40, encoding="utf-8")
    (root / "Documents").mkdir(exist_ok=True)
    (root / "Documents" / "budget.csv").write_text("month,rent,food\n" + "".join(f"{m},500,{200 + m}\n" for m in range(1, 13)), encoding="utf-8")
    rnd = random.Random(7)
    for p in APPS:
        apk = root / "data" / "app" / p / "base.apk"
        apk.parent.mkdir(parents=True, exist_ok=True)
        apk.write_bytes(b"PK\x03\x04" + rnd.randbytes(60_000))


def build_pc(root: Path) -> None:
    """The PC side already has one filed album and one picture the phone has too."""
    (root / "House" / "Garden plans").mkdir(parents=True, exist_ok=True)
    (root / "Work").mkdir(exist_ok=True)


def make(base: Path | None = None, delay: float = 0.0) -> tuple[Config, DemoPhone]:
    """Build the sample world. Sets PHONEVAULT_HOME and PHONEVAULT_NO_DEVICES for this process."""
    from . import transports
    base = Path(base or tempfile.mkdtemp(prefix="phonevault-demo-"))
    home = base / "phonevault-home"
    home.mkdir(parents=True, exist_ok=True)
    os.environ["PHONEVAULT_HOME"] = str(home)
    os.environ["PHONEVAULT_NO_DEVICES"] = "1"
    build_phone(base / "sample phone")
    build_pc(base / "PC backup")
    cfg = Config(pc_root=str(base / "PC backup"))
    cfg.save()
    phone = DemoPhone(base / "sample phone", delay)
    transports.use(phone)
    return cfg, phone


def free_port(first: int = 8770) -> int:
    from .server.app import port_taken
    for p in range(first, first + 40):
        if not port_taken(p):
            return p
    raise RuntimeError("no free port between 8770 and 8809")
