"""EF-BIB-012 / ENF-SEC-002: the integrity check reads files nobody vouches for.

Every trap an archive can carry is refused as a failed check, never raised, never extracted, and
the file is opened only through the archive guard (`safe_open_archive`).
"""

import struct
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from shelfhold.jobs.integrity_job import window_guard
from shelfhold.jobs.public import DEFAULT_WINDOW, OutsideMaintenanceWindowError
from shelfhold.library.application import integrity
from shelfhold.library.application.integrity import FileVerdict, check_cbz
from tests.integrity_env import zip_with
from tests.library_env import JPEG, PNG, cbz_bytes

pytestmark = pytest.mark.req("EF-BIB-012")

PAGES = (("001.png", PNG), ("002.jpg", JPEG))


def _write(tmp_path: Path, data: bytes) -> Path:
    folder = tmp_path / "untrusted"
    folder.mkdir()
    path = folder / "x.cbz"
    path.write_bytes(data)
    return path


def _forged_crc() -> bytes:
    data = bytearray(cbz_bytes(PAGES))
    for signature, offset in ((b"PK\x03\x04", 14), (b"PK\x01\x02", 16)):
        start = 0
        while (at := data.find(signature, start)) != -1:
            struct.pack_into("<I", data, at + offset, 0xDEADBEEF)
            start = at + 4
    return bytes(data)


def _bomb() -> bytes:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("001.png", PNG + b"\x00" * (16 * 1024 * 1024))
    return buffer.getvalue()


TRAPS = {
    "dotdot": (zip_with([("../../evil.png", PNG), ("001.png", PNG)]), "unsafe_archive"),
    "absolute": (zip_with([("/etc/evil.png", PNG)]), "unsafe_archive"),
    "symlink": (zip_with([("001.png", PNG)], symlink="002.png"), "unsafe_archive"),
    "bomb": (_bomb(), "unsafe_archive"),
    "truncated": (cbz_bytes(PAGES)[:-60], "unsafe_archive"),
    "forged_crc": (_forged_crc(), "crc"),
    "not_a_zip": (b"<html>not an archive at all</html>" * 4, "unsafe_archive"),
    "script_page": (zip_with([("001.png", b"<script>alert(1)</script>")]), "unsafe_archive"),
    "too_many_entries": (
        zip_with([(f"{n:05d}.png", PNG) for n in range(2100)]),
        "unsafe_archive",
    ),
}


@pytest.mark.parametrize("trap", sorted(TRAPS))
def test_hostile_archive_fails_the_check_without_raising_or_extracting(
    tmp_path: Path, trap: str
) -> None:
    data, reason = TRAPS[trap]
    path = _write(tmp_path, data)
    verdict = check_cbz(path, expected_pages=None, expected_hash=None)
    assert verdict == FileVerdict(False, reason), trap
    assert sorted(p.name for p in tmp_path.rglob("*")) == ["untrusted", "x.cbz"]  # no extraction


def test_a_good_archive_passes(tmp_path: Path) -> None:
    path = _write(tmp_path, cbz_bytes(PAGES))
    assert check_cbz(path, expected_pages=2, expected_hash=None) == FileVerdict(True, None)


def test_page_count_and_hash_are_compared(tmp_path: Path) -> None:
    path = _write(tmp_path, cbz_bytes(PAGES))
    assert check_cbz(path, expected_pages=3, expected_hash=None).reason == "page_count"
    assert check_cbz(path, expected_pages=2, expected_hash="sha256:" + "0" * 64).reason == "hash"


def test_the_file_is_opened_only_through_the_archive_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, cbz_bytes(PAGES))
    seen: list[object] = []
    real = integrity.safe_open_archive

    def spy(source: object, **kwargs: object) -> object:
        seen.append(source)
        return real(source, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(integrity, "safe_open_archive", spy)
    check_cbz(path, expected_pages=2, expected_hash=None)
    assert seen == [path]


def test_no_check_outside_the_maintenance_window() -> None:
    inside = datetime(2026, 11, 12, 3, 30, tzinfo=UTC)  # 04:30 in Paris
    noon = datetime(2026, 11, 12, 11, 0, tzinfo=UTC)
    window_guard(lambda: inside, DEFAULT_WINDOW)()  # does not raise
    with pytest.raises(OutsideMaintenanceWindowError):
        window_guard(lambda: noon, DEFAULT_WINDOW)()
