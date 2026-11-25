from __future__ import annotations

import json
import os
import subprocess
import sys

from conftest import PROJECT_ROOT
from phonevault import cli
from phonevault.core.config import Config


def test_module_help_exits_zero(home):
    env = dict(os.environ, PHONEVAULT_HOME=str(home), PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-m", "phonevault", "--help"], cwd=str(PROJECT_ROOT),
                       capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 0, p.stderr
    for cmd in ("sync", "scan", "undo", "runs", "config", "restore"):
        assert cmd in p.stdout


def _json_part(out: str) -> dict:
    body = out.split("(file:")[0]
    return json.loads(body)


def test_config_prints_json_with_pc_root(home, capsys):
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    data = _json_part(out)
    assert "pc_root" in data and "trash_dir" in data and "gallery_roots" in data
    assert f"(file: {home / 'config.json'})" in out
    assert (home / "config.json").exists()
    assert Config.path() == home / "config.json"


def test_config_set_persists_to_tmp_home(home, pc, tmp_path, capsys):
    trash = tmp_path / "trash"
    assert cli.main(["config", "--set", f"pc_root={pc}", f"trash_dir={trash}", "gallery_roots=DCIM, Pictures"]) == 0
    data = _json_part(capsys.readouterr().out)
    assert data["pc_root"] == str(pc)
    assert data["trash_dir"] == str(trash)
    assert data["gallery_roots"] == ["DCIM", "Pictures"]
    saved = json.loads((home / "config.json").read_text(encoding="utf-8"))
    assert saved["pc_root"] == str(pc)
    assert Config.load().pc_root == str(pc)


def test_config_set_unknown_key_fails(home, capsys):
    assert cli.main(["config", "--set", "nope=1"]) == 1
    assert "unknown key nope" in capsys.readouterr().out


def test_runs_and_undo_with_empty_journal(home, pc, tmp_path, capsys):
    cli.main(["config", "--set", f"pc_root={pc}", f"trash_dir={tmp_path / 'trash'}"])
    capsys.readouterr()
    assert cli.main(["runs"]) == 0
    assert cli.main(["undo"]) == 1
    assert "Nothing to undo." in capsys.readouterr().out
    assert cli.main(["undo", "20990101-000000-dead"]) == 1
    assert "No run" in capsys.readouterr().out


def test_missing_subcommand_is_a_usage_error(home):
    try:
        cli.main([])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("argparse should exit with code 2")


def test_config_refuses_the_token_and_reads_booleans(home, capsys):
    assert cli.main(["config", "--set", "api_token=x"]) == 1
    assert cli.main(["config", "--set", "watch_enabled=true"]) == 0 and Config.load().watch_enabled is True
    assert cli.main(["config", "--set", "watch_enabled=false"]) == 0 and Config.load().watch_enabled is False
    assert cli.main(["config", "--set", 'album_aliases={"camera": "Cam"}']) == 0 and Config.load().album_aliases == {"camera": "Cam"}
    out = capsys.readouterr().out
    assert "api_token" not in out.replace("unknown key api_token", "")


def test_defaults_belong_to_nobody(home):
    from pathlib import Path
    c = Config()
    assert c.root == Path.home() / "PhoneVault" and c.trash == c.root / ".phonevault" / "trash"
    assert c.gallery_roots == ["DCIM", "Pictures", "Movies"] and c.phone_name_hint == "" and c.adb_path == ""
