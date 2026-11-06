"""CBZ packaging (EF-EXP-040 order, EF-EXP-063 provenance, ENF-SEC-002)."""

import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.infrastructure.content_trust import check_cbz_entries
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.packaging.cbz import repackage_cbz, write_cbz
from shelfhold.infrastructure.packaging.comicinfo import ComicInfoData, render_comicinfo

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
COMICINFO = render_comicinfo(ComicInfoData(series="Dr Stone", number="1")).xml


def source_cbz(*, with_old_comicinfo: bool = True) -> io.BytesIO:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        if with_old_comicinfo:  # first in the source, e.g. written by the upstream tool
            zf.writestr("ComicInfo.xml", b"<ComicInfo><Series>Old</Series></ComicInfo>")
        zf.writestr("002.jpg", JPEG)
        zf.writestr("001.png", PNG)
    buffer.seek(0)
    return buffer


@pytest.mark.req("EF-EXP-040", "EF-EXP-011")
def test_comicinfo_written_as_last_zip_entry() -> None:
    out = io.BytesIO()
    with safe_open_archive(source_cbz(), name="x.cbz") as src:
        repackage_cbz(src, COMICINFO, out)
    out.seek(0)
    with safe_open_archive(out, name="out.cbz") as result:
        names = [e.name for e in result.entries]
        assert names == ["001.png", "002.jpg", "ComicInfo.xml"] or names == [
            "002.jpg",
            "001.png",
            "ComicInfo.xml",
        ]
        assert names[-1] == "ComicInfo.xml" and names.count("ComicInfo.xml") == 1
        assert bytes(result.read("ComicInfo.xml")) == COMICINFO
        listing = check_cbz_entries(result)  # the output passes the same allowlist
        assert listing.comicinfo == "ComicInfo.xml" and len(listing.images) == 2


@pytest.mark.req("EF-EXP-040")
def test_write_cbz_from_pages_keeps_page_order_and_ends_with_comicinfo() -> None:
    out = io.BytesIO()
    write_cbz([("p1.png", PNG), ("p2.jpg", JPEG)], COMICINFO, out)
    out.seek(0)
    with zipfile.ZipFile(out) as zf:
        assert zf.namelist() == ["p1.png", "p2.jpg", "ComicInfo.xml"]


@pytest.mark.req("EF-EXP-063", "ENF-QUAL-004")
def test_exported_file_carries_no_shelfhold_identity() -> None:
    out = io.BytesIO()
    with safe_open_archive(source_cbz(), name="x.cbz") as src:
        repackage_cbz(src, COMICINFO, out)
    raw = out.getvalue()
    assert b"shelfhold" not in raw.lower()
    assert b"rayonnage" not in raw.lower()
    out.seek(0)
    with zipfile.ZipFile(out) as zf:
        assert zf.comment == b""
        for info in zf.infolist():
            assert info.comment == b"" and info.extra == b""
            assert info.date_time == (1980, 1, 1, 0, 0, 0)  # no clock, no host
            assert info.create_system == 3 and info.external_attr == 0o644 << 16


@pytest.mark.req("ENF-QUAL-004")
def test_repackaging_is_byte_deterministic() -> None:
    first, second = io.BytesIO(), io.BytesIO()
    for out in (first, second):
        with safe_open_archive(source_cbz(), name="x.cbz") as src:
            repackage_cbz(src, COMICINFO, out)
    assert first.getvalue() == second.getvalue()


@pytest.mark.req("ENF-SEC-015")
def test_untrusted_entries_are_refused_before_anything_is_written() -> None:
    from shelfhold.infrastructure.content_trust import UntrustedEntryError

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("001.png", PNG)
        zf.writestr("x.svg", b"<svg onload='x()'/>")
    buffer.seek(0)
    out = io.BytesIO()
    with safe_open_archive(buffer, name="x.cbz") as src, pytest.raises(UntrustedEntryError):
        repackage_cbz(src, COMICINFO, out)
    assert out.getvalue() == b""


# --- ENF-QUAL-004: the golden of a file written by the real export pipeline ---

GOLDEN_CBZ = Path(__file__).parents[1] / "golden" / "cbz"


def _describe(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        entries = [
            {
                "name": i.filename,
                "date_time": list(i.date_time),
                "compress_type": i.compress_type,
                "file_size": i.file_size,
                "crc": f"{i.CRC:08x}",
                "create_system": i.create_system,
                "external_attr": i.external_attr,
                "comment": i.comment.decode(),
                "extra": i.extra.hex(),
            }
            for i in zf.infolist()
        ]
        return {
            "entries": entries,
            "archive_comment": zf.comment.decode(),
            "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "comicinfo": zf.read("ComicInfo.xml").decode("utf-8"),
        }


@pytest.mark.req("ENF-QUAL-004", "EF-EXP-040", "EF-EXP-063")
def test_cbz_golden_entries_order_dates_compression_hash() -> None:
    """A chapter exported through the pipeline (received archive, render, fsops rename): the file
    on the shelf is compared with the committed golden, byte for byte through its hash."""
    from tests.comicinfo_xsd import kavita_schema
    from tests.export_env import export_env

    with export_env() as env:
        series = env.new_series()
        env.add_chapter(series, "2")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        (exported,) = env.files()
        actual = _describe(exported)
    assert exported.name == "Dr Stone - Ch.2.cbz"
    kavita_schema().validate(str(actual["comicinfo"]))
    entries = actual["entries"]
    assert isinstance(entries, list)
    names = [e["name"] for e in entries]
    assert names[-1] == "ComicInfo.xml" and names.count("ComicInfo.xml") == 1  # last, once
    assert names[:-1] == sorted(names[:-1])  # the page order of the received archive
    assert {tuple(e["date_time"]) for e in entries} == {(1980, 1, 1, 0, 0, 0)}
    assert [e["compress_type"] for e in entries] == [zipfile.ZIP_STORED] * (len(entries) - 1) + [
        zipfile.ZIP_DEFLATED
    ]  # pages are images (stored), only the XML is compressed
    assert actual["archive_comment"] == ""
    target = GOLDEN_CBZ / "exported_chapter.json"
    text = json.dumps(actual, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if os.environ.get("SHELFHOLD_UPDATE_GOLDEN") == "1":
        target.write_text(text, encoding="utf-8")
    assert target.read_text(encoding="utf-8") == text
