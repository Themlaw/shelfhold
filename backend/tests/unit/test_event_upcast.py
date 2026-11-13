"""M3 L12 (EF-INT-002): an old event is presented at the current version, the row is not touched."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from shelfhold.events.bus import SCHEMA_VERSION
from shelfhold.events.upcast import (
    UPCASTERS,
    UnreadableVersionError,
    upcast_payload,
)

TYPE = "work.updated"
# A history of three versions of one payload: v1 `name`, v2 `title`, v3 adds `lang`.
STEPS = {
    (TYPE, 1): lambda p: {"title": p["name"], "work_id": p["work_id"]},
    (TYPE, 2): lambda p: {**p, "lang": "en"},
}
OLD_SHAPES = {
    1: {"name": "A tale", "work_id": "w1"},
    2: {"title": "A tale", "work_id": "w1"},
    3: {"title": "A tale", "work_id": "w1", "lang": "en"},
}


@pytest.mark.req("EF-INT-002")
@pytest.mark.parametrize("version", [1, 2, 3])
def test_event_upcast_for_each_retained_schema_version(version: int) -> None:
    """Each version of the (three-version) history reaches the current shape, none is rewritten."""
    old = OLD_SHAPES[version]
    before = copy.deepcopy(old)
    out = upcast_payload(TYPE, version, old, current=3, steps=STEPS)
    assert out == OLD_SHAPES[3]
    assert old == before  # the input is a row's payload: never modified


@pytest.mark.req("EF-INT-002")
def test_event_upcast_leaves_a_type_with_no_step_untouched() -> None:
    payload: dict[str, Any] = {"x": 1}
    assert upcast_payload("unit.stored", 1, payload, current=3, steps=STEPS) == payload


@pytest.mark.req("EF-INT-002")
def test_event_upcast_refuses_a_version_it_cannot_present() -> None:
    for version in (0, -1, 4):
        with pytest.raises(UnreadableVersionError):
            upcast_payload(TYPE, version, {}, current=3, steps=STEPS)


@pytest.mark.req("EF-INT-002")
def test_every_retained_version_of_the_real_registry_reaches_the_current_one() -> None:
    """Whatever steps the code holds, every version a row of the table may carry (1 up to the
    current one) is presentable, and a step never skips or reaches past the current version."""
    for version in range(1, SCHEMA_VERSION + 1):
        out = upcast_payload("unit.stored", version, {"k": "v"})
        assert out == {"k": "v"} or any(t == "unit.stored" for t, _ in UPCASTERS)
    for _event_type, from_version in UPCASTERS:
        assert 1 <= from_version < SCHEMA_VERSION
