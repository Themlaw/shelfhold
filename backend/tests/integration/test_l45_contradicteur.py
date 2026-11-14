"""Adverse review of the L45 doors (PR #252): one red test per hole found.

Each test turns green when its objection in `docs/decisions/M3.md` ("L45 : relecture adverse") is
lifted. Production is not changed here.
"""

import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

import shelfhold.runtime_jobs as rj
from shelfhold.config import Settings
from shelfhold.infrastructure.schema import export
from shelfhold.ops.detach import DetachOps
from tests.auth_env import AuthEnv, auth_env, db_rows
from tests.integration.test_l45_destination_doors import (
    DETACH_PREVIEW,
    REMOVAL,
    REMOVAL_PREVIEW,
    _Teardown,
)
from tests.l50_env import seeded

pytestmark = pytest.mark.req("EF-EXP-031", "EF-EXP-005", "EF-USR-022")


def _service(env: Any) -> rj.PlanService:
    comp = env.inst.comp
    pipeline, _reconciler, _fsops = rj.build_pipeline(comp, env.scans)
    return rj.PlanService(comp, lambda: pipeline, lambda: None)


# -- M1: the count of contested names waits behind a preview that waits on Kavita ---------------


def test_the_contested_count_never_waits_on_a_preview_that_waits_on_kavita(
    settings: Settings, tmp_path: Path
) -> None:
    """`PlanService.preview` holds `_lock` while it asks Kavita (`refresh_names`, `BulkParse`):
    a sleeping Kavita holds it for the whole network timeout. `flagged_names` takes the same lock,
    so `GET /destinations` (and the Shelves screen) hangs with it, against its own docstring
    ("never waits on a sleeping server"). The count reads the database alone: it needs no lock."""
    with seeded(settings, tmp_path) as env:
        env.add_chapter(env.new_series("Dr Stone"), "1")
        service = _service(env)
        done: list[int] = []
        service._lock.acquire()  # a preview, waiting on Kavita
        try:
            worker = threading.Thread(
                target=lambda: done.append(service.flagged_names(env.destination_id)),
                daemon=True,
            )
            worker.start()
            worker.join(timeout=3)
            answered = not worker.is_alive()
        finally:
            service._lock.release()
            worker.join(timeout=30)
        assert answered, "the listing waited on the preview lock"
        assert done == [0]


# -- M2: one destination that cannot be counted takes the whole listing down --------------------


def test_one_destination_that_cannot_be_counted_does_not_hide_the_others(
    settings: Settings, tmp_path: Path
) -> None:
    """`list_destinations` already reads an unreadable template as `{}`, but then calls the
    count, which loads the same template and raises: `GET /destinations` answers 500 and the
    screen meant to remove the broken destination cannot even list it. The same 500 comes from a
    destination removed between the listing query and its count (`scalar_one`, `.one()`)."""
    with seeded(settings, tmp_path) as env:
        env.add_chapter(env.new_series("Dr Stone"), "1")
        service = _service(env)
        with env.inst.comp.engine.begin() as conn:
            conn.execute(
                sa.update(export.shelf_destination)
                .where(export.shelf_destination.c.id == env.destination_id)
                .values(template='{"no_such_field": 1}')
            )
        ops = DetachOps(
            env.inst.comp,
            SimpleNamespace(),  # type: ignore[arg-type]
            flagged_names=service.flagged_names,
        )
        items = ops.list_destinations()  # raises today
        assert [item["id"] for item in items] == [env.destination_id]


# -- m1: a preview and a refused phrase are written in the audit as a permanent deletion --------


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    services = SimpleNamespace(teardown=_Teardown())
    with auth_env(monkeypatch, settings, ops_services=services) as env:
        yield env


def test_a_preview_or_a_wrong_phrase_leaves_no_line_of_a_permanent_deletion(api: AuthEnv) -> None:
    """The two previews go through `reserved_action(..., audit=True)`: the guard writes a line
    `destination.remove_all` / `detach.full` (catalogued `DELETES_PERMANENTLY`) for a call that
    deletes nothing, and so does a removal refused for its phrase. The service already writes its
    own line, with its target, when it really removes or detaches: the route must not add one."""
    api.login()
    assert api.post(REMOVAL_PREVIEW, json={"mode": "delete_files"}).status_code == 200
    assert api.post(DETACH_PREVIEW).status_code == 200
    wrong = api.post(REMOVAL, json={"mode": "delete_files", "typed": "not the phrase"})
    assert wrong.status_code == 409
    lines = db_rows(
        api.settings,
        "SELECT action FROM audit WHERE action IN ('destination.remove_all', 'detach.full')",
    )
    assert lines == []
