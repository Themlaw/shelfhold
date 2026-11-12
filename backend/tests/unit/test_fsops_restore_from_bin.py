"""`Fsops.recycle(entry=...)` and `Fsops.restore_from_bin` (L25, EF-BIB-021): the bin entry named
beforehand, and the file put back where it was, never over anything and never out of its root."""

from pathlib import Path

import pytest

from shelfhold.infrastructure.fsops.errors import OutsidePathMapError
from tests.library_env import lib_env

pytestmark = pytest.mark.req("EF-BIB-021")
ENTRY = "0123456789abcdef0123456789abcdef"


def test_recycle_into_the_named_entry_and_restore_the_same_inode() -> None:
    with lib_env() as lib:
        target = lib.manga / "Series" / "one.cbz"
        lib.fsops.write_atomic(str(target), b"bytes", parents=True)
        inode = target.stat().st_ino
        moved = lib.fsops.recycle(str(target), entry=ENTRY)
        assert (
            Path(moved) == lib.library / ".shelfhold" / "recycle" / ENTRY / "manga/Series/one.cbz"
        )
        assert not target.exists()
        lib.fsops.restore_from_bin(str(target), f"{ENTRY}/manga/Series/one.cbz")
        assert target.read_bytes() == b"bytes" and target.stat().st_ino == inode


@pytest.mark.parametrize(
    "entry", ["", "../x", "ABCDEF0123456789ABCDEF0123456789", "abc", ENTRY + "0"]
)
def test_recycle_refuses_a_bin_entry_that_is_not_32_hex_digits(entry: str) -> None:
    with lib_env() as lib:
        target = lib.manga / "one.cbz"
        lib.fsops.write_atomic(str(target), b"bytes")
        with pytest.raises(ValueError, match="hexadecimal"):
            lib.fsops.recycle(str(target), entry=entry)
        assert target.exists()


def test_restore_never_replaces_a_file_nor_leaves_the_root() -> None:
    with lib_env() as lib:
        target = lib.manga / "one.cbz"
        lib.fsops.write_atomic(str(target), b"bytes")
        lib.fsops.recycle(str(target), entry=ENTRY)
        target.write_bytes(b"another file")
        with pytest.raises(FileExistsError):
            lib.fsops.restore_from_bin(str(target), f"{ENTRY}/manga/one.cbz")
        assert target.read_bytes() == b"another file"  # untouched
        target.unlink()
        for bad in ("../../x", "/etc/passwd", "", f"{ENTRY}/../../../x"):
            with pytest.raises(OutsidePathMapError):
                lib.fsops.restore_from_bin(str(target), bad)
        with pytest.raises(FileNotFoundError):
            lib.fsops.restore_from_bin(str(target), f"{ENTRY}/manga/missing.cbz")
        assert not target.exists()
