"""Small JPEG previews for the review page. Never raises.

thumb(path) -> JPEG bytes, cached under <app_dir>/thumbs/<sha1 of path+mtime+size>.jpg.
Images go through Pillow (EXIF rotation honoured). Videos get one frame from
ffmpeg when it is on PATH, otherwise a dark placeholder box with the extension.
Anything that fails (HEIC without a codec, a truncated file, a missing file)
also gets the placeholder, so the dashboard grid always fills.
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
from pathlib import Path

from .config import app_dir

VIDEO_EXT = {".mp4", ".mov", ".3gp", ".mkv", ".webm", ".avi", ".m4v"}
BOX = (27, 31, 37)        # --panel-2 of the dashboard
INK = (139, 147, 161)     # --muted
_FFMPEG_TIMEOUT = 20


def cache_dir() -> Path:
    d = app_dir() / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cache_key(path: Path, mtime: float, size: int) -> str:
    return hashlib.sha1(f"{path}|{mtime:.3f}|{size}".encode("utf-8", "surrogateescape")).hexdigest()


def is_video(path: Path | str) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXT


def placeholder(text: str, size: int = 256) -> bytes:
    """A dark box with the extension written in the middle (and a play mark for videos)."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (size, size), BOX)
    d = ImageDraw.Draw(img)
    label = (text or "?").strip().upper().lstrip(".")[:6] or "?"
    try:
        font = ImageFont.load_default(size=max(12, size // 7))
    except TypeError:  # very old Pillow: no size argument
        font = ImageFont.load_default()
    try:
        l, t, r, b = d.textbbox((0, 0), label, font=font)
        w, h = r - l, b - t
    except Exception:  # noqa: BLE001
        w, h = len(label) * size // 12, size // 8
    d.rectangle([size // 10, size // 10, size - size // 10, size - size // 10], outline=(49, 55, 66), width=max(1, size // 128))
    d.text(((size - w) / 2, (size - h) / 2 - size // 14), label, fill=INK, font=font)
    if "." + label.lower() in VIDEO_EXT:
        cx, cy, r = size / 2, size * 0.72, size // 12
        d.polygon([(cx - r * 0.8, cy - r), (cx - r * 0.8, cy + r), (cx + r, cy)], fill=INK)
    return _jpeg(img)


def _jpeg(img, quality: int = 80) -> bytes:
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality, optimize=True)
    return out.getvalue()


def _image_thumb(path: Path, size: int) -> bytes:
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im.draft("RGB", (size * 2, size * 2))       # JPEG: decode at reduced scale, much faster
        im = ImageOps.exif_transpose(im) or im
        im.thumbnail((size, size))
        if im.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", im.size, BOX)
            bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        return _jpeg(im)


def _video_frame(path: Path, size: int) -> bytes | None:
    ff = shutil.which("ffmpeg")
    if not ff:
        return None
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for seek in (["-ss", "1"], []):          # a clip shorter than 1 s has no frame at 1 s: retry from 0
        cmd = [ff, "-v", "error", "-nostdin", "-protocol_whitelist", "file", *seek, "-i", str(path), "-frames:v", "1",
               "-vf", f"scale={size}:-1", "-f", "image2", "pipe:1"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=_FFMPEG_TIMEOUT, creationflags=flags)
        except Exception:  # noqa: BLE001
            return None
        if r.returncode == 0 and r.stdout:
            try:
                from PIL import Image
                with Image.open(io.BytesIO(r.stdout)) as im:
                    im = im.convert("RGB")
                    im.thumbnail((size, size))
                    return _jpeg(im)
            except Exception:  # noqa: BLE001
                return None
    return None


def thumb(path: Path, size: int = 256) -> bytes:
    """JPEG thumbnail of an image or video file. Never raises; falls back to a placeholder."""
    p = Path(path)
    ext = p.suffix
    try:
        st = p.stat()
        cache = cache_dir() / f"{cache_key(p, st.st_mtime, st.st_size)}.jpg"
        if cache.exists():
            return cache.read_bytes()
        if is_video(p):
            data = _video_frame(p, size) or placeholder(ext, size)
        else:
            try:
                data = _image_thumb(p, size)
            except Exception:  # noqa: BLE001
                data = placeholder(ext, size)
        try:
            tmp = cache.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, cache)
        except OSError:
            pass
        return data
    except Exception:  # noqa: BLE001
        try:
            return placeholder(ext, size)
        except Exception:  # noqa: BLE001
            return _MIN_JPEG


# A valid 1x1 grey JPEG, the last resort if even Pillow fails.
_MIN_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f14"
    "1d1a1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e333432ffc0000b080001000101011100"
    "ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b5100002010303020403050504040000"
    "017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a"
    "3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a9293949596"
    "9798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2"
    "f3f4f5f6f7f8f9faffda0008010100003f00fbd3ffd9"
)
