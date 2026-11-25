"""The shape every phone link must have. MTP and adb both fit it."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable


@dataclass
class RemoteFile:
    path: str              # phone path relative to internal storage, forward slashes: "DCIM/Camera/IMG_1.jpg"
    name: str
    size: int
    mtime: float | None    # epoch seconds, None if unknown
    is_dir: bool = False

    @property
    def parent(self) -> str:
        return self.path.rsplit("/", 1)[0] if "/" in self.path else ""

    @property
    def top(self) -> str:
        return self.path.split("/", 1)[0]

    def to_dict(self) -> dict:
        return asdict(self)


# on_done(remote_path, local_path, ok, error_text)
DoneCb = Callable[[str, Path, bool, str], None]


class Transport:
    name = "base"

    def available(self) -> bool:
        raise NotImplementedError

    def describe(self) -> str:
        return self.name

    def list_tree(self, root: str, max_depth: int = 6) -> list[RemoteFile]:
        """Every file under <internal storage>/<root>, recursively."""
        raise NotImplementedError

    def list_dir(self, path: str) -> list[RemoteFile]:
        raise NotImplementedError

    def pull(self, remote_path: str, local_path: Path) -> None:
        raise NotImplementedError

    def pull_batch(self, pairs: list[tuple[str, Path]], on_done: DoneCb,
                   cancel: Callable[[], bool] | None = None,
                   on_start: Callable[[str], None] | None = None,
                   sizes: dict[str, int] | None = None) -> None:
        """Default: one by one. Transports may override with something faster."""
        for remote, local in pairs:
            if cancel and cancel():
                return
            if on_start:
                on_start(remote)
            try:
                self.pull(remote, local)
                on_done(remote, local, True, "")
            except Exception as e:  # noqa: BLE001
                on_done(remote, local, False, str(e))

    def sha256(self, remote_path: str) -> str | None:
        """Hash of the file as it is on the phone now. None = this link cannot tell (then the file is copied and hashed here)."""
        return None

    # Capabilities beyond files (apps, settings...). Only adb has them.
    @property
    def has_shell(self) -> bool:
        return False
