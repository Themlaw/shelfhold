"""EF-BIB-039: related Works are linked by a typed relation and never merged."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from shelfhold.catalog.public import (
    RelatedWork,
    RelationError,
    relate_works,
    relations_of,
    unrelate_works,
)
from tests.catalog_env import Harness, harness

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


@pytest.fixture
def h() -> Iterator[Harness]:
    with harness() as inside:
        yield inside


def snapshot(h: Harness) -> dict[str, list[dict[str, object]]]:
    return {
        table: h.rows(f"SELECT * FROM {table} ORDER BY 1, 2")  # noqa: S608
        for table in ("work", "edition", "unit", "series_member", "source_binding")
    }


@pytest.mark.req("EF-BIB-039")
def test_work_relation_never_merges(h: Harness) -> None:
    novel = h.work("novel", "The novel")
    webtoon = h.work("webtoon", "The adaptation")
    h.edition(novel, kind="novel", media="text")
    h.edition(webtoon)
    before = snapshot(h)
    assert relate_works(h.connection, webtoon, novel, "adaptation_of", origin="admin", now=NOW)
    assert snapshot(h) == before  # two Works, their Editions and Units, exactly as they were
    assert h.scalar("SELECT count(*) FROM work WHERE id IN (:a, :b)", a=novel, b=webtoon) == 2
    # the relation is visible from each end of it
    assert relations_of(h.connection, webtoon) == [RelatedWork("adaptation_of", novel, True)]
    assert relations_of(h.connection, novel) == [RelatedWork("adaptation_of", webtoon, False)]


@pytest.mark.req("EF-BIB-039")
def test_work_relation_kinds_and_repeats(h: Harness) -> None:
    first, second = h.work(title="One"), h.work(title="Two")
    assert relate_works(h.connection, second, first, "sequel_of", origin="admin", now=NOW)
    assert not relate_works(h.connection, second, first, "sequel_of", origin="admin", now=NOW)
    assert relate_works(h.connection, first, second, "prequel_of", origin="admin", now=NOW)
    assert relate_works(h.connection, second, first, "spin_off_of", origin="admin", now=NOW)
    assert [r.kind for r in relations_of(h.connection, second)] == [
        "prequel_of", "sequel_of", "spin_off_of",
    ]  # fmt: skip
    assert unrelate_works(h.connection, second, first, "spin_off_of")
    assert not unrelate_works(h.connection, second, first, "spin_off_of")


@pytest.mark.req("EF-BIB-039")
def test_a_relation_needs_two_distinct_known_works_and_a_known_kind(h: Harness) -> None:
    work = h.work()
    other = h.work(title="Other")
    with pytest.raises(RelationError):
        relate_works(h.connection, work, work, "sequel_of", origin="admin", now=NOW)
    with pytest.raises(RelationError):
        relate_works(h.connection, work, "01JNOSUCHWORK000000000000A", "sequel_of",
                     origin="admin", now=NOW)  # fmt: skip
    with pytest.raises(RelationError):
        relate_works(h.connection, work, other, "merged_into", origin="admin", now=NOW)
    assert h.scalar("SELECT count(*) FROM work_relation") == 0
