"""M2 wiring (L26, L25, EF-SRC-032, EF-BIB-040, D5): the cut of a novel into volumes when its
source declares none. Pure decisions over the listing of a source: nothing reads a disk."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from shelfhold.acquisition.application.ranges import RangeTickets, RangeUnit
from shelfhold.acquisition.application.volume_cut import cut_volumes
from shelfhold.acquisition.domain.requests import RequestBudget
from shelfhold.library.public import DEFAULT_VOLUME_SIZE
from shelfhold.sdk.refs import UnitInfo

pytestmark = pytest.mark.req("EF-SRC-032", "EF-BIB-040")


def listing(n: int, **extra: object) -> list[UnitInfo]:
    return [UnitInfo(remote_unit_id=f"r{i}", source_order=i, number=str(i), **extra)  # type: ignore[arg-type]
            for i in range(1, n + 1)]  # fmt: skip


def labels(cut_infos: list[UnitInfo]) -> dict[str | None, int]:
    found: dict[str | None, int] = {}
    for info in cut_infos:
        found[info.volume_label] = found.get(info.volume_label, 0) + 1
    return found


def test_default_size_is_the_one_of_the_decision_d5() -> None:
    assert DEFAULT_VOLUME_SIZE == 50
    cut = cut_volumes(listing(120), policy="closed_only", size=None, finished=False, adopted=False)
    assert labels(cut.infos) == {"Vol.1": 50, "Vol.2": 50, "Vol.3": 20}
    closed = {i.volume_label: i.volume_closed for i in cut.infos}
    assert closed == {"Vol.1": True, "Vol.2": True, "Vol.3": False}
    assert cut.waiting == {f"r{i}" for i in range(101, 121)}  # closed_only: the tail waits


def test_the_policy_decides_which_volumes_are_asked() -> None:
    kwargs = {"size": 10, "adopted": False}
    rolling = cut_volumes(listing(25), policy="rolling_tail", finished=False, **kwargs)  # type: ignore[arg-type]
    assert rolling.waiting == frozenset()  # the tail is exported, with its warning, later
    assert {i.volume_label: i.volume_closed for i in rolling.infos}["Vol.3"] is False
    finished = cut_volumes(listing(25), policy="closed_only", finished=True, **kwargs)  # type: ignore[arg-type]
    assert finished.waiting == frozenset()
    assert {i.volume_label: i.volume_closed for i in finished.infos}["Vol.3"] is True
    whole = cut_volumes(listing(25), policy="whole", finished=False, **kwargs)  # type: ignore[arg-type]
    assert whole.waiting == {f"r{i}" for i in range(1, 26)}  # a novel in progress is never whole
    done = cut_volumes(listing(25), policy="whole", finished=True, **kwargs)  # type: ignore[arg-type]
    assert labels(done.infos) == {"Vol.1": 25} and done.waiting == frozenset()
    assert {i.volume_closed for i in done.infos} == {True}


def test_a_source_that_declares_volumes_is_never_cut_again() -> None:
    declared = listing(6)
    declared[0] = declared[0].model_copy(update={"volume_label": "Vol.7", "volume_closed": True})
    cut = cut_volumes(declared, policy="closed_only", size=2, finished=False, adopted=False)
    assert cut.infos == declared and cut.waiting == frozenset() and cut.cut == frozenset()


def test_nothing_is_cut_over_an_adopted_volume_or_an_unknown_policy() -> None:
    for kwargs in ({"policy": "closed_only", "adopted": True}, {"policy": "yolo", "adopted": False},
                   {"policy": "", "adopted": False}):  # fmt: skip
        cut = cut_volumes(listing(120), size=50, finished=False, **kwargs)  # type: ignore[arg-type]
        assert cut.infos == listing(120) and cut.cut == frozenset()  # a policy nobody knows: none


@pytest.mark.parametrize("size", [0, -1, None])
def test_a_size_that_is_not_one_falls_back_to_the_default(size: int | None) -> None:
    cut = cut_volumes(listing(120), policy="closed_only", size=size, finished=False, adopted=False)
    assert labels(cut.infos) == {"Vol.1": 50, "Vol.2": 50, "Vol.3": 20}


def test_only_main_chapters_are_cut_and_the_order_is_the_one_of_the_source() -> None:
    mixed = [i.model_copy(update={"source_order": 100 - i.source_order}) for i in listing(5)]
    mixed.append(UnitInfo(remote_unit_id="extra", source_order=0, track="extra", number="E1"))
    cut = cut_volumes(mixed, policy="closed_only", size=2, finished=True, adopted=False)
    by_id = {i.remote_unit_id: i.volume_label for i in cut.infos}
    assert by_id["extra"] is None  # an extra belongs to no volume
    assert (by_id["r5"], by_id["r4"], by_id["r3"], by_id["r2"], by_id["r1"]) == (
        "Vol.1",
        "Vol.1",
        "Vol.2",
        "Vol.2",
        "Vol.3",
    )  # source_order 0.. is r5 first: the cut follows the order the source gives


def test_the_cut_is_stable_when_chapters_join_the_tail() -> None:
    before = cut_volumes(listing(120), policy="closed_only", size=50, finished=False, adopted=False)
    after = cut_volumes(listing(131), policy="closed_only", size=50, finished=False, adopted=False)
    kept = {i.remote_unit_id: i.volume_label for i in before.infos if i.volume_closed}
    assert kept == {
        i.remote_unit_id: i.volume_label for i in after.infos if i.remote_unit_id in kept
    }


def test_an_empty_listing_and_a_huge_one() -> None:
    assert cut_volumes([], policy="closed_only", size=50, finished=True, adopted=False).infos == []
    big = cut_volumes(listing(20000), policy="closed_only", size=50, finished=False, adopted=False)
    assert len(big.cut) == 20000 and len(labels(big.infos)) == 400


def test_a_volume_never_exceeds_the_cap_of_a_key() -> None:
    """Review of #172: 50 Units were granted in one cycle against a cap of 10."""
    cut = cut_volumes(
        listing(120), policy="closed_only", size=None, finished=False, adopted=False, cap=10
    )
    sizes = labels(cut.infos)
    assert max(sizes.values()) == 10 and sum(sizes.values()) == 120
    whole = cut_volumes(
        listing(120), policy="whole", finished=True, adopted=False, size=None, cap=10
    )
    assert max(labels(whole.infos).values()) == 10


def _tickets() -> RangeTickets:
    tickets = RangeTickets.__new__(RangeTickets)
    tickets._series = SimpleNamespace(source_key="lncrawl")  # type: ignore[attr-defined]
    return tickets


def test_a_waiting_volume_is_not_starved_by_the_other_series_of_its_key() -> None:
    """Review of #172: with another series asking a chapter first each cycle, the volume was
    never granted (100 cycles out of 100). It now keeps the key free for the next cycle."""
    cut = cut_volumes(
        listing(120), policy="closed_only", size=None, finished=False, adopted=False, cap=10
    )
    volume = [RangeUnit(f"u{i}", x) for i, x in enumerate(cut.infos[:10])]
    tickets, held, granted_at = _tickets(), {}, None
    for cycle in range(100):
        budget = RequestBudget(held=held)
        assert budget.take("lncrawl", 1) == (0 if held else 1)  # the other series is kept out
        granted, _ = tickets._grant(volume, budget)
        held = budget.holds
        if granted:
            granted_at = cycle
            break
    assert granted_at == 1 and len(granted) == 10  # the cycle after the first refusal
    budget = RequestBudget(held={})
    assert budget.take("lncrawl", 10) == 10  # nothing stays held once it went


def test_one_volume_holds_a_key_at_a_time_and_a_hold_lasts_one_cycle() -> None:
    first = RequestBudget()
    first.hold("lncrawl", 10)
    first.hold("lncrawl", 3)  # a second volume of the same key waits behind
    assert first.holds == {"lncrawl": 10}
    later = RequestBudget(held={})  # the volume went (or the series left): the hold is gone
    assert later.room("lncrawl") == 10
