"""The store budget (EF-BIB-042 (4)): pure rules, shared with "Mettre à l'abri" (L16)."""

import pytest

from shelfhold.library.domain.store_budget import GIB, Candidate, StoreBudget, plan_batch

pytestmark = pytest.mark.req("EF-BIB-042")


def queue(count: int, size: int) -> list[Candidate]:
    return [Candidate(f"U{n:03}", size) for n in range(count)]


def test_800_candidates_and_20_gib_over_the_reserve_give_one_capped_batch() -> None:
    budget = StoreBudget(free_bytes=30 * GIB, reserve_bytes=10 * GIB)  # 20 GiB of room
    batch = plan_batch(queue(800, 100 * 1024 * 1024), budget)  # 100 MiB each
    assert len(batch.selected) == 51  # 5 GiB per cycle is the cap that bites first
    assert batch.backlog == 749 and batch.bytes <= 5 * GIB
    small = plan_batch(queue(800, 1024), budget)
    assert len(small.selected) == 200 and small.backlog == 600  # 200 Units per cycle


def test_nothing_is_queued_when_the_room_is_gone() -> None:
    batch = plan_batch(queue(3, 1000), StoreBudget(free_bytes=5 * GIB, reserve_bytes=10 * GIB))
    assert batch.selected == () and batch.backlog == 3


def test_an_oversize_unit_goes_alone_when_it_fits_the_room() -> None:
    budget = StoreBudget(free_bytes=40 * GIB, reserve_bytes=0)
    batch = plan_batch([Candidate("BIG", 6 * GIB), Candidate("S", 1)], budget)
    assert [c.unit_id for c in batch.selected] == ["BIG"] and batch.backlog == 1
    tight = plan_batch([Candidate("BIG", 6 * GIB)], StoreBudget(free_bytes=GIB, reserve_bytes=0))
    assert tight.selected == ()
