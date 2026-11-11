"""The host's own look at what a range service answers (M2 L25): EF-SRC-034, EF-SRC-032, EF-BIB-051.

The adapter checks the answer of its instance; the host checks it again, from what it asked, before
a byte is received: the second step of a two-step request never gets to name a range, a host or a
file that was not announced.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from shelfhold.acquisition.application.lncrawl_ranges import (
    MAX_COVERED,
    MAX_TICKET_AGE,
    RangeAnswerRefused,
    epub_only,
    ticket_expired,
    verify_range_answer,
)
from shelfhold.acquisition.application.ranges import RangeUnit
from shelfhold.library.public import Received
from shelfhold.sdk.refs import FileAsset, ImageRequest, RangedPayload, UnitInfo
from shelfhold.sdk.source import SourceChanged
from tests.ranged_env import volume_epub

ORIGIN = "http://lncrawl.lan:8080"
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def units(count: int = 4) -> list[RangeUnit]:
    return [
        RangeUnit(f"u{n}", UnitInfo(remote_unit_id=f"r{n}", source_order=n, volume_label="Vol.1"))
        for n in range(1, count + 1)
    ]


def payload(
    covers: list[str], *, url: str = ORIGIN + "/api/v1/artifacts/a1/file", **extra: Any
) -> RangedPayload:
    return RangedPayload(
        asset=FileAsset(
            request=ImageRequest(url=url), mime="application/epub+zip", filename="v.epub"
        ),
        covers=covers, volume_closed=True, **extra,
    )  # fmt: skip


@pytest.mark.req("EF-SRC-034")
def test_a_faithful_answer_passes_and_a_partial_one_too() -> None:
    verify_range_answer(units(), payload(["r1", "r2", "r3", "r4"]), origin=ORIGIN)
    verify_range_answer(units(), payload(["r2", "r4"]), origin=ORIGIN)  # a hole: left uncovered


@pytest.mark.req("EF-SRC-034")
@pytest.mark.parametrize(
    "covers",
    [["r1", "r9"], ["r1", "r1"], ["r2", "r1"], [], [f"r{n}" for n in range(MAX_COVERED + 1)]],
    ids=["unasked", "twice", "unordered", "empty", "too_many"],
)
def test_an_answer_that_covers_more_or_other_than_asked_is_refused(covers: list[str]) -> None:
    with pytest.raises(RangeAnswerRefused) as refused:
        verify_range_answer(units(), payload(covers), origin=ORIGIN)
    assert isinstance(refused.value, SourceChanged)  # a change of the source, not a state


@pytest.mark.req("EF-SRC-034", "ENF-SEC-009")
@pytest.mark.parametrize(
    "url",
    [
        "http://other.invalid/api/v1/artifacts/a1/file",
        "http://lncrawl.lan:9999/api/v1/artifacts/a1/file",  # same name, another port
        "https://lncrawl.lan:8080/api/v1/artifacts/a1/file",  # same name, another scheme
        "http://lncrawl.lan:8080@other.invalid/api/v1/artifacts/a1/file",
        "http://user:pw@lncrawl.lan:8080/api/v1/artifacts/a1/file",
        "http://lncrawl.lan.other.invalid:8080/x",
        "//lncrawl.lan:8080/x",
        "file:///etc/passwd",
    ],
)
def test_a_file_on_another_origin_than_the_instance_is_refused(url: str) -> None:
    with pytest.raises(RangeAnswerRefused):
        verify_range_answer(units(), payload(["r1"], url=url), origin=ORIGIN)


@pytest.mark.req("EF-SRC-034")
def test_bounds_may_name_only_what_the_file_covers() -> None:
    with pytest.raises(RangeAnswerRefused):
        verify_range_answer(units(), payload(["r1"], bounds={"r2": ("a", "b")}), origin=ORIGIN)


@pytest.mark.req("EF-SRC-034")
def test_a_ticket_older_than_the_bound_expires() -> None:
    assert not ticket_expired(NOW, NOW - MAX_TICKET_AGE + timedelta(seconds=1))
    assert ticket_expired(NOW, NOW - MAX_TICKET_AGE - timedelta(seconds=1))


def _received(tmp_path: Path, data: bytes) -> Received:
    path = tmp_path / "recv.bin"
    path.write_bytes(data)
    return Received(path=path, size=len(data), sha256="sha256:" + "0" * 64)


@pytest.mark.req("EF-SRC-010", "EF-BIB-051")
def test_only_an_epub_container_is_kept_as_a_ranged_file(tmp_path: Path) -> None:
    kept: list[str] = []
    keep = epub_only(lambda received, payload, item_id: kept.append(item_id))
    keep(_received(tmp_path, volume_epub(2)), payload(["r1"]), "item-1")
    assert kept == ["item-1"]
    for hostile in (b"<html><script>1</script></html>", b"PK\x03\x04" + b"\0" * 40, b"", b"\0" * 9):
        with pytest.raises(RangeAnswerRefused):
            keep(_received(tmp_path, hostile), payload(["r1"]), "item-2")
    assert kept == ["item-1"]  # nothing else was ever kept
