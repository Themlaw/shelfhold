"""EF-INT-026: the sentinel carries the restore epoch; a database older than it is behind."""

import pytest

from shelfhold.export.domain.physical_roots import (
    ExpectedRoot,
    RootState,
    SentinelDocument,
    judge_sentinel,
)
from shelfhold.export.domain.restore import fingerprint_of

pytestmark = pytest.mark.req("EF-INT-026")

ROOT = "01J0000000000000000000ROOT"
INSTANCE = "01J000000000000000000INST1"


def doc(generation: int, epoch: int | None = None, **extra: object) -> SentinelDocument:
    fields = dict(extra)
    if epoch is not None:
        fields["restore_epoch"] = epoch
    return SentinelDocument(ROOT, INSTANCE, generation, extra=fields)


def expected(generation: int, epoch: int = 0) -> ExpectedRoot:
    return ExpectedRoot(ROOT, "/library", INSTANCE, generation, epoch)


def test_sentinel_without_epoch_is_epoch_zero() -> None:
    assert doc(3).restore_epoch == 0
    assert doc(3, epoch=2).restore_epoch == 2


def test_sentinel_restamp_keeps_unknown_fields_and_sets_the_epoch() -> None:
    stamped = doc(3, epoch=1, tombstone_kids={"k": 1}, future="x").restamped(7, restore_epoch=4)
    assert (stamped.generation, stamped.restore_epoch) == (7, 4)
    assert stamped.extra["tombstone_kids"] == {"k": 1} and stamped.extra["future"] == "x"
    assert doc(3, epoch=2).restamped(5).restore_epoch == 2  # a normal session keeps the epoch
    parsed = SentinelDocument.parse(stamped.dumps())
    assert parsed.restore_epoch == 4 and parsed.generation == 7


@pytest.mark.parametrize(
    ("sentinel", "database", "state", "restamp"),
    [
        (doc(5), expected(5), RootState.OK, False),
        (doc(4), expected(5), RootState.OK, True),  # stopped between the commit and the sentinel
        (doc(3), expected(5), RootState.STALE, False),  # an old copy of the folder
        (doc(6), expected(5), RootState.RESTORED_BEHIND, False),  # the database is older
        (doc(5, epoch=2), expected(5, epoch=1), RootState.RESTORED_BEHIND, False),
        (doc(5, epoch=1), expected(5, epoch=2), RootState.OK, False),
        (doc(4, epoch=2), expected(5, epoch=1), RootState.RESTORED_BEHIND, False),
    ],
)
def test_judge_sentinel_against_the_database(
    sentinel: SentinelDocument, database: ExpectedRoot, state: RootState, restamp: bool
) -> None:
    verdict = judge_sentinel(sentinel, database)
    assert (verdict.state, verdict.restamp) == (state, restamp)
    assert verdict.blocks_writes is (state is not RootState.OK)


def test_fingerprint_depends_on_every_part_and_not_on_order() -> None:
    base = fingerprint_of({"b": [2, 1], "a": {"y": 1, "x": 2}})
    assert base == fingerprint_of({"a": {"x": 2, "y": 1}, "b": [2, 1]})
    assert base != fingerprint_of({"a": {"x": 2, "y": 1}, "b": [1, 2]})
    assert len(base) == 64
