"""Dedupe: a newcomer whose content is already filed goes to the trash folder, undoable."""
from __future__ import annotations

from pathlib import Path

from phonevault.core.engine import Engine


def test_dedupe_sweep_moves_content_duplicates_from_new_albums(engine: Engine, tmp_path: Path):
    from phonevault.modules import dedupe
    root = engine.cfg.root
    (root / "PROJECTS/garden plans").mkdir(parents=True, exist_ok=True)
    (root / "PROJECTS/garden plans/old.jpg").write_bytes(b"SAME" * 1000)
    (root / "_New from phone/album x").mkdir(parents=True)
    (root / "_New from phone/album x/new.jpg").write_bytes(b"SAME" * 1000)      # duplicate content
    (root / "_New from phone/album x/unique.jpg").write_bytes(b"ONLY" * 1000)   # keep
    res = dedupe.sweep(engine)
    assert res["duplicates"] == 1
    assert not (root / "_New from phone/album x/new.jpg").exists()
    assert (root / "_New from phone/album x/unique.jpg").exists()
    assert (root / "PROJECTS/garden plans/old.jpg").exists()
    assert list(engine.cfg.trash.rglob("new.jpg"))
    engine.undo(res["run_id"])
    assert (root / "_New from phone/album x/new.jpg").exists()
