"""M3 L09 (EF-USR-006): what an event payload names, as the filter of the projection reads it."""

import pytest

from shelfhold.events.projection import event_refs


@pytest.mark.req("EF-USR-006")
def test_event_refs_reads_every_kind_of_name() -> None:
    refs = event_refs(
        {
            "work_id": "w1", "other_work_id": "w2", "edition_id": "e1", "series_id": "s1",
            "unit_id": "u1", "work_ids": ["w3", 4, None], "edition_ids": ("e2",),
            "state": "done", "count": 3, "note": "work_id",
        }
    )  # fmt: skip
    assert refs.works == {"w1", "w2", "w3"}
    assert refs.editions == {"e1", "e2"}
    assert refs.series == {"s1"} and refs.units == {"u1"}
    assert bool(refs)


@pytest.mark.req("EF-USR-006")
def test_event_refs_finds_names_in_nested_payloads_and_ignores_the_rest() -> None:
    refs = event_refs({"detail": {"items": [{"work_id": "deep"}], "level": {"edition_id": "e"}}})
    assert refs.works == {"deep"} and refs.editions == {"e"}
    assert not event_refs({"work_id": 7, "series_id": None, "state": "ok"})  # not a name
    assert not event_refs({})
    too_deep: dict[str, object] = {"work_id": "far"}
    for _ in range(10):
        too_deep = {"inner": too_deep}
    assert event_refs(too_deep).opaque  # a bound on the walk, never a way round the filter


@pytest.mark.req("EF-USR-006")
def test_a_row_of_the_destination_is_not_read_as_a_catalogue_series() -> None:
    """`export_series_id` names a row of `export_series` and `kavita_series_id` a series of the
    server: neither is a series of the catalogue, and none is looked for there."""
    refs = event_refs(
        {"export_series_id": "x", "kavita_series_id": "k", "remote_unit_id": "r", "series_id": "s"}
    )
    assert refs.series == {"s"} and refs.units == frozenset()
    assert not event_refs({"export_series_id": "x"})
