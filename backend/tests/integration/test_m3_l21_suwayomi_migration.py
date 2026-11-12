"""M3 L21 (EF-BIB-029): a series that moved sources in Suwayomi is proposed as "Same work?", never
created and never merged on its own, and never on the title alone."""

from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from shelfhold.catalog.application.external_ids import add_external_id
from shelfhold.catalog.domain.external_ids import ExternalId
from shelfhold.catalog.public import BindingRef, detect_suwayomi_migrations
from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog
from shelfhold.ops.dedup import DedupOps
from tests.auth_env import auth_env
from tests.catalog_env import Harness, database, harness
from tests.integration.test_l20_dedup import NO_SCREEN, Screen
from tests.support.catalog import SupportCatalog

SAME_WORK = "suwayomi_migration"


@pytest.fixture
def h() -> Iterator[Harness]:
    with harness() as inside:
        yield inside


def bind(
    h: Harness, work_id: str, *, plugin: str = "suwayomi", reason: str | None = None
) -> BindingRef:
    """A binding of the Work: live when `reason` is None, else disabled for that reason."""
    edition = h.edition(work_id)
    item_id, binding_id = new_ulid(), new_ulid()
    h.connection.execute(
        sa.insert(acquisition.source_item).values(
            id=item_id, plugin_id=plugin, source_key=f"{plugin}:s", remote_id=new_ulid()
        )
    )
    h.connection.execute(
        sa.insert(acquisition.source_binding).values(
            id=binding_id, work_id=work_id, edition_id=edition.id, source_item_id=item_id,
            priority=1, active=reason is None, disabled_reason=reason,
        )
    )  # fmt: skip
    return BindingRef(id=binding_id, edition_id=edition.id)


def moved(h: Harness, old_title: str = "La Voie", new_title: str = "la  voie ") -> tuple[str, str]:
    """The old Work, whose Suwayomi source went away, and a new one that appeared live."""
    old, new = h.work("manga", old_title), h.work("manga", new_title)
    bind(h, old, reason="not_loaded")
    bind(h, new)
    return old, new


def detect(h: Harness, screen: Screen = NO_SCREEN) -> list[str]:
    return detect_suwayomi_migrations(h.connection, screen=screen)


def rows(h: Harness) -> list[dict[str, object]]:
    return h.rows("SELECT entity_type, a, b, reason, state FROM merge_suggestion")


def census(h: Harness) -> tuple[object, ...]:
    return tuple(
        h.connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
        for table in (
            catalog.work,
            catalog.edition,
            acquisition.source_binding,
            catalog.unit,
            catalog.merge_suggestion,
        )
    )


@pytest.mark.req("EF-BIB-029")
def test_a_migration_is_proposed_as_the_same_work_and_nothing_is_merged(h: Harness) -> None:
    old, new = moved(h)
    before = census(h)
    created = detect(h)
    assert len(created) == 1
    assert rows(h) == [
        {"entity_type": "work", "a": min(old, new), "b": max(old, new), "reason": SAME_WORK,
         "state": "open"}
    ]  # fmt: skip
    after = census(h)
    assert after[:-1] == before[:-1]  # no Work, Edition, binding or Unit created or merged
    assert detect(h) == []  # asked once


@pytest.mark.req("EF-BIB-029")
def test_a_pair_decided_is_not_asked_again(h: Harness) -> None:
    moved(h)
    detect(h)
    h.connection.execute(sa.text("UPDATE merge_suggestion SET state = 'rejected'"))
    assert detect(h) == []
    assert [r["state"] for r in rows(h)] == ["rejected"]


@pytest.mark.req("EF-BIB-029")
def test_the_title_alone_proposes_nothing(h: Harness) -> None:
    """Two live Works of one title are not a migration: no binding went away."""
    for title in ("La Voie", "La Voie"):
        bind(h, h.work("manga", title))
    assert detect(h) == []
    assert rows(h) == []


@pytest.mark.req("EF-BIB-029")
@pytest.mark.parametrize("reason", ["manual", "content_failures"])
def test_a_paused_or_failing_binding_is_not_a_migration(h: Harness, reason: str) -> None:
    old, new = h.work("manga", "La Voie"), h.work("manga", "La Voie")
    bind(h, old, reason=reason)
    bind(h, new)
    assert detect(h) == []


@pytest.mark.req("EF-BIB-029")
def test_a_binding_that_went_away_with_no_new_one_proposes_nothing(h: Harness) -> None:
    bind(h, h.work("manga", "La Voie"), reason="removed_upstream")
    assert detect(h) == []
    new = h.work("manga", "Autre")  # a new live Work whose title is not the old one's
    bind(h, new)
    assert detect(h) == []


@pytest.mark.req("EF-BIB-029")
def test_a_work_that_a_live_binding_still_serves_did_not_leave(h: Harness) -> None:
    old, _new = moved(h)
    bind(h, old, plugin="lncrawl")  # still served by another source
    assert detect(h) == []


@pytest.mark.req("EF-BIB-029")
def test_only_a_suwayomi_binding_makes_a_migration(h: Harness) -> None:
    old, new = h.work("manga", "La Voie"), h.work("manga", "La Voie")
    bind(h, old, plugin="lncrawl", reason="not_loaded")
    bind(h, new)
    assert detect(h) == []
    other = h.work("manga", "La Voie")
    bind(h, other, plugin="lncrawl")  # a live Work of another source is no arrival from Suwayomi
    assert detect(h) == []


@pytest.mark.req("EF-BIB-029")
def test_another_kind_is_another_work(h: Harness) -> None:
    old, new = h.work("manga", "La Voie"), h.work("novel", "La Voie")
    bind(h, old, reason="not_loaded")
    bind(h, new)
    assert detect(h) == []


@pytest.mark.req("EF-BIB-029")
def test_two_identifiers_of_one_provider_tell_two_works_apart(h: Harness) -> None:
    old, new = moved(h)
    add_external_id(h.connection, old, ExternalId("work", "anilist", "11"), origin="provider")
    add_external_id(h.connection, new, ExternalId("work", "anilist", "12"), origin="provider")
    assert detect(h) == []


@pytest.mark.req("EF-BIB-029")
def test_a_purged_work_is_never_proposed(h: Harness) -> None:
    old, new = moved(h)
    add_external_id(h.connection, new, ExternalId("work", "anilist", "12"), origin="provider")
    assert detect(h, Screen(("anilist", "12"))) == []
    assert old and rows(h) == []


class _Screens:
    def open(self) -> Screen:
        return NO_SCREEN


class _Ops:
    def __init__(self, dedup: DedupOps) -> None:
        self.dedup = dedup


@pytest.mark.req("EF-BIB-029")
def test_relink_ui_detects_suwayomi_migration(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """The API volet: the admin scan route finds the migration and the list route shows it as
    "Same work?"; the Works are the same two afterwards, and a visitor reaches neither."""
    with database() as path:
        engine = create_db_engine(path)
        try:
            with engine.begin() as connection:
                old, new = moved(Harness(connection, SupportCatalog(connection)))
            ops = _Ops(DedupOps(engine, _Screens()))
            with auth_env(monkeypatch, settings, ops_services=ops) as api:
                scan = "/api/v1/merge-suggestions/works/scan"
                assert api.client.post(scan).status_code == 401
                api.login()
                assert len(api.post(scan).json()["created"]) == 1
                listed = api.client.get("/api/v1/merge-suggestions/works").json()["items"]
            assert [(s["reason"], s["state"]) for s in listed] == [(SAME_WORK, "open")]
            assert {listed[0]["a"], listed[0]["b"]} == {old, new}
            with engine.connect() as connection:
                assert connection.execute(sa.text("SELECT count(*) FROM work")).scalar_one() == 2
        finally:
            engine.dispose()
