"""Regression tests for the security review of PR #29 (ENF-SEC-002, ENF-SEC-014)."""

from __future__ import annotations

import io
import time
import tracemalloc
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

from shelfhold.infrastructure.archives import ArchiveLimits, UnsafeArchiveError, safe_open_archive
from shelfhold.infrastructure.xml import UnsafeXmlError, XmlLimits, safe_parse_xml

BACKEND = Path(__file__).resolve().parents[2]


def _refuse(data: bytes, limits: ArchiveLimits | None = None) -> UnsafeArchiveError:
    with pytest.raises(UnsafeArchiveError) as caught:
        safe_open_archive(io.BytesIO(data), name="x.cbz", limits=limits or ArchiveLimits())
    return caught.value


@pytest.mark.req("ENF-SEC-002")
def test_entry_cap_is_applied_before_zipfile_loads_the_directory(
    malicious: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("ZipFile must not be reached")

    monkeypatch.setattr(zipfile, "ZipFile", boom)
    limits = ArchiveLimits(max_entries=100)
    plain = malicious.many_central_records(20_000)  # ~1 MiB of directory
    zip64 = malicious.zip64_claiming(3_000_000, 3_000_000 * 50)
    for data in (plain, zip64):
        tracemalloc.start()
        started = time.monotonic()
        error = _refuse(data, limits)
        elapsed = time.monotonic() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert error.reason == "too_many_entries"
        assert elapsed < 0.5
        assert peak < 1024 * 1024, f"{peak} bytes allocated by the refusal"


@pytest.mark.req("ENF-SEC-002")
def test_directory_size_cap_refuses_a_huge_directory_with_few_declared_entries(
    malicious: ModuleType,
) -> None:
    data = malicious.zip64_claiming(10, 10**12)
    assert _refuse(data).reason == "too_many_entries"


@pytest.mark.req("ENF-SEC-002")
def test_zip_hidden_local_entries_are_refused(malicious: ModuleType) -> None:
    assert _refuse(malicious.hidden_local_entry()).reason == "hidden_data"
    assert _refuse(malicious.trailing_hidden_data()).reason == "hidden_data"


@pytest.mark.req("ENF-SEC-002")
def test_zip_nul_in_a_stored_name_is_refused(malicious: ModuleType) -> None:
    error = _refuse(malicious.nul_in_name())
    assert error.reason == "unsafe_path"


@pytest.mark.req("ENF-SEC-002")
def test_zip_directory_entries_must_be_empty_directories(malicious: ModuleType) -> None:
    assert _refuse(malicious.directory_with_data()).reason == "unsafe_directory"


@pytest.mark.req("ENF-SEC-002")
def test_zip_tiling_accepts_what_zipfile_writes(malicious: ModuleType) -> None:
    for data in (malicious.good_cbz(), malicious.zip_bytes([("pages/", b""), ("pages/a", b"x")])):
        safe_open_archive(io.BytesIO(data), name="x.cbz").close()
    safe_open_archive(io.BytesIO(malicious.zip_bytes([])), name="x.cbz").close()


@pytest.mark.req("ENF-SEC-002")
def test_zip_read_default_cap_is_32_mib_and_returns_one_buffer(malicious: ModuleType) -> None:
    assert ArchiveLimits().max_buffered_bytes == 32 * 1024 * 1024
    data = malicious.zip_bytes([malicious.Member("a.bin", b"q" * 4096, deflate=False)])
    with safe_open_archive(io.BytesIO(data), name="x.zip") as archive:
        assert archive.read("a.bin") == b"q" * 4096
    tight = ArchiveLimits(max_buffered_bytes=1000)
    with safe_open_archive(io.BytesIO(data), name="x.zip", limits=tight) as archive:
        with pytest.raises(UnsafeArchiveError) as caught:
            archive.read("a.bin")
        assert caught.value.reason == "entry_too_large"
        with archive.open("a.bin") as stream:  # streaming keeps its own cap
            assert len(stream.read()) == 4096


@pytest.mark.req("ENF-SEC-014")
def test_xml_undecodable_declared_encoding_is_a_typed_error(malicious: ModuleType) -> None:
    with pytest.raises(UnsafeXmlError) as caught:
        safe_parse_xml(malicious.utf7_xml())
    assert caught.value.reason == "encoding"
    with pytest.raises(UnsafeXmlError) as caught:
        safe_parse_xml(b'<?xml version="1.0" encoding="no-such-codec"?><a/>')
    assert caught.value.reason == "encoding"


@pytest.mark.req("ENF-SEC-014")
def test_xml_attribute_and_element_caps(malicious: ModuleType) -> None:
    assert XmlLimits().max_attributes == 256
    assert XmlLimits().max_elements == 100_000
    with pytest.raises(UnsafeXmlError) as caught:
        safe_parse_xml(malicious.many_attributes(257))
    assert caught.value.reason == "too_many_attributes"
    safe_parse_xml(malicious.many_attributes(256))
    with pytest.raises(UnsafeXmlError) as caught:
        safe_parse_xml(malicious.wide(100_001))
    assert caught.value.reason == "too_many_elements"


@pytest.mark.req("ENF-SEC-002", "ENF-SEC-014")
def test_bypass_modules_and_dynamic_imports_are_banned() -> None:
    import configparser
    import tomllib

    ini = configparser.ConfigParser()
    ini.read(BACKEND / "importlinter.ini")
    forbidden = ini["importlinter:contract:xml-single-entry"]["forbidden_modules"].split()
    assert {"pyexpat", "plistlib", "xmlrpc"} <= set(forbidden)
    ruff = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["ruff"]
    assert "TID" in ruff["lint"]["select"]
    banned = ruff["lint"]["flake8-tidy-imports"]["banned-api"]
    assert {"shutil.unpack_archive", "importlib.import_module", "builtins.__import__"} <= set(
        banned
    )
