"""ENF-SEC-002: safe_open_archive, the only archive entry point (streamed, capped)."""

from __future__ import annotations

import ast
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

from shelfhold.infrastructure.archives import (
    ArchiveLimits,
    UnsafeArchiveError,
    safe_open_archive,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"
ENTRY_POINTS = {
    "infrastructure/archives": {"zipfile", "tarfile", "py7zr", "rarfile"},
    "infrastructure/xml": {"xml", "lxml", "defusedxml"},
}


def _write(tmp_path: Path, data: bytes, name: str = "book.cbz") -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _reason(tmp_path: Path, data: bytes, limits: ArchiveLimits | None = None) -> str:
    path = _write(tmp_path, data)
    with (
        pytest.raises(UnsafeArchiveError) as caught,
        safe_open_archive(path, limits=limits or ArchiveLimits()) as archive,
    ):
        for entry in archive.entries:
            if not entry.is_dir:
                archive.read(entry.name)
    return caught.value.reason


@pytest.mark.req("ENF-SEC-002")
def test_safe_open_archive_is_only_entry_point() -> None:
    """No module outside the entry packages imports an archive or XML parsing library."""
    assert callable(safe_open_archive)
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom) and node.level == 0
                else []
            )
            for name in names:
                top = name.split(".")[0]
                for package, forbidden in ENTRY_POINTS.items():
                    if top in forbidden and not rel.startswith(package + "/"):
                        offenders.append(f"{rel} imports {name}")
    assert not offenders, offenders


@pytest.mark.req("ENF-SEC-002")
def test_zip_streamed_never_extracted_with_caps(
    malicious: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("an archive must never be extracted")

    monkeypatch.setattr(zipfile.ZipFile, "extract", refuse)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", refuse)
    path = _write(tmp_path, malicious.good_cbz())
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    with safe_open_archive(path) as archive:
        names = [e.name for e in archive.entries]
        assert names == ["001.png", "002.png", "003.png", "ComicInfo.xml"]
        with archive.open("001.png") as stream:
            assert stream.read(8) == malicious.PNG_MAGIC
        assert archive.read("ComicInfo.xml") == malicious.COMICINFO
        assert archive.read_head("002.png", 4) == malicious.PNG_MAGIC[:4]
    assert list(workdir.iterdir()) == [], "nothing may be written to disk"
    assert sorted(p.name for p in path.parent.iterdir()) == ["book.cbz", "work"]


@pytest.mark.req("ENF-SEC-002")
def test_zip_accepts_a_binary_stream_with_a_declared_name(malicious: ModuleType) -> None:
    import io

    with safe_open_archive(io.BytesIO(malicious.good_cbz()), name="x.cbz") as archive:
        assert len(archive.entries) == 4
    with pytest.raises(UnsafeArchiveError) as caught:
        safe_open_archive(io.BytesIO(malicious.good_cbz()), name="x.cbr")
    assert caught.value.reason == "magic_mismatch"


@pytest.mark.req("ENF-SEC-002")
def test_zip_ratio_cap_refuses_a_zip_bomb_before_reading(
    malicious: ModuleType, tmp_path: Path
) -> None:
    path = _write(tmp_path, malicious.zip_bomb(8 * 1024 * 1024))
    with pytest.raises(UnsafeArchiveError) as caught:
        safe_open_archive(path, limits=ArchiveLimits(max_entry_bytes=64 * 1024 * 1024))
    assert caught.value.reason == "ratio"


@pytest.mark.req("ENF-SEC-002")
def test_zip_entry_count_cap(malicious: ModuleType, tmp_path: Path) -> None:
    path = _write(tmp_path, malicious.many_entries(50))
    with pytest.raises(UnsafeArchiveError) as caught:
        safe_open_archive(path, limits=ArchiveLimits(max_entries=10))
    assert caught.value.reason == "too_many_entries"
    safe_open_archive(path, limits=ArchiveLimits(max_entries=50)).close()


@pytest.mark.req("ENF-SEC-002")
def test_zip_per_entry_size_cap(malicious: ModuleType, tmp_path: Path) -> None:
    data = malicious.zip_bytes([malicious.Member("a.bin", bytes(range(256)) * 400, deflate=False)])
    path = _write(tmp_path, data)
    with pytest.raises(UnsafeArchiveError) as caught:
        safe_open_archive(path, limits=ArchiveLimits(max_entry_bytes=50_000))
    assert caught.value.reason == "entry_too_large"


@pytest.mark.req("ENF-SEC-002")
def test_zip_total_size_cap_declared_and_cumulative(malicious: ModuleType, tmp_path: Path) -> None:
    data = malicious.zip_bytes(
        [malicious.Member(f"{i}.bin", bytes(range(256)) * 20, deflate=False) for i in range(4)]
    )
    path = _write(tmp_path, data)
    with pytest.raises(UnsafeArchiveError) as caught:
        safe_open_archive(path, limits=ArchiveLimits(max_total_bytes=10_000))
    assert caught.value.reason == "total_too_large"
    # the cap is also enforced on what is actually read, across every read of one opening
    limits = ArchiveLimits(max_total_bytes=4 * 5120, max_read_bytes=3 * 5120)
    with safe_open_archive(path, limits=limits) as archive:
        archive.read("0.bin")
        archive.read("1.bin")
        archive.read("2.bin")
        with pytest.raises(UnsafeArchiveError) as caught:
            archive.read("3.bin")
        assert caught.value.reason == "total_too_large"


@pytest.mark.req("ENF-SEC-002")
def test_zip_read_never_returns_more_than_asked(malicious: ModuleType, tmp_path: Path) -> None:
    data = malicious.zip_bytes([malicious.Member("a.bin", b"z" * 10_000, deflate=False)])
    with safe_open_archive(_write(tmp_path, data)) as archive:
        with pytest.raises(UnsafeArchiveError) as caught:
            archive.read("a.bin", max_bytes=100)
        assert caught.value.reason == "entry_too_large"
        assert len(archive.read_head("a.bin", 100)) == 100
        with pytest.raises(KeyError):
            archive.read("missing.bin")


@pytest.mark.req("ENF-SEC-002")
def test_zip_overlapping_entries_are_refused(malicious: ModuleType, tmp_path: Path) -> None:
    path = _write(tmp_path, malicious.overlapping_entries())
    with pytest.raises(UnsafeArchiveError) as caught:
        safe_open_archive(path)
    assert caught.value.reason == "overlap"


@pytest.mark.req("ENF-SEC-002")
@pytest.mark.parametrize(
    "key",
    [
        "dotdot", "nested_dotdot", "absolute", "backslash_dotdot", "windows_backslash",
        "drive_letter", "drive_absolute", "unc", "right_to_left_override",
        "zero_width", "bidi_isolate", "control_char",
    ],
)  # fmt: skip
def test_zip_unsafe_names_are_refused(malicious: ModuleType, tmp_path: Path, key: str) -> None:
    path = _write(tmp_path, malicious.traversal(malicious.TRAVERSAL_NAMES[key]))
    with pytest.raises(UnsafeArchiveError) as caught:
        safe_open_archive(path)
    assert caught.value.reason == "unsafe_path"


@pytest.mark.req("ENF-SEC-002")
def test_zip_encrypted_symlink_and_special_entries_are_refused(
    malicious: ModuleType, tmp_path: Path
) -> None:
    assert _reason(tmp_path, malicious.encrypted_entry()) == "encrypted"
    assert _reason(tmp_path, malicious.symlink_entry()) == "symlink"
    assert _reason(tmp_path, malicious.device_entry()) == "special_file"


@pytest.mark.req("ENF-SEC-002")
def test_zip_duplicate_names_are_refused_exact_case_and_unicode(
    malicious: ModuleType, tmp_path: Path
) -> None:
    for build in (
        malicious.duplicate_names,
        malicious.case_duplicate_names,
        malicious.unicode_duplicate_names,
    ):
        assert _reason(tmp_path, build()) == "duplicate_name", build.__name__


@pytest.mark.req("ENF-SEC-002")
def test_zip_only_stored_and_deflated_entries(malicious: ModuleType, tmp_path: Path) -> None:
    assert _reason(tmp_path, malicious.exotic_compression()) == "unsupported_compression"


@pytest.mark.req("ENF-SEC-002")
def test_archive_magic_bytes_must_match_the_extension(
    malicious: ModuleType, tmp_path: Path
) -> None:
    assert _reason(tmp_path, malicious.rar_bytes()) == "magic_mismatch"
    assert _reason(tmp_path, malicious.sevenzip_bytes()) == "magic_mismatch"
    assert _reason(tmp_path, malicious.zip_with_prefix()) == "magic_mismatch"
    assert _reason(tmp_path, b"") == "magic_mismatch"
    # a zip renamed to something that is not a zip container
    path = _write(tmp_path, malicious.good_cbz(), "book.rar")
    with pytest.raises(UnsafeArchiveError) as caught:
        safe_open_archive(path)
    assert caught.value.reason in {"magic_mismatch", "unsupported_format"}
    # a corrupt zip is a typed error, not a library one
    corrupt = malicious.good_cbz()[:-30]
    assert _reason(tmp_path, b"PK\x03\x04" + corrupt[4:]) == "corrupt"


@pytest.mark.req("ENF-SEC-002")
def test_archive_extension_aliases_are_zip_containers(
    malicious: ModuleType, tmp_path: Path
) -> None:
    for name in ("a.cbz", "a.zip", "a.epub", "A.CBZ"):
        with safe_open_archive(_write(tmp_path, malicious.good_cbz(), name)) as archive:
            assert archive.entries


@pytest.mark.req("ENF-SEC-002")
def test_directories_entries_are_listed_but_flagged(malicious: ModuleType, tmp_path: Path) -> None:
    data = malicious.zip_bytes([("pages/", b""), ("pages/001.png", malicious.tiny_png())])
    with safe_open_archive(_write(tmp_path, data)) as archive:
        assert [(e.name, e.is_dir) for e in archive.entries] == [
            ("pages/", True),
            ("pages/001.png", False),
        ]
