"""M4 L49a: the layout of the block of a series, a stored ZIP that is the same bytes every time
(ADR-0053 §2, §3; EF-INT-007, EF-LEC-009, ENF-QUAL-005).

The layout is pure: it knows names, sizes and CRC-32 of the entries and produces the headers; the
bytes of the pages come from elsewhere. Everything below is read back with the standard library,
and the KOReader digest of a layout is compared with the digest of the same bytes materialised.
"""

from __future__ import annotations

import io
import struct
import zipfile
import zlib
from collections.abc import Sequence

import pytest

from shelfhold.reading.domain.bundle_zip import (
    MAX_PART_BYTES,
    ZIP64_ENTRIES,
    BundleZip,
    LayoutStream,
    PartTooLargeError,
    UnsafeMemberNameError,
    ZipEntry,
    chapter_folder,
    check_member_name,
    page_name,
)
from shelfhold.reading.domain.document_hash import koreader_partial_md5

pytestmark = pytest.mark.req("EF-INT-007", "EF-LEC-009", "ENF-QUAL-005")

IDENTITY = bytes(range(32))


def entries_of(pages: Sequence[tuple[str, bytes]]) -> tuple[list[ZipEntry], list[bytes]]:
    return (
        [ZipEntry(name, len(data), zlib.crc32(data)) for name, data in pages],
        [data for _, data in pages],
    )


def materialise(block: BundleZip, payloads: Sequence[bytes]) -> bytes:
    out = io.BytesIO()
    for piece in block.pieces():
        out.write(payloads[piece] if isinstance(piece, int) else piece)
    return out.getvalue()


PAGES = [
    ("00001 ch1/00001.png", b"\x89PNG one" * 20),
    ("00001 ch1/00002.jpg", b"\xff\xd8 two" * 30),
    ("00002 ch2/00001.webp", b"RIFFxxxxWEBP three" * 10),
    ("00002 ch2/00002.gif", b"GIF89a four" * 5),
]


def test_the_block_opens_with_the_standard_library_as_a_stored_zip_in_page_order() -> None:
    entries, payloads = entries_of(PAGES)
    raw = materialise(BundleZip(entries, IDENTITY), payloads)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        assert [i.filename for i in archive.infolist()] == [n for n, _ in PAGES]
        assert {i.compress_type for i in archive.infolist()} == {zipfile.ZIP_STORED}
        assert archive.testzip() is None
        for name, data in PAGES:
            assert archive.read(name) == data


def test_the_declared_size_is_the_exact_size_of_the_bytes() -> None:
    entries, payloads = entries_of(PAGES)
    block = BundleZip(entries, IDENTITY)
    assert block.size == len(materialise(block, payloads))


def test_two_layouts_of_the_same_members_are_the_same_bytes() -> None:
    entries, payloads = entries_of(PAGES)
    first = materialise(BundleZip(entries, IDENTITY), payloads)
    second = materialise(BundleZip(list(entries), bytes(IDENTITY)), list(payloads))
    assert first == second


def test_every_variable_field_of_the_zip_is_fixed() -> None:
    entries, payloads = entries_of(PAGES)
    raw = materialise(BundleZip(entries, IDENTITY), payloads)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        for info in archive.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.external_attr == 0
            assert info.comment == b""
            assert info.flag_bits == 0
        assert archive.comment == b""


def test_the_identity_sits_in_the_first_kibibyte_and_is_what_tells_two_versions_apart() -> None:
    entries, payloads = entries_of(PAGES)
    one = materialise(BundleZip(entries, IDENTITY), payloads)
    two = materialise(BundleZip(entries, bytes(reversed(IDENTITY))), payloads)
    assert IDENTITY in one[:1024]
    assert one[:1024] != two[:1024]
    assert koreader_partial_md5(io.BytesIO(one)) != koreader_partial_md5(io.BytesIO(two))


def test_the_koreader_digest_is_computed_without_materialising_the_zip() -> None:
    big = [(f"{n:05d} ch{n}/00001.png", bytes([n % 251]) * (300_000 + n)) for n in range(1, 40)]
    entries, payloads = entries_of(big)
    block = BundleZip(entries, IDENTITY)
    raw = materialise(block, payloads)
    reads: list[tuple[int, int, int]] = []

    def read_entry(index: int, start: int, length: int) -> bytes:
        reads.append((index, start, length))
        return payloads[index][start : start + length]

    streamed = koreader_partial_md5(LayoutStream(block, read_entry))
    assert streamed == koreader_partial_md5(io.BytesIO(raw))
    assert sum(length for _, _, length in reads) <= 12 * 1024  # only the samples were read


def test_a_range_is_what_the_whole_stream_holds_at_that_place() -> None:
    entries, payloads = entries_of(PAGES)
    block = BundleZip(entries, IDENTITY)
    raw = materialise(block, payloads)
    for offset, length in [(0, 10), (5, 300), (len(raw) - 50, 50), (100, 0), (len(raw), 10)]:
        got = block.read_range(offset, length, lambda i, s, n: payloads[i][s : s + n])
        assert got == raw[offset : offset + length]


def test_zip64_begins_above_65535_entries_and_not_before() -> None:
    def build(count: int) -> tuple[BundleZip, bytes]:
        pages = [(f"{(n // 500) + 1:05d} ch1/{n % 500 + 1:05d}.png", b"x") for n in range(count)]
        entries, payloads = entries_of(pages)
        block = BundleZip(entries, IDENTITY)
        return block, materialise(block, payloads)

    below, raw_below = build(ZIP64_ENTRIES)
    above, raw_above = build(ZIP64_ENTRIES + 1)
    assert not below.zip64
    assert above.zip64
    assert b"PK\x06\x06" not in raw_below[-200:]
    assert b"PK\x06\x06" in raw_above[-200:]
    for raw, count in ((raw_below, ZIP64_ENTRIES), (raw_above, ZIP64_ENTRIES + 1)):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            assert len(archive.infolist()) == count
            assert archive.read(archive.infolist()[-1].filename) == b"x"


def test_a_part_stays_strictly_under_two_gibibytes() -> None:
    assert MAX_PART_BYTES == 2**31 - 1
    last = MAX_PART_BYTES - 200
    BundleZip([ZipEntry("00001 ch1/00001.png", 1000, 0)], IDENTITY)  # small is fine
    with pytest.raises(PartTooLargeError):
        BundleZip([ZipEntry("00001 ch1/00001.png", last + 10_000, 0)], IDENTITY)


@pytest.mark.parametrize(
    "name",
    [
        "../x.png", "00001 ch1/../../etc/passwd", "/abs/00001.png", "C:/x/00001.png",
        "00001 ch1\\00001.png", "00001 ch1/00001.png\x00", "", "00001 ch1/é.png",
        "00001 ch1//00001.png", "./00001.png", "00001 ch1/", "a" * 300 + "/1.png",
    ],
)  # fmt: skip
def test_no_path_of_the_disk_and_no_climbing_name_passes(name: str) -> None:
    with pytest.raises(UnsafeMemberNameError):
        check_member_name(name)
    with pytest.raises(UnsafeMemberNameError):
        BundleZip([ZipEntry(name, 1, 0)], IDENTITY)


def test_a_normal_member_name_passes() -> None:
    assert check_member_name("00012 ch7.5/00003.webp") == "00012 ch7.5/00003.webp"


def test_chapter_folders_are_ranked_ascii_and_never_a_title_or_a_path() -> None:
    assert chapter_folder(3, "12") == "00003 ch12"
    assert chapter_folder(1, "7.5") == "00001 ch7.5"
    cleaned = chapter_folder(2, "../../é ch/ap\\ter")
    assert cleaned.startswith("00002 ch")
    check_member_name(cleaned + "/00001.png")
    assert ".." not in cleaned and "/" not in cleaned and "\\" not in cleaned
    assert len(chapter_folder(2, "9" * 100)) <= len("00002 ch") + 24
    assert chapter_folder(2, "") == "00002 ch"  # no number: the rank alone, still a valid name


def test_page_names_are_five_digits_with_a_lowercase_extension() -> None:
    assert page_name(7, "PNG") == "00007.png"
    assert page_name(12345, "jpeg") == "12345.jpeg"
    with pytest.raises(UnsafeMemberNameError):
        page_name(1, "p/ng")


def test_entries_are_in_rank_order_which_is_the_natural_order_of_the_names() -> None:
    names = [
        chapter_folder(n, str(n)) + "/" + page_name(p, "png") for n in (1, 2, 10) for p in (1, 2)
    ]
    assert names == sorted(names)


def test_a_crc_or_a_size_that_does_not_fit_a_32_bit_field_is_refused() -> None:
    with pytest.raises(ValueError):
        ZipEntry("00001 ch1/00001.png", 1, 2**32)
    with pytest.raises(ValueError):
        ZipEntry("00001 ch1/00001.png", -1, 0)


def test_the_local_header_of_the_first_entry_carries_the_private_field() -> None:
    entries, payloads = entries_of(PAGES)
    raw = materialise(BundleZip(entries, IDENTITY), payloads)
    signature, _v, flags, method, _t, _d, _crc, _cs, _us, name_length, extra_length = struct.unpack(
        "<4sHHHHHIIIHH", raw[:30]
    )
    assert (signature, flags, method) == (b"PK\x03\x04", 0, 0)
    extra = raw[30 + name_length : 30 + name_length + extra_length]
    assert extra[4:] == IDENTITY
    second = raw.index(b"PK\x03\x04", 4)
    assert struct.unpack("<H", raw[second + 28 : second + 30])[0] == 0  # the others have none
