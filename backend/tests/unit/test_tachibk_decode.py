"""M4 L26 (EF-INT-008, ENF-SEC-002): a `.tachibk` is read as a hostile file.

The decoder is written by hand (gzip, then protobuf): every bound is a number the test can break,
and a file over any of them is refused whole, with a closed word, never cut and never trusted.
Corpus: synthetic only (`tests/tachibk_env.py`).
"""

from __future__ import annotations

import gzip
import time

import pytest

from shelfhold.platform.import_tachibk.decode import (
    SCHEMA_VERSIONS,
    Limits,
    TachibkRefusedError,
    decode_backup,
)
from shelfhold.platform.import_tachibk.map import map_backup
from tests import tachibk_env as env


def _gz(raw: bytes) -> bytes:
    return gzip.compress(raw, mtime=0)  # the same bytes on every run: stable test ids


def _refused(data: bytes, limits: Limits | None = None) -> TachibkRefusedError:
    with pytest.raises(TachibkRefusedError) as caught:
        decode_backup(data, limits or Limits())
    return caught.value


@pytest.mark.req("EF-INT-008")
def test_a_backup_is_decoded_and_the_fields_it_does_not_know_are_ignored() -> None:
    raw = decode_backup(env.sample())
    assert raw.schema_version in SCHEMA_VERSIONS
    assert [m.title for m in raw.manga] == ["Orchard Tales", "Harbor Notes"]
    first = raw.manga[0]
    assert (first.source, first.url) == (env.SOURCE_ID, "/series/orchard-tales")
    assert [(c.url, c.read, c.last_page) for c in first.chapters][:3] == [
        ("/c/1", True, 0), ("/c/2", True, 0), ("/c/3", False, 7),
    ]  # fmt: skip
    assert [t.remote_id for t in first.tracking] == [7_001]
    assert raw.source_ids == [env.SOURCE_ID, env.OTHER_SOURCE_ID]


@pytest.mark.req("EF-INT-008", "ENF-SEC-002")
def test_tachibk_bomb_and_depth_caps_refused() -> None:
    small = Limits(max_decompressed=1 << 20, max_compressed=1 << 20)
    # a gzip bomb: a few kilobytes that would expand to 64 MiB and more
    bomb = env.bomb(70)
    assert len(bomb) < 200_000
    assert _refused(bomb).reason == "decompressed_too_large"
    assert _refused(env.bomb(2), small).reason == "decompressed_too_large"
    # a compressed file over its own bound, before any decompression
    assert _refused(b"\x1f\x8b" + bytes(2048), Limits(max_compressed=1024)).reason == "too_large"
    # a gzip bomb that hides in a second member is no bomb: a second member is refused
    two = env.tachibk(env.backup([])) + env.tachibk(env.backup([]))
    assert _refused(two).reason == "malformed"
    # a start-group tag nested 17 times: refused, not recursed into
    forged = env.tachibk(env.backup([env.manga(1, "/u", "T", extra=env.nested_groups(40))]))
    assert _refused(forged).reason == "too_deep"
    # a message inside a message inside a message: only the fields the schema names are entered,
    # so the rest is skipped in constant stack, whatever the nesting
    deep = env.tachibk(env.nested_messages(5_000))
    started = time.monotonic()
    outcome = None
    try:
        outcome = decode_backup(deep)
    except TachibkRefusedError as refused:
        outcome = refused
    assert time.monotonic() - started < 5 and outcome is not None
    # more entries than the bound: the messages read (here, 100 001 chapters) are the entries
    many = env.manga(1, "/u", "T", chapters=[b""] * 100_001)
    assert _refused(env.tachibk(env.backup([many]))).reason == "too_many_entries"
    # a string of enormous declared length, truncated file: malformed, never allocated
    truncated = gzip.compress(env.tag(1, 2) + env.varint(2**40) + b"abc")
    assert _refused(truncated).reason == "malformed"


@pytest.mark.req("EF-INT-008", "ENF-SEC-002")
@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"", "empty"),
        (b"PK\x03\x04 not a gzip file at all", "not_gzip"),
        (b'{"version": 2}', "not_gzip"),  # the legacy JSON backup is no gzip either
        (_gz(b""), "empty"),
        (_gz(b"\x0a"), "malformed"),  # a tag with nothing after it
        (_gz(env.tag(1, 2) + env.varint(10) + b"abc"), "malformed"),  # cut short
        (_gz(b"\xff" * 11), "malformed"),  # a varint that never ends
        (_gz(env.tag(1, 0) + env.varint(1)), "malformed"),  # manga must be a message
        (
            _gz(env.tag(0, 0) + env.varint(1)),
            "malformed",
        ),  # field number 0 does not exist
        (_gz(env.tag(1, 7)), "malformed"),  # a wire type that does not exist
    ],
)
def test_a_file_that_is_not_a_backup_is_refused_with_a_word(data: bytes, reason: str) -> None:
    assert _refused(data).reason == reason


@pytest.mark.req("EF-INT-008")
def test_tachibk_unknown_schema_version_refused_with_code() -> None:
    # a file with no top-level field this build knows is no backup it recognises: refused by name
    future = env.tachibk(env.ld(999, "a later field") + env.ld(998, b""))
    refused = _refused(future)
    assert (refused.code, refused.reason) == ("import.tachibk_schema", "unknown_schema")
    assert refused.code != _refused(b"").code
    # every recognised generation is accepted
    for extra in (b"", env.ld(104, "p"), env.ld(106, "r")):
        raw = decode_backup(env.tachibk(env.backup([env.manga(1, "/u", "T")], extra=extra)))
        assert raw.schema_version in SCHEMA_VERSIONS and raw.unknown_fields == []


@pytest.mark.req("EF-INT-008")
def test_a_later_field_is_ignored_and_reported_and_a_long_text_costs_only_its_series() -> None:
    # the forks add their own top-level fields (600, 601) and Mihon adds more: skipped, reported
    later = env.ld(600, env.ld(1, "a saved search")) + env.ld(601, env.vi(1, 1)) + env.ld(107, b"")
    raw = decode_backup(env.tachibk(env.backup([env.manga(1, "/u", "T")], extra=later)))
    assert (raw.schema_version, raw.unknown_fields) == ("mihon-unknown", [107, 600, 601])
    assert [m.title for m in raw.manga] == ["T"]
    # a text over the bound leaves its series out, counted, and the rest of the file is read
    mangas = [
        env.manga(1, "/a", "x" * 5_000), env.manga(1, "/" + "u" * 5_000, "B"),
        env.manga(1, "/c", "C", [env.chapter("/" + "c" * 5_000, 1.0)]), env.manga(1, "/d", "D"),
    ]  # fmt: skip
    raw = decode_backup(env.tachibk(env.backup(mangas)))
    assert [m.title for m in raw.manga] == ["D"] and raw.skipped_long == 3
    assert map_backup(raw).skipped_long == 3


@pytest.mark.req("EF-INT-008")
def test_nothing_in_the_decoder_reads_a_path_or_asks_a_network() -> None:
    # a path, an address or a script in a title is only text: nothing opens it
    hostile = env.manga(
        1, "../../etc/passwd", "file:///etc/shadow", [env.chapter("http://127.0.0.1/x", 1.0)]
    )
    raw = decode_backup(env.tachibk(env.backup([hostile])))
    assert (raw.manga[0].url, raw.manga[0].title) == ("../../etc/passwd", "file:///etc/shadow")


# -- the map ---------------------------------------------------------------------------------------


@pytest.mark.req("EF-INT-008", "ENF-SEC-002")
def test_a_real_sized_library_is_read_and_a_scalar_flood_is_still_refused() -> None:
    # 150 series of 120 chapters: 18 000 chapters of 7 fields each, well under the bounds
    chapters = [env.chapter(f"/c/{n}", float(n), read=True, page=3) for n in range(120)]
    series = [env.manga(7, f"/s/{i}", f"Series {i}", chapters) for i in range(150)]
    big = decode_backup(env.tachibk(env.backup(series)))
    assert sum(len(m.chapters) for m in big.manga) == 18_000
    # two million scalar fields in one series are a flood, whatever their kind
    flood = env.manga(1, "/u", "T", extra=b"".join(env.vi(90, 1) for _ in range(2_000_001)))
    assert _refused(env.tachibk(env.backup([flood]))).reason == "too_many_entries"
    # a group at the top level is a field of a later version: skipped like any other, and a file
    # made of nothing else knows no field of the format, so it is refused by name
    only_unknown = env.tachibk(env.nested_groups(2))
    assert _refused(only_unknown).code == "import.tachibk_schema"
    assert decode_backup(env.tachibk(env.backup([], extra=env.nested_groups(2)))).unknown_fields
    # many sources are listed once each, quickly
    many = [(n, "s") for n in range(1, 30_001)]
    started = time.monotonic()
    assert len(decode_backup(env.tachibk(env.backup([], sources=many))).source_ids) == 30_000
    assert time.monotonic() - started < 5


@pytest.mark.req("EF-INT-008")
def test_trackers_are_named_by_the_ids_mihon_gives_them() -> None:
    ids = {1: "myanimelist", 2: "anilist", 3: "kitsu", 4: "shikimori", 5: "bangumi",
           7: "mangaupdates"}  # fmt: skip
    entries = [env.tracking(k, 10 + k) for k in (*ids, 6, 8, 9)]  # 6, 8, 9: self-hosted servers
    staged = map_backup(
        decode_backup(env.tachibk(env.backup([env.manga(1, "/u", "T", trackers=entries)])))
    )
    assert [h.tracker for h in staged.series[0].hints] == list(ids.values())
    assert staged.skipped_trackers == 3


@pytest.mark.req("EF-INT-008")
def test_a_chapter_is_read_only_when_the_file_says_it_is() -> None:
    staged = map_backup(decode_backup(env.sample()))
    first = staged.series[0]
    by_ref = {m.chapter_ref: m for m in first.markers}
    # a marker for each chapter the file proves read, or started; nothing else
    assert sorted(by_ref) == ["/c/1", "/c/2", "/c/3"]
    assert (by_ref["/c/1"].read, by_ref["/c/2"].read) == (True, True)
    assert (by_ref["/c/3"].read, by_ref["/c/3"].last_page) == (False, 7)
    # the known units: every chapter the file lists, in the state `known`, read or not
    assert [(u.ref, u.state) for u in first.units] == [
        ("/c/1", "known"), ("/c/2", "known"), ("/c/3", "known"), ("/c/4", "known"),
        ("/c/5", "known"),
    ]  # fmt: skip
    # the tracker said the reader was at chapter 40: no chapter becomes read because of it
    assert not any(m.read for m in first.markers if m.chapter_ref not in {"/c/1", "/c/2"})
    # a flag that is not a boolean proves nothing
    odd = env.manga(1, "/u", "T", [env.chapter("/c/9", 9.0, extra=env.vi(4, 2))])
    assert map_backup(decode_backup(env.tachibk(env.backup([odd])))).series[0].markers == []


@pytest.mark.req("EF-INT-008")
def test_tracker_hints_hold_what_identifies_the_entry_and_nothing_else() -> None:
    staged = map_backup(decode_backup(env.sample()))
    hints = staged.series[0].hints
    assert [(h.tracker, h.remote_id, h.last_chapter) for h in hints] == [("anilist", "7001", 40.0)]
    dumped = staged.to_json()
    assert env.SECRET not in dumped and "tracker.example" not in dumped
    assert env.SOURCE_NAME not in dumped and "SynthSource" not in dumped
    # a tracker this build does not know is left out, and counted
    odd = env.manga(1, "/u", "T", trackers=[env.tracking(99, 5)])
    again = map_backup(decode_backup(env.tachibk(env.backup([odd]))))
    assert again.series[0].hints == [] and again.skipped_trackers == 1


@pytest.mark.req("EF-INT-008")
def test_a_series_off_the_library_or_on_the_local_source_is_left_out() -> None:
    mangas = [
        env.manga(7, "/a", "Kept"),
        env.manga(7, "/b", "Not in the library", favorite=False),
        env.manga(0, "Folder Name", "On the local source"),  # source 0: a folder of the device
    ]
    staged = map_backup(decode_backup(env.tachibk(env.backup(mangas))))
    assert [s.title for s in staged.series] == ["Kept"]
    assert [s.source_key for s in staged.series] == ["mihon:7"]
    assert (staged.skipped_not_favorite, staged.skipped_local) == (1, 1)


@pytest.mark.req("EF-INT-008")
def test_titles_are_cleaned_as_upstream_text_and_numbers_never_invented() -> None:
    nasty = env.manga(
        3, "/u", "<script>alert(1)</script>Bold ‮title‬",
        [env.chapter("/c", float("nan"), read=True), env.chapter("/d", 1e30, read=True)],
    )  # fmt: skip
    series = map_backup(decode_backup(env.tachibk(env.backup([nasty])))).series[0]
    assert "<script>" not in series.title and "‮" not in series.title
    # a chapter number that is not a plausible number is not kept: the marker keeps its reference
    assert [m.number for m in series.markers] == [None, None]
    assert [m.chapter_ref for m in series.markers] == ["/c", "/d"]
