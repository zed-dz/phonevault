from __future__ import annotations

import hashlib

from phonevault.core.hashing import CHUNK, quick_hash, sha256


def test_quick_hash_differs_for_different_content_same_size(tmp_path, mkfile):
    a = mkfile(tmp_path / "a.bin", b"A" * 1000)
    b = mkfile(tmp_path / "b.bin", b"B" * 1000)
    assert a.stat().st_size == b.stat().st_size
    assert quick_hash(a) != quick_hash(b)


def test_quick_hash_same_for_identical_content(tmp_path, mkfile):
    a = mkfile(tmp_path / "a.bin", b"same" * 500)
    b = mkfile(tmp_path / "b.bin", b"same" * 500)
    assert quick_hash(a) == quick_hash(b)


def test_quick_hash_differs_when_size_differs(tmp_path, mkfile):
    a = mkfile(tmp_path / "a.bin", b"x" * 10)
    b = mkfile(tmp_path / "b.bin", b"x" * 11)
    assert quick_hash(a) != quick_hash(b)


def test_quick_hash_explicit_size_matches_stat_size(tmp_path, mkfile):
    a = mkfile(tmp_path / "a.bin", b"q" * 777)
    assert quick_hash(a, 777) == quick_hash(a)


def test_quick_hash_large_file_tail_difference_is_detected(tmp_path, mkfile):
    big = b"0" * (CHUNK * 3)
    a = mkfile(tmp_path / "a.bin", big)
    b = mkfile(tmp_path / "b.bin", big[:-1] + b"1")
    assert quick_hash(a) != quick_hash(b)


def test_quick_hash_is_blind_to_middle_only_change_by_design(tmp_path, mkfile):
    """Documented trade-off: quick = size + head + tail. sha256 is the truth."""
    big = bytearray(b"0" * (CHUNK * 3))
    a = mkfile(tmp_path / "a.bin", bytes(big))
    big[CHUNK + 100] = ord("1")
    b = mkfile(tmp_path / "b.bin", bytes(big))
    assert quick_hash(a) == quick_hash(b)
    assert sha256(a) != sha256(b)


def test_sha256_known_string(tmp_path, mkfile):
    p = mkfile(tmp_path / "hello.txt", b"hello world")
    assert sha256(p) == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"


def test_sha256_matches_hashlib_on_multi_megabyte_file(tmp_path, mkfile):
    data = bytes(range(256)) * 8192  # 2 MiB, spans the 1 MiB read loop
    p = mkfile(tmp_path / "big.bin", data)
    assert sha256(p) == hashlib.sha256(data).hexdigest()
