"""L24 (EF-BIB-029): the axis of the « Même œuvre ? » screen over the real API. A series that moved
sources in Suwayomi is proposed as "Same work?" and listed with its two Works; the merge plan comes
from the M3 door (`POST /api/v1/works/merge`) and the Edition merge plan from the M4 door
(`POST /api/v1/editions/merge`), whose plan consults the tombstones; nothing is applied without the
fingerprint of the plan that was shown; a visitor, a member, a child and an API key reach none of
the reserved doors."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.catalog.application.external_ids import add_external_id
from shelfhold.catalog.application.tombstone_screen import MergedPlace
from shelfhold.catalog.domain.external_ids import ExternalId
from shelfhold.catalog.domain.identity import sort_title
from shelfhold.composition import Composition
from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.schema import catalog
from shelfhold.ops.dedup import DedupOps
from shelfhold.ops.edition_merge import EditionMergeOps
from shelfhold.ops.editions import EditionOps
from shelfhold.ops.work_merge import WorkMergeOps
from tests.auth_env import CSRF_HEADER, ManualClock, auth_env, run_cli
from tests.catalog_env import database
from tests.integration.test_schema_catalog import (
    add_binding,
    add_edition,
    add_source_item,
    add_unit,
    add_work,
)
from tests.visibility_env import add_profile

pytestmark = pytest.mark.req("EF-BIB-029")

SAME_WORK = "suwayomi_migration"
KEY = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")
SUGGESTIONS = "/api/v1/merge-suggestions/works"
SCAN = "/api/v1/merge-suggestions/works/scan"
EDITIONS = "/api/v1/editions"
WORKS_MERGE = "/api/v1/works/merge"
EDITIONS_MERGE = "/api/v1/editions/merge"


class Screen:
    """The tombstones: the Work identifiers and the Unit places it is given as purged."""

    def __init__(self) -> None:
        self.purged_ids: set[tuple[str, str]] = set()
        self.purged_places: set[str] = set()

    def places(self, places: Sequence[MergedPlace]) -> set[str]:
        return {p.unit_id for p in places} & self.purged_places

    def work_ids(self, ids: Any) -> set[tuple[str, str]]:
        return {(i.provider, i.value) for i in ids} & self.purged_ids

    def announce(self) -> None:
        pass


class _Screens:
    def __init__(self, screen: Screen) -> None:
        self._screen = screen

    def open(self) -> Screen:
        return self._screen


class _Ops:
    def __init__(self, comp: Composition, engine: Engine, screen: Screen) -> None:
        self.dedup = DedupOps(engine, _Screens(screen))
        self.work_merge = WorkMergeOps(comp)
        self.edition_merge = EditionMergeOps(comp, _Screens(screen))
        self.editions = EditionOps(comp)


@contextmanager
def world(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, screen: Screen | None = None
) -> Iterator[tuple[Any, Engine]]:
    """The real app with the real ops over one migrated database (the catalog lives in `engine`,
    the auth in the app's own one, as in the M3 L21 door test)."""
    screen = screen or Screen()
    with database() as path:
        engine = create_db_engine(path)
        comp = Composition(settings, engine, clock=ManualClock())
        ops = _Ops(comp, engine, screen)
        with auth_env(monkeypatch, settings, ops_services=ops) as api:
            yield api, engine
        engine.dispose()


def seed(
    engine: Engine, *, same_key: bool, donor_unit: bool = False
) -> tuple[str, str, str, str, str]:
    """The old Work, whose Suwayomi binding went away, and the new one that appeared live under the
    same normalized title, each with one Edition; the old Work also has a second Edition (another
    publisher) that may hold a Unit. Returns (old, new, old_edition, new_edition, twin_edition)."""
    with engine.begin() as connection:
        old = add_work(connection, title="La Voie", sort_title=sort_title("La Voie"))
        new = add_work(connection, title="la  voie ", sort_title=sort_title("la  voie "))
        old_edition = add_edition(connection, old)
        new_edition = add_edition(connection, new, publisher="" if same_key else "Gamma")
        twin_edition = add_edition(connection, old, publisher="Beta")
        gone_item = add_source_item(connection, remote_id="gone-1")
        add_binding(
            connection, old, old_edition, gone_item, active=False, disabled_reason="not_loaded"
        )
        live_item = add_source_item(connection, remote_id="live-1")
        add_binding(connection, new, new_edition, live_item)
        if donor_unit:
            add_unit(connection, twin_edition, "1")
    return old, new, old_edition, new_edition, twin_edition


def work_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(sa.select(sa.func.count()).select_from(catalog.work)).scalar_one()
        )


def edition_owner(engine: Engine, edition_id: str) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                sa.select(catalog.edition.c.work_id).where(catalog.edition.c.id == edition_id)
            ).scalar_one()
        )


def _key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> dict[str, str]:
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "admin", "keys", "create", "--name", "ci") == 0
    match = KEY.search(capsys.readouterr().out)
    assert match
    return {"Authorization": f"Bearer {match.group(0)}"}


@pytest.mark.req("EF-BIB-029")
def test_the_proposal_is_listed_with_its_works_and_the_work_merge_applies_for_its_fingerprint(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with world(monkeypatch, settings) as (api, engine):
        old, new, _old_edition, new_edition, _twin = seed(engine, same_key=False)
        api.login()
        assert len(api.post(SCAN).json()["created"]) == 1
        listed = api.client.get(SUGGESTIONS).json()["items"]
        assert [(s["reason"], s["state"]) for s in listed] == [(SAME_WORK, "open")]
        assert {listed[0]["a"], listed[0]["b"]} == {old, new}
        # the screen reads the titles of the two Works in the Editions list
        titles = {r["work_id"]: r["title"] for r in api.client.get(EDITIONS).json()["items"]}
        assert titles[old] == "La Voie" and titles[new] == "la voie"
        # the M3 door: a plan first, applied only for its fingerprint
        plan = api.post(WORKS_MERGE, json={"keep_id": old, "drop_id": new})
        assert plan.status_code == 200, plan.text
        shown = plan.json()
        assert shown["applied"] is False and shown["mergeable"] is True
        assert shown["editions"] == [new_edition] and shown["fingerprint"]
        assert work_count(engine) == 2  # a plan writes nothing
        done = api.post(
            WORKS_MERGE, json={"keep_id": old, "drop_id": new, "confirm": shown["fingerprint"]}
        )
        assert done.status_code == 200, done.text
        assert done.json()["applied"] is True and done.json()["merge_id"]
        assert work_count(engine) == 1  # the absorbed Work is gone
        assert edition_owner(engine, new_edition) == old  # its Edition moved to the survivor


@pytest.mark.req("EF-BIB-029")
def test_the_edition_merge_plan_consults_the_tombstones(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    screen = Screen()
    with world(monkeypatch, settings, screen) as (api, engine):
        old, new, old_edition, _new_edition, twin = seed(engine, same_key=True, donor_unit=True)
        with engine.begin() as connection:  # the Unit of the twin Edition
            donor_unit = connection.execute(
                sa.select(catalog.unit.c.id).where(catalog.unit.c.edition_id == twin)
            ).scalar_one()
        api.login()
        assert len(api.post(SCAN).json()["created"]) == 1
        # the M3 door: the two Editions would share a key, the work merge waits and says why
        blocked = api.post(WORKS_MERGE, json={"keep_id": old, "drop_id": new}).json()
        assert blocked["blockers"] == ["collision:edition_key"] and not blocked["mergeable"]
        # the M4 door: the plan consults the tombstones, a hit stops the merge
        screen.purged_places = {str(donor_unit)}
        with engine.begin() as connection:
            add_external_id(connection, old, ExternalId("work", "anilist", "11"), origin="provider")
        screen.purged_ids = {("anilist", "11")}
        shown = api.post(EDITIONS_MERGE, json={"keep_id": old_edition, "drop_id": twin})
        assert shown.status_code == 200, shown.text
        plan = shown.json()
        assert plan["applied"] is False and plan["work_id"] == old
        assert plan["tombstoned_units"] == 1 and plan["tombstoned_ids"] == 1
        assert not plan["mergeable"]  # a tombstone hit is a blocker, nothing is applied
        refused = api.post(
            EDITIONS_MERGE,
            json={"keep_id": old_edition, "drop_id": twin, "confirm": plan["fingerprint"]},
        )
        assert refused.status_code == 409 and refused.json()["code"] == "edition.merge_refused"
        assert edition_owner(engine, twin) == old  # nothing moved
        # without the tombstone hits, the edition merge applies for its fingerprint
        screen.purged_places = set()
        screen.purged_ids = set()
        plan = api.post(EDITIONS_MERGE, json={"keep_id": old_edition, "drop_id": twin}).json()
        assert plan["mergeable"] and plan["tombstoned_units"] == 0 and plan["tombstoned_ids"] == 0
        done = api.post(
            EDITIONS_MERGE,
            json={"keep_id": old_edition, "drop_id": twin, "confirm": plan["fingerprint"]},
        )
        assert done.status_code == 200 and done.json()["applied"] is True
        with engine.connect() as connection:
            moved = connection.execute(
                sa.select(catalog.unit.c.edition_id).where(catalog.unit.c.id == donor_unit)
            ).scalar_one()
        assert str(moved) == old_edition  # the Unit moved to the survivor


@pytest.mark.req("EF-BIB-029", "EF-USR-024")
def test_the_screen_doors_refuse_everyone_but_a_fresh_admin_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    with world(monkeypatch, settings) as (api, engine):
        old, new, old_edition, new_edition, _twin = seed(engine, same_key=False)
        doors = (
            ("GET", SUGGESTIONS, None),
            ("POST", SCAN, None),
            ("GET", EDITIONS, None),
            ("POST", WORKS_MERGE, {"keep_id": old, "drop_id": new}),
            ("POST", EDITIONS_MERGE, {"keep_id": old_edition, "drop_id": new_edition}),
        )
        for method, path, body in doors:  # no session
            answer = api.client.request(method, path, json=body)
            assert answer.status_code == 401, (method, path)
        admin_key = _key(monkeypatch, capsys, settings)
        for path, body in (
            (WORKS_MERGE, {"keep_id": old, "drop_id": new}),
            (EDITIONS_MERGE, {"keep_id": old_edition, "drop_id": new_edition}),
        ):
            refused = api.client.post(path, json=body, headers=admin_key)
            assert refused.status_code == 403, path  # a reserved action is never a key's
            assert refused.json()["type"] in (
                "/api/v1/problems/credential-not-allowed",
                "/api/v1/problems/forbidden",
            )
        api.login()
        for role, ip in (("member", "10.0.0.5"), ("child", "10.0.0.6")):
            extra: dict[str, object] = {"max_age_rating": 10} if role == "child" else {}
            who = add_profile(api, f"{role}1", role, ip, **extra)
            for method, path, body in doors:
                answer = who.request(method, path, json=body, headers={CSRF_HEADER: api.csrf(who)})
                assert answer.status_code == 403, (role, method, path)
        assert work_count(engine) == 2  # no refusal reached the service
