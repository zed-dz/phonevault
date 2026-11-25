"""Where things live. One JSON file, plain keys, safe defaults."""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Folders on the phone that hold gallery albums (photos + videos).
GALLERY_ROOTS = ["DCIM", "Pictures", "Movies"]
# Other folders worth mirroring as plain files.
FILE_ROOTS = ["Download", "Documents", "Music", "Recordings", "Ringtones", "Notifications", "Alarms"]
# App "external data": readable over adb without root.
# Android/media holds chat apps' local backups and media; Android/data holds per-app files; obb = game packs.
EXTDATA_ROOTS = ["Android/media", "Android/data", "Android/obb"]
EXTDATA_SKIP_DIRS = {"cache", "code_cache", "no_backup", ".thumbnails", "thumbnails"}
EXTDATA_SKIP_PACKAGES = {"com.android.chrome", "com.google.android.youtube", "com.android.vending", "com.google.android.gms"}
SKIP_NAMES = {".nomedia", ".thumbnails", ".trash", "Thumbs.db", "desktop.ini"}

MEDIA_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp", ".dng", ".raw",
    ".mp4", ".mov", ".3gp", ".mkv", ".avi", ".webm", ".m4v",
}

# phone album name (lower case) -> PC folder name, for albums most phones have. Change them in config.json.
ALBUM_ALIASES = {
    "camera": "Camera",
    "screenshot": "Screenshots",
    "screenshots": "Screenshots",
    "whatsapp images": "WhatsApp Images",
    "whatsapp video": "WhatsApp Video",
    "whatsapp animated gifs": "WhatsApp GIFs",
}


def app_dir() -> Path:
    """Config, index, journal. %LOCALAPPDATA%/PhoneVault on Windows, ~/.local/share/phonevault elsewhere."""
    base = os.environ.get("PHONEVAULT_HOME")
    if not base:
        if os.name == "nt":
            base = os.path.join(os.environ.get("LOCALAPPDATA") or str(Path.home()), "PhoneVault")
        else:
            base = os.path.join(os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share"), "phonevault")
    p = Path(base)
    p.mkdir(parents=True, exist_ok=True)
    return p


def devices_off() -> bool:
    """PHONEVAULT_NO_DEVICES=1: never start adb, never open MTP. The tests and the demo set it."""
    return os.environ.get("PHONEVAULT_NO_DEVICES", "").lower() in ("1", "true", "yes", "on")


def find_adb() -> str:
    """adb on PATH, else the usual install folders. "" when there is none."""
    if devices_off():
        return ""
    hit = shutil.which("adb")
    if hit:
        return hit
    exe = "adb.exe" if os.name == "nt" else "adb"
    cands: list[Path] = []
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        if os.environ.get(env):
            cands.append(Path(os.environ[env]) / "platform-tools" / exe)
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        cands.append(local / "Android" / "Sdk" / "platform-tools" / exe)
        pkgs = local / "Microsoft" / "WinGet" / "Packages"
        if pkgs.is_dir():
            cands += sorted(pkgs.glob("Google.PlatformTools_*/platform-tools/adb.exe"))
    else:
        cands += [Path.home() / "Android" / "Sdk" / "platform-tools" / exe,
                  Path.home() / "Library" / "Android" / "sdk" / "platform-tools" / exe]
    for c in cands:
        if c.is_file():
            return str(c)
    return ""


@dataclass
class Config:
    pc_root: str = field(default_factory=lambda: str(Path.home() / "PhoneVault"))
    adb_path: str = ""  # empty = look on PATH and in the usual folders
    device_serial: str = ""  # empty = the one phone adb sees
    wifi_address: str = ""  # "192.168.1.20:5555" once `phonevault wifi` ran
    phone_name_hint: str = ""  # the phone's name under "This PC" (Windows). Empty = the MTP link is off
    gallery_roots: list[str] = field(default_factory=lambda: list(GALLERY_ROOTS))
    file_roots: list[str] = field(default_factory=lambda: list(FILE_ROOTS))
    extdata_roots: list[str] = field(default_factory=lambda: list(EXTDATA_ROOTS))
    extdata_skip_packages: list[str] = field(default_factory=lambda: sorted(EXTDATA_SKIP_PACKAGES))
    album_aliases: dict[str, str] = field(default_factory=lambda: dict(ALBUM_ALIASES))
    extdata_subfolder: str = "Phone app data"
    files_subfolder: str = "Phone files"  # pc_root / "Phone files" / Download / ...
    new_albums_subfolder: str = "_New from phone"  # albums with no PC match land here
    apps_subfolder: str = "Phone apps"
    settings_subfolder: str = "Phone settings"
    pim_subfolder: str = "Phone contacts-sms-calls"
    trash_dir: str = ""  # empty = <pc_root>/.phonevault/trash
    dashboard_port: int = 8765
    watch_enabled: bool = False  # the dashboard looks for the phone every N minutes and syncs by itself
    watch_interval_min: int = 15
    watch_full_every_hours: int = 6
    # The dashboard asks every request for this token, from this PC too. Made on the first `phonevault serve`.
    api_token: str = ""

    @property
    def root(self) -> Path:
        return Path(self.pc_root)

    @property
    def state_dir(self) -> Path:
        """Staging + trash. Inside pc_root, so every move is a rename on the same drive."""
        return self.root / ".phonevault"

    @property
    def trash(self) -> Path:
        return Path(self.trash_dir) if self.trash_dir else self.state_dir / "trash"

    @property
    def adb(self) -> str:
        return self.adb_path or find_adb()

    @staticmethod
    def path() -> Path:
        return app_dir() / "config.json"

    @classmethod
    def load(cls) -> "Config":
        p = cls.path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            return cls(**known)
        c = cls()
        c.save()
        return c

    def save(self) -> None:
        p = self.path()
        p.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(p, 0o600)      # it holds the dashboard token; no effect on Windows, where the profile folder is private
        except OSError:
            pass

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("api_token", None)        # never echo the secret through the API or `phonevault config`
        return d


def ensure_api_token(cfg: "Config") -> str:
    """Give the server a random token once, kept in config.json. Called by serve() at start."""
    import secrets
    if not cfg.api_token:
        cfg.api_token = secrets.token_urlsafe(24)
        cfg.save()
    return cfg.api_token
