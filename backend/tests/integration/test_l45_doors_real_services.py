"""The five doors of L45 through the real services, no recorder (M3 L45, EF-EXP-031, EF-USR-022).

The frozen tests of the doors stand a recorder behind the routes: they cannot say what the
services do with the phrase. Here the routes are the real ones and so is `DetachOps`: the phrase
is exact (no trailing space, no case, no look-alike character), it names the mode of a removal,
and a real removal leaves one line in the audit, with its target.
"""

import unicodedata
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

import shelfhold.runtime_jobs as rj
from shelfhold.config import Settings
from shelfhold.infrastructure.schema import export
from shelfhold.ops.detach import DetachOps
from tests.auth_env import auth_env, db_rows
from tests.l50_env import seeded

pytestmark = pytest.mark.req("EF-EXP-031", "EF-USR-022")

LIST = "/api/v1/destinations"


def _counter(env: Any) -> Any:
    comp = env.inst.comp
    pipeline, _reconciler, _fsops = rj.build_pipeline(comp, env.scans)
    return rj.PlanService(comp, lambda: pipeline, lambda: None).flagged_names


NBSP, HYPHEN, ACUTE = chr(0xA0), chr(0x2010), chr(0x301)


def _variants(phrase: str) -> list[str]:
    changed = [
        phrase + " ",
        " " + phrase,
        phrase + "\n",
        phrase.upper(),
        phrase.replace(" ", NBSP),  # a no-break space
        phrase.replace("-", HYPHEN),  # a look-alike hyphen
        unicodedata.normalize("NFD", phrase) + ACUTE,  # not the NFC form
    ]
    return [typed for typed in changed if typed != phrase]  # a phrase without a hyphen stays


def test_each_door_on_the_real_services_with_an_exact_phrase(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path) as env:
        dest = env.destination_id
        with env.inst.engine.connect() as conn:
            target = conn.execute(
                sa.select(export.shelf_destination.c.target_id).where(
                    export.shelf_destination.c.id == dest
                )
            ).scalar_one()
        teardown = DetachOps(env.inst.comp, SimpleNamespace(), flagged_names=_counter(env))  # type: ignore[arg-type]
        with auth_env(
            monkeypatch, settings, ops_services=SimpleNamespace(teardown=teardown)
        ) as api:
            env.inst.comp.clock = api.clock  # one time for the session and for the service
            api.login()
            [item] = api.client.get(LIST).json()["items"]
            assert item["id"] == dest and item["flagged_names"] == 0

            base = f"/api/v1/destinations/{dest}"
            keep = api.post(f"{base}/removal-preview", json={"mode": "keep_files"}).json()["phrase"]
            gone = api.post(f"{base}/removal-preview", json={"mode": "delete_files"}).json()[
                "phrase"
            ]
            assert keep != gone and dest in keep and dest in gone  # the mode is in the phrase
            detach = api.post(f"/api/v1/targets/{target}/detach-preview").json()["phrase"]
            assert target in detach

            for typed in (*_variants(keep), gone):  # the other mode's phrase too
                refused = api.post(f"{base}/removal", json={"mode": "keep_files", "typed": typed})
                assert refused.status_code == 409, repr(typed)
            for typed in _variants(detach):
                refused = api.post(f"/api/v1/targets/{target}/detach", json={"typed": typed})
                assert refused.status_code == 409, repr(typed)
            assert db_rows(settings, "SELECT id FROM shelf_destination WHERE id = ?", dest)

            # the previews and the refusals left no line: nothing was deleted
            only_audit = "SELECT target FROM audit WHERE action = 'destination.remove_all'"
            assert db_rows(settings, only_audit) == []
            done = api.post(f"{base}/removal", json={"mode": "keep_files", "typed": keep})
            assert done.status_code == 200 and done.json()["complete"] is True
            assert db_rows(settings, "SELECT id FROM shelf_destination WHERE id = ?", dest) == []
            assert db_rows(settings, only_audit) == [(f"{dest}:keep_files",)]
