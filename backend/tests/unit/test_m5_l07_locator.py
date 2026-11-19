"""M5 L07 (EF-LEC-007, EF-LEC-014): the locator v1 of ADR-0055 and the bounded merge (pure).

The locator the reader sends is closed: an EPUB says where (`cfi`), how far (`totalProgression`)
and under which version of the filter the DOM was made (`filter`); a support of pages says which
page and of how many. Anything else is refused before the database sees it. The merge: the latest
`client_ts`, bounded by `server_ts`; a conflict only above the threshold of D8.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from shelfhold.infrastructure.content_trust import CONTENT_FILTER
from shelfhold.reading.domain.locator import InvalidLocatorV1Error, parse_locator_v1
from shelfhold.reading.domain.merge import Reported, conflicting, winner

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
EPUB: dict[str, Any] = {
    "v": 1, "type": "cfi", "cfi": "epubcfi(/6/4!/4/2/1:0)", "totalProgression": 0.37,
    "filter": CONTENT_FILTER,
}  # fmt: skip
PAGE: dict[str, Any] = {"v": 1, "type": "page", "index": 11, "count": 180}


@pytest.mark.req("EF-LEC-007")
def test_locator_v1_schema_rejects_missing_fields() -> None:
    """ADR-0055: an EPUB without `cfi`, `totalProgression` or `filter`, and a support of pages
    whose `index` is not below `count`, are refused; so is every shape the format does not name."""
    for field in ("cfi", "totalProgression", "filter", "v", "type"):
        broken = {k: v for k, v in EPUB.items() if k != field}
        with pytest.raises(InvalidLocatorV1Error):
            parse_locator_v1(broken)
    for field in ("index", "count"):
        with pytest.raises(InvalidLocatorV1Error):
            parse_locator_v1({k: v for k, v in PAGE.items() if k != field})
    bad: list[Any] = [
        {**EPUB, "totalProgression": 1.2}, {**EPUB, "totalProgression": -0.1},
        {**EPUB, "totalProgression": True}, {**EPUB, "totalProgression": float("nan")},
        {**EPUB, "cfi": "/6/4"}, {**EPUB, "cfi": "epubcfi(" + "1" * 5000 + ")"},
        {**EPUB, "filter": ""}, {**EPUB, "filter": "no version"}, {**EPUB, "extra": 1},
        {**EPUB, "v": 2}, {**EPUB, "type": "xpointer"},
        {**PAGE, "index": 180}, {**PAGE, "index": 200}, {**PAGE, "index": -1},
        {**PAGE, "count": 0}, {**PAGE, "index": 1.5}, {**PAGE, "index": True},
        {**PAGE, "index": "3"}, {**PAGE, "cfi": "epubcfi(/6)"},
        None, [], "text", 3,
    ]  # fmt: skip
    for raw in bad:
        with pytest.raises(InvalidLocatorV1Error):
            parse_locator_v1(raw)


@pytest.mark.req("EF-LEC-007", "EF-LEC-014")
def test_locator_v1_progression_and_the_stored_form() -> None:
    """`totalProgression` is read as it is for an EPUB and `(index + 1) / count` for pages; what
    is stored is rebuilt from the checked fields only (the page as an integer for the view)."""
    epub = parse_locator_v1(EPUB)
    assert (epub.kind, epub.progression, epub.filter) == ("cfi", 0.37, CONTENT_FILTER)
    assert epub.stored() == EPUB
    page = parse_locator_v1(PAGE)
    assert page.kind == "page" and page.progression == pytest.approx(12 / 180)
    assert page.stored() == {**PAGE, "page": 12}  # `page` counts from 1, `index` from 0
    assert parse_locator_v1({**PAGE, "index": 179}).progression == 1.0


def reported(
    ident: str,
    progression: float,
    minutes: int,
    *,
    device: str | None = "a",
    index: int | None = None,
    count: int | None = None,
    seen: int | None = None,
    content: str = "sha256:" + "a" * 64,
) -> Reported:
    sent = NOW + timedelta(minutes=minutes)
    return Reported(
        ident, device, progression, sent, sent if seen is None else NOW + timedelta(minutes=seen),
        content, index, count,
    )  # fmt: skip


@pytest.mark.req("EF-LEC-007")
def test_winner_is_the_latest_client_ts_bounded_by_server_ts() -> None:
    old, new = reported("1", 0.9, -10), reported("2", 0.1, -5)
    assert winner([old, new]).position_id == "2"  # the latest wins, even if it is behind
    assert winner([new, old]).position_id == "2"
    # a clock set far ahead counts as the day the server received it: it ties, then the id decides
    ahead = reported("3", 0.5, 600, seen=-4)  # claims +10 h, was received 4 minutes before NOW
    assert winner([new, ahead]).position_id == "3"  # received at -4, later than -5
    later = reported("4", 0.2, -3)
    assert winner([ahead, later]).position_id == "4"  # the claimed +10 h bought nothing
    assert winner([reported("5", 0.2, -3), reported("6", 0.3, -3)]).position_id == "6"
    with pytest.raises(ValueError, match="no position"):
        winner([])


@pytest.mark.req("EF-LEC-007")
def test_conflict_threshold_of_d8() -> None:
    """EPUB: more than 0.02 of `totalProgression`. Pages: more than 2 % **and** at least 3 pages.
    Only another device's position counts, on the same content, and only when it is the further."""
    win = reported("w", 0.30, 0, device="phone")
    assert conflicting(win, [reported("l", 0.33, -10, device="tab")]) is not None  # 0.03 > 0.02
    assert conflicting(win, [reported("l", 0.32, -10, device="tab")]) is None  # 0.02 is not above
    assert conflicting(win, [reported("l", 0.10, -10, device="tab")]) is None  # behind: no loss
    assert conflicting(win, [reported("l", 0.90, -10, device="phone")]) is None  # same device
    other = reported("l", 0.90, -10, device="tab", content="sha256:" + "b" * 64)
    assert conflicting(win, [other]) is None  # another content: not comparable
    best = conflicting(
        win, [reported("x", 0.5, -20, device="tab"), reported("y", 0.7, -30, device="kobo")]
    )
    assert best is not None and best.position_id == "y"  # the furthest of the losers
    pages = reported("w", 0.2, 0, device="phone", index=9, count=50)
    two = reported("l", 0.24, -9, device="tab", index=11, count=50)
    assert conflicting(pages, [two]) is None  # 4 % but 2 pages
    three = reported("l", 0.26, -9, device="tab", index=12, count=50)
    assert conflicting(pages, [three]) is not None  # 6 % and 3 pages
    big = reported("w", 0.2, 0, device="phone", index=99, count=1000)
    near = reported("l", 0.21, -9, device="tab", index=109, count=1000)
    assert conflicting(big, [near]) is None  # 10 pages, but 1 %
