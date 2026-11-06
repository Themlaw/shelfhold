"""The access counter of the window tests sees every way a file is reached (ENF-OPS-014)."""

import os
from pathlib import Path

import pytest

from shelfhold.infrastructure.fsops.openat2 import openat2
from tests.fs_spy import FsSpy

pytestmark = pytest.mark.req("ENF-OPS-014")


def test_every_way_to_reach_a_file_is_counted(tmp_path: Path) -> None:
    (tmp_path / "f").write_bytes(b"x")
    elsewhere = tmp_path.parent / "elsewhere-not-watched"
    elsewhere.mkdir(exist_ok=True)
    spy = FsSpy([tmp_path])
    reads = {
        "Path.read_bytes": lambda: (tmp_path / "f").read_bytes(),
        "builtins.open": lambda: open(tmp_path / "f", "rb").close(),  # noqa: SIM115
        "os.stat": lambda: os.stat(tmp_path / "f"),
        "os.listdir": lambda: os.listdir(tmp_path),
    }
    for name, read in reads.items():
        before = len(spy.accesses)
        with spy.watching():
            read()
        assert len(spy.accesses) > before, name
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        before = len(spy.accesses)
        with spy.watching():
            os.close(openat2(dir_fd, "f", os.O_RDONLY))  # ctypes, relative to a descriptor
        assert len(spy.accesses) > before
    finally:
        os.close(dir_fd)
    with spy.watching():
        os.stat(elsewhere)
    assert not any("elsewhere" in a for a in spy.accesses)
