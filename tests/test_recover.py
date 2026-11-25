"""What a power cut leaves behind is cleaned up on the next start."""
from __future__ import annotations

from pathlib import Path

from phonevault.core.engine import Engine


def test_recover_after_power_cut_marks_run_and_clears_staging(engine: Engine, tmp_path: Path):
    """Simulate a crash: a run left 'running' and a half file in staging. A new Engine cleans both."""
    rid = engine.journal.start_run("gallery", note="crashed")
    half = engine.staging / rid / "000001_half.jpg"
    half.parent.mkdir(parents=True, exist_ok=True)
    half.write_bytes(b"x" * 10)
    out = engine.recover()
    assert out == {"interrupted": 1, "staged_files": 1}
    assert engine.journal.run(rid).status == "interrupted"
    assert not half.exists()
    assert list(engine.cfg.trash.rglob("000001_half.jpg"))
    # album folders were never touched: nothing half-written outside staging
    assert not list(engine.cfg.root.rglob("000001_half.jpg"))
