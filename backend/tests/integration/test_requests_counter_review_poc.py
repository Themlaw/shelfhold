"""Counter-review of #209 (M3 L35), after the fixes: proofs of concept, each red on the head of
the PR. None of them changes production code. The ports are the fakes of `tests/request_env.py`;
the source is `source-a`."""

from __future__ import annotations

from typing import Any

import pytest

from shelfhold.acquisition.application.requests import SourceRef
from shelfhold.config import Settings
from tests.request_env import CHILD_ID, SIBLING_ID, FakePorts, admin, bind_work, requester, rig

SRC = "source-a"
REF = SourceRef(source_key=SRC, remote_id="r1", title="A Quiet Tale", memo={"entry": "e1"})


@pytest.mark.req("EF-USR-010", "EF-USR-006")
def test_counter_poc_a_stricter_age_after_the_series_arrived_is_dropped(
    settings: Settings,
) -> None:
    """B1, at arrival, once the first request has arrived. A followed series: the younger
    child's request is approved with 10, the Work appears and is rated 10. The elder asks for
    the same series; the preview asks for an age and shows none decided; the admin approves
    with 16. At the next arrival `rate_decided` never writes over a rated Work: the 16 is
    silently dropped and the laxer 10 stands. A novel in the same place is re-rated 16
    (`_to_apply`): the stricter must win for a followed series too, or the preview must say
    the Work is already rated."""
    ports = FakePorts(kinds={SRC: "manga"})
    with rig(settings, ports=ports) as r:
        young = r.service.create_from_ref(requester(CHILD_ID, "child"), REF).id
        r.service.approve(young, admin(), age=10)
        with r.engine.begin() as connection:
            work = bind_work(connection, SRC, "r1")
        r.service.arrived(work)
        assert r.state_of(young) == "arrived"
        elder = r.service.create_from_ref(requester(SIBLING_ID, "child"), REF).id
        preview = r.service.preview(elder, admin())
        assert preview.needs_age and preview.decided_age is None  # nothing says "rated 10"
        r.service.approve(elder, admin(), age=16)
        r.service.arrived(work)  # the next Unit of the series
        assert r.state_of(elder) == "arrived"
        rated: Any = r.rows("SELECT age_rating FROM work WHERE id = :w", w=work)[0][0]
        assert rated == 16, f"the admin decided 16, the Work stays rated {rated}"
