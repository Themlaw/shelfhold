"""M4 L50: the small rules of the relay that need no application (ADR-0053 §2 and §5).

The chain itself is `tests/integration/test_m4_l50_relay.py`; here the cases of the page map, the
keyring that cannot be read, and the shape of the warnings of the Health page.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
import sqlalchemy as sa

from shelfhold.ops.health import _kosync_unknown
from shelfhold.reading.application import page_map
from shelfhold.reading.application.sync import ChapterRef, _verify

pytestmark = pytest.mark.req("EF-LEC-009", "EF-EXP-026")


@dataclass(frozen=True)
class Ref:
    carrier_id: str
    pages: int


def test_the_codes_of_a_chapter_that_is_not_relayed_are_closed() -> None:
    assert page_map.CODES == ("page_count_mismatch", "page_map_unverified")


def test_no_chapter_means_nothing_to_compare_and_nothing_written() -> None:
    engine = sa.create_engine("sqlite://")  # never opened: nothing is asked of it
    assert page_map.verify_page_maps(engine, [], b"k") == page_map.PageMapReport()


def test_a_keyring_that_cannot_be_read_verifies_nothing_and_does_not_raise() -> None:
    def locked() -> object:
        raise RuntimeError("keyring is locked")

    refs = [ChapterRef(1, 1, 1, 1, 10, "01HZZZZZZZZZZZZZZZZZZZZZZA")]
    _verify(sa.create_engine("sqlite://"), refs, locked)  # type: ignore[arg-type]


def test_without_a_keyring_nothing_is_verified() -> None:
    _verify(sa.create_engine("sqlite://"), [], None)


def test_the_unknown_documents_are_listed_by_token_and_reason_without_a_digest() -> None:
    found = _kosync_unknown({"b-token": {"integrity": 2, "unknown": 1}, "a-token": {"unknown": 5}})
    assert found == [
        {"code": "kosync.unknown", "token": "a-token", "count": 5},
        {"code": "kosync.integrity", "token": "b-token", "count": 2},
        {"code": "kosync.unknown", "token": "b-token", "count": 1},
    ]
    assert _kosync_unknown(None) == []
