from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import ALIASES
from phonevault.core.albums import AlbumMapper, norm


@pytest.fixture
def map_path(tmp_path) -> Path:
    return tmp_path / "home" / "album-map.json"


@pytest.fixture
def mapper(pc, map_path) -> AlbumMapper:
    return AlbumMapper(pc, map_path, aliases=ALIASES)


def test_norm_folds_case_whitespace_and_punctuation():
    assert norm("Garden_Plans") == "garden plans"
    assert norm("  GARDEN-plans.  ") == "garden plans"
    assert norm("Phone (camera)") == "phone camera"


def test_exact_match(mapper, pc):
    m = mapper.resolve("photo set 1")
    assert m.how == "exact"
    assert Path(m.pc_folder) == pc / "photo set 1"


def test_nested_category_folder_is_found(mapper, pc):
    m = mapper.resolve("garden plans", "Pictures/garden plans")
    assert m.how == "exact"
    assert Path(m.pc_folder) == pc / "PROJECTS" / "garden plans"


def test_exact_match_prefers_shallowest_folder(pc, map_path):
    (pc / "garden plans").mkdir()
    m = AlbumMapper(pc, map_path, aliases=ALIASES).resolve("garden plans")
    assert Path(m.pc_folder) == pc / "garden plans"


def test_case_and_punctuation_insensitive_match(mapper, pc):
    m = mapper.resolve("Garden_Plans")
    assert m.how == "fold"
    assert Path(m.pc_folder) == pc / "PROJECTS" / "garden plans"


def test_fuzzy_match_with_single_clear_winner(mapper, pc):
    m = mapper.resolve("kitchen renovaton sketches")
    assert m.how == "fuzzy"
    assert m.score >= 0.86
    assert Path(m.pc_folder) == pc / "PROJECTS" / "kitchen renovation sketches"


def test_fuzzy_ambiguity_falls_back_to_new_from_phone(mapper, pc):
    # "photo set 1" and "photo set 2" score the same -> no clear winner
    m = mapper.resolve("photo set")
    assert m.how == "new"
    assert Path(m.pc_folder) == pc / "_New from phone" / "photo set"


def test_no_match_at_all_goes_to_new_from_phone(mapper, pc):
    m = mapper.resolve("Random Stuff", "Pictures/Random Stuff")
    assert m.how == "new"
    assert Path(m.pc_folder) == pc / "_New from phone" / "Random Stuff"


def test_existing_new_from_phone_folder_never_counts_as_a_match(pc, map_path):
    (pc / "_New from phone" / "Random Stuff").mkdir(parents=True)
    m = AlbumMapper(pc, map_path, aliases=ALIASES).resolve("Random Stuff")
    assert m.how == "new"
    assert Path(m.pc_folder) == pc / "_New from phone" / "Random Stuff"


def test_alias_screenshots(mapper, pc):
    m = mapper.resolve("Screenshots", "Pictures/Screenshots")
    assert m.how == "alias"
    assert Path(m.pc_folder) == pc / "Phone screenshots"


def test_alias_camera(mapper, pc):
    m = mapper.resolve("Camera", "DCIM/Camera")
    assert m.how == "alias"
    assert Path(m.pc_folder) == pc / "Phone camera"


def test_alias_target_missing_on_pc_still_points_under_pc_root(pc, map_path):
    import shutil
    shutil.rmtree(pc / "Phone camera")
    m = AlbumMapper(pc, map_path, aliases=ALIASES).resolve("Camera")
    assert m.how == "alias"
    assert Path(m.pc_folder) == pc / "Phone camera"


def test_saved_decision_wins_over_matching(mapper, pc):
    custom = pc / "My Phone Camera"
    mapper.set("DCIM/Camera", str(custom))
    m = mapper.resolve("Camera", "DCIM/Camera")
    assert m.how == "saved"
    assert Path(m.pc_folder) == custom


def test_saved_decision_by_album_name_applies_without_phone_path(mapper, pc):
    custom = pc / "Elsewhere"
    mapper.set("photo set 1", str(custom))
    assert mapper.resolve("photo set 1").how == "saved"
    assert Path(mapper.resolve("photo set 1").pc_folder) == custom


def test_set_persists_to_album_map_json(mapper, map_path, pc):
    mapper.set("DCIM/Camera", str(pc / "X"))
    data = json.loads(map_path.read_text(encoding="utf-8"))
    assert data["DCIM/Camera"] == str(pc / "X")


def test_resolve_persists_decision_so_next_run_is_identical(mapper, map_path, pc):
    first = mapper.resolve("kitchen renovaton sketches", "Pictures/kitchen renovaton sketches")
    assert first.how == "fuzzy"
    again = AlbumMapper(pc, map_path, aliases=ALIASES).resolve("kitchen renovaton sketches",
                                              "Pictures/kitchen renovaton sketches")
    assert again.how == "saved"
    assert again.pc_folder == first.pc_folder


def test_corrupt_map_file_is_ignored(pc, map_path):
    map_path.parent.mkdir(parents=True)
    map_path.write_text("{not json", encoding="utf-8")
    m = AlbumMapper(pc, map_path, aliases=ALIASES)
    assert m.saved == {}
    assert m.resolve("photo set 1").how == "exact"


def test_invalidate_rescans_pc_folders(mapper, pc):
    assert mapper.resolve("Brand New Album").how == "new"
    (pc / "Brand New Album").mkdir()
    mapper.saved.clear()
    assert mapper.resolve("Brand New Album").how == "new"  # cached catalogue
    mapper.saved.clear()  # resolve() persists its own decision; drop it again
    mapper.invalidate()
    assert mapper.resolve("Brand New Album").how == "exact"
