import os

import pytest

from cubiczan_resilience import atomic_write
from cubiczan_resilience import atomic as atomic_mod


def test_writes_str_and_bytes(tmp_path):
    p = tmp_path / "a.txt"
    atomic_write(p, "hello")
    assert p.read_text() == "hello"

    b = tmp_path / "b.bin"
    atomic_write(b, b"\x00\x01\x02")
    assert b.read_bytes() == b"\x00\x01\x02"


def test_overwrite_replaces_atomically(tmp_path):
    p = tmp_path / "c.txt"
    atomic_write(p, "v1")
    atomic_write(p, "v2")
    assert p.read_text() == "v2"


def test_creates_parent_dirs(tmp_path):
    p = tmp_path / "nested" / "deep" / "f.txt"
    atomic_write(p, "x")
    assert p.read_text() == "x"


def test_mode_applied(tmp_path):
    p = tmp_path / "secret.txt"
    atomic_write(p, "s", mode=0o600)
    assert (os.stat(p).st_mode & 0o777) == 0o600


def test_no_partial_file_on_crash(tmp_path, monkeypatch):
    """Simulate a crash mid-write: destination must be untouched, no temp left."""
    p = tmp_path / "money.txt"
    atomic_write(p, "ORIGINAL")

    # Force os.replace to blow up as if the process died right before rename.
    def boom(src, dst):
        raise RuntimeError("simulated crash before replace")

    monkeypatch.setattr(atomic_mod.os, "replace", boom)

    with pytest.raises(RuntimeError, match="simulated crash"):
        atomic_write(p, "CORRUPT-HALF-WRITE")

    # Original content intact, never a partial overwrite.
    assert p.read_text() == "ORIGINAL"
    # No orphan temp files left behind.
    leftovers = [f for f in os.listdir(tmp_path) if f != "money.txt"]
    assert leftovers == [], f"temp files leaked: {leftovers}"
