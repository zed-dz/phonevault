"""Album name mapping: phone album -> PC folder.

The PC side may nest albums under category folders (House/Garden plans, ...).
The phone side is flat (Pictures/Garden plans). So we match on the album's
leaf name, in this order:
  1. a saved decision in album-map.json (you can edit it in the dashboard)
  2. exact name
  3. case-insensitive / punctuation-insensitive name
  4. fuzzy name (difflib ratio >= 0.86), only when there is one clear winner
  5. aliases from the config (Camera, Screenshots, ...)
  6. otherwise: <pc_root>/_New from phone/<album name>
Every decision is saved, so the next run is identical and you can change it.
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import ALBUM_ALIASES
from .index import folder_names
from .names import pc_name

FUZZY_MIN = 0.86
# Names too generic to guess with. They only ever match exactly.
GENERIC = {"video", "videos", "image", "images", "photo", "photos", "picture", "pictures", "new", "old",
           "misc", "other", "others", "stuff", "files", "docs", "temp", "test", "camera", "screenshots"}


def norm(s: str) -> str:
    s = s.casefold().strip()
    s = re.sub(r"[\s_\-\.]+", " ", s)
    s = re.sub(r"[^\w\s]", "", s)
    return s.strip()


@dataclass
class Match:
    album: str          # phone album leaf name
    pc_folder: str      # absolute PC folder
    how: str            # saved | exact | fold | fuzzy | alias | new
    score: float = 1.0


class AlbumMapper:
    def __init__(self, pc_root: Path, map_path: Path, new_subfolder: str = "_New from phone",
                 aliases: dict[str, str] | None = None):
        self.aliases = ALBUM_ALIASES if aliases is None else aliases
        self.pc_root = Path(pc_root)
        self.map_path = Path(map_path)
        self.new_subfolder = new_subfolder
        self.saved: dict[str, str] = {}
        if self.map_path.exists():
            try:
                self.saved = json.loads(self.map_path.read_text(encoding="utf-8"))
            except Exception:
                self.saved = {}
        self._folders: list[tuple[str, str]] | None = None

    # ---- PC folder catalogue --------------------------------------------
    @property
    def folders(self) -> list[tuple[str, str]]:
        if self._folders is None:
            self._folders = folder_names(self.pc_root) if self.pc_root.exists() else []
        return self._folders

    def rebase(self, old: str, new: str) -> None:
        """The PC folder changed: decisions under the old one move along, anything else is dropped."""
        from .index import under
        out = {}
        for k, v in self.saved.items():
            if under(v, new):
                out[k] = v
            elif old and under(v, old):
                rel = str(v)[len(old.rstrip("\\/")):].lstrip("\\/")
                out[k] = str(Path(new) / rel) if rel else new
        self.pc_root = Path(new)
        self.saved = out
        self.save()
        self.invalidate()

    def invalidate(self) -> None:
        self._folders = None

    # ---- the decision ---------------------------------------------------
    def resolve(self, album: str, phone_path: str | None = None) -> Match:
        key = phone_path or album
        if key in self.saved and self.saved[key]:
            return Match(album, self.saved[key], "saved")
        if album in self.saved and self.saved[album]:
            return Match(album, self.saved[album], "saved")

        exact = [p for n, p in self.folders if n == album and not self._is_new_folder(p)]
        if len(exact) == 1:
            return self._keep(key, Match(album, exact[0], "exact"))
        if len(exact) > 1:
            # prefer the shallowest (a category-level folder beats a nested one)
            exact.sort(key=lambda p: p.count("\\") + p.count("/"))
            return self._keep(key, Match(album, exact[0], "exact"))

        na = norm(album)
        fold = [p for n, p in self.folders if norm(n) == na and not self._is_new_folder(p)]
        if fold:
            fold.sort(key=lambda p: p.count("\\") + p.count("/"))
            return self._keep(key, Match(album, fold[0], "fold"))

        if na in self.aliases:
            target = self.aliases[na]
            hit = [p for n, p in self.folders if n == target]
            pc = hit[0] if hit else str(self.pc_root / target)
            return self._keep(key, Match(album, pc, "alias"))

        best: tuple[float, str] | None = None
        second = 0.0
        for n, p in self.folders:
            if self._is_new_folder(p) or na in GENERIC or norm(n) in GENERIC or len(na) < 6:
                continue
            r = difflib.SequenceMatcher(None, na, norm(n)).ratio()
            if best is None or r > best[0]:
                second = best[0] if best else 0.0
                best = (r, p)
            elif r > second:
                second = r
        if best and best[0] >= FUZZY_MIN and best[0] - second >= 0.05:
            return self._keep(key, Match(album, best[1], "fuzzy", best[0]))

        return self._keep(key, Match(album, str(self.pc_root / self.new_subfolder / pc_name(album)), "new"))

    def _is_new_folder(self, p: str) -> bool:
        return self.new_subfolder in Path(p).parts

    def _keep(self, key: str, m: Match) -> Match:
        self.saved[key] = m.pc_folder
        self.save()
        return m

    def set(self, key: str, pc_folder: str) -> None:
        self.saved[key] = pc_folder
        self.save()

    def save(self) -> None:
        self.map_path.parent.mkdir(parents=True, exist_ok=True)
        self.map_path.write_text(json.dumps(self.saved, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
