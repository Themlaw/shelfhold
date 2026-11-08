"""L10 (EF-EXP-002, EF-EXP-047, EF-USR-015, ENF-SEC-010): creating, binding and changing a family
shelf through the service and the real routes. The audit entry is written in the transaction
that stores the shelf, a key never changes a ceiling, and a destination that does not partition
access is refused before anything is stored."""

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.shelves import ShelfService
from shelfhold.export.domain.bindings import (
    ShelfCeilingError,
    UnpartitionedDestinationError,
)
from shelfhold.headless.actor import Actor
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from tests.auth_env import AuthEnv, auth_env, db_rows
from tests.family_env import NOW, family_env, rate
from tests.integration.test_auth_keys_reserved import _bearer, _create_key

ACTOR = Actor("01HZZZZZZZZZZZZZZZZZZZZZZA", "01HZZZZZZZZZZZZZZZZZZZZZZS", NOW)


def audited(env_engine: sa.Engine, action: str) -> list[sa.Row[tuple[object, ...]]]:
    with env_engine.connect() as connection:
        return list(
            connection.execute(sa.text("SELECT * FROM audit WHERE action = :a"), {"a": action})
        )


@pytest.mark.req("EF-EXP-002", "EF-USR-015", "ENF-SEC-010")
def test_family_shelf_created_with_its_ceiling_hold_default_and_audit() -> None:
    with family_env(18) as env:
        service = ShelfService(env.engine)
        shelf_id = service.create_shelf(
            name="Famille", phrase="Pour les enfants", max_age_rating=13, actor=ACTOR
        )
        with env.engine.connect() as connection:
            row = connection.execute(
                sa.select(export.shelf).where(export.shelf.c.id == shelf_id)
            ).one()
        assert row.max_age_rating == 13
        assert row.hold_until_rated is True  # on by default for a shelf below 18+ (EF-EXP-050)
        (entry,) = audited(env.engine, "shelf.create")
        assert entry.actor_id == ACTOR.actor_id and shelf_id in entry.target
        adult = service.create_shelf(name="Adultes 2", phrase="Adultes", max_age_rating=18)
        with env.engine.connect() as connection:
            held = connection.execute(
                sa.select(export.shelf.c.hold_until_rated).where(export.shelf.c.id == adult)
            ).scalar_one()
        assert held is False
        with pytest.raises(ShelfCeilingError):
            service.create_shelf(name="Odd", phrase="odd", max_age_rating=7)
        assert len(audited(env.engine, "shelf.create")) == 2  # nothing stored for the refusal


@pytest.mark.req("EF-EXP-047")
def test_family_shelf_to_unpartitioned_destination_refused() -> None:
    with family_env(13) as env:
        service = ShelfService(env.engine)
        shelf_id = service.create_shelf(name="Famille", phrase="f", max_age_rating=13)
        drop = new_ulid()
        with env.engine.begin() as connection:
            connection.execute(
                sa.insert(export.export_target).values(
                    id=drop,
                    kind="drop",
                    name="CWA",
                    access_partitioning="none",
                    path_map=[{"local": str(env.lib.manga), "remote": "/ingest", "mode": "rw"}],
                )
            )
        with pytest.raises(UnpartitionedDestinationError) as refused:
            service.bind_destination(
                shelf_id=shelf_id,
                target_id=drop,
                transport="tree",
                plugin_egress="foyer",
                root_subpath="Famille",
            )
        assert "none" in str(refused.value)
        with env.engine.connect() as connection:
            bound = connection.execute(
                sa.select(sa.func.count())
                .select_from(export.shelf_destination)
                .where(export.shelf_destination.c.shelf_id == shelf_id)
            ).scalar_one()
        assert bound == 0  # nothing stored
        adult = service.create_shelf(name="Adultes 3", phrase="a", max_age_rating=18)
        service.bind_destination(
            shelf_id=adult,
            target_id=drop,
            transport="tree",
            plugin_egress="foyer",
            root_subpath="Adultes",
        )  # an adult shelf may use any destination
        with pytest.raises(UnpartitionedDestinationError):
            service.set_max_age(adult, 10, actor=ACTOR)  # nor can it be lowered onto one
        with env.engine.connect() as connection:
            kept = connection.execute(
                sa.select(export.shelf.c.max_age_rating).where(export.shelf.c.id == adult)
            ).scalar_one()
        assert kept == 18


@pytest.mark.req("EF-USR-015", "EF-BIB-037")
def test_lowering_a_shelf_returns_the_removal_plan_and_audits_in_the_transaction() -> None:
    with family_env(16) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 16)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        service = ShelfService(env.engine)
        lowered = service.set_max_age(env.shelf_id, 10, actor=ACTOR)
        assert [i.edition_id for i in lowered.withdrawals.items] == [series.edition_id]
        assert len(env.files()) == 1  # a plan; nothing leaves before it is applied
        (entry,) = audited(env.engine, "shelf.set_ceiling")
        assert "16" in entry.target and "10" in entry.target
        profiles = service.set_allowed_profiles(env.shelf_id, ["kid-1", "kid-2"], actor=ACTOR)
        assert profiles == ["kid-1", "kid-2"]
        assert len(audited(env.engine, "shelf.set_profiles")) == 1
        with env.engine.connect() as connection:
            stored = connection.execute(
                sa.select(export.shelf.c.allowed_profiles).where(export.shelf.c.id == env.shelf_id)
            ).scalar_one()
        assert stored == ["kid-1", "kid-2"]


@pytest.mark.req("EF-EXP-002", "EF-USR-015", "EF-BIB-055")
def test_choosing_a_family_shelf_for_an_edition_goes_through_the_admission() -> None:
    with family_env(13) as env:
        service = ShelfService(env.engine)
        series = env.new_series()
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(catalog.edition)
                .where(catalog.edition.c.id == series.edition_id)
                .values(shelf_id=None)
            )
        refused = service.file_edition(series.edition_id, env.shelf_id, actor=ACTOR)
        assert refused.filed is False and refused.reason == "unrated_edition"
        rate(env, series, 10)
        filed = service.file_edition(series.edition_id, env.shelf_id, actor=ACTOR)
        assert filed.filed is True
        assert len(audited(env.engine, "edition.file")) == 1  # only the one that went in
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(export.shelf)
                .where(export.shelf.c.id == env.shelf_id)
                .values(name="Manga")
            )
            connection.execute(
                sa.update(catalog.work_kind)
                .where(catalog.work_kind.c.key == "manga")
                .values(default_shelf="Manga")
            )
        assert service.proposed_shelf(series.edition_id) == env.shelf_id  # the kind's default


@pytest.mark.req("EF-USR-015", "ENF-SEC-010")
def test_family_shelf_routes_are_reserved_audited_and_refuse_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as api:
        assert api.login().status_code == 200
        created = api.post(
            "/api/v1/shelves",
            json={"name": "Famille", "phrase": "Pour les enfants", "max_age_rating": 13},
        )
        assert created.status_code == 201, created.text
        shelf_id = created.json()["id"]
        assert len(db_rows(settings, "SELECT 1 FROM audit WHERE action = ?", "shelf.create")) == 1
        refused = api.post(
            "/api/v1/shelves", json={"name": "X", "phrase": "x", "max_age_rating": 7}
        )
        assert refused.status_code == 422 and refused.json()["code"] == "shelf.ceiling_invalid"
        lowered = api.post(f"/api/v1/shelves/{shelf_id}/max-age", json={"max_age_rating": 10})
        assert lowered.status_code == 200, lowered.text
        assert lowered.json()["withdrawals"] == []
        profiles = api.post(
            f"/api/v1/shelves/{shelf_id}/allowed-profiles", json={"profiles": ["kid"]}
        )
        assert profiles.status_code == 200 and profiles.json()["profiles"] == ["kid"]
        # P3: once a restricted profile exists, these need a fresh proof
        assert _fresh_required_once_restricted(api)
        # and an API key, even an admin one, is refused on every one of them (EF-USR-024)
        key = _create_key(monkeypatch, capsys, api.settings)
        before = len(db_rows(settings, "SELECT 1 FROM audit WHERE action LIKE 'shelf.%'"))
        for method, path, body in (
            ("post", "/api/v1/shelves", {"name": "K", "phrase": "k", "max_age_rating": 99}),
            ("post", f"/api/v1/shelves/{shelf_id}/max-age", {"max_age_rating": 99}),
            ("post", f"/api/v1/shelves/{shelf_id}/allowed-profiles", {"profiles": []}),
            ("post", "/api/v1/editions/E/shelf", {"shelf_id": shelf_id}),
            ("get", "/api/v1/shelves/withdrawals", None),
            ("post", "/api/v1/shelves/withdrawals", {"if_match": "x"}),
        ):
            other = api.other_client("10.0.0.9")  # no session cookie: only the key speaks
            kwargs = {"json": body} if body is not None else {}
            answer = getattr(other, method)(path, headers=_bearer(key), **kwargs)
            assert answer.status_code in (401, 403), (path, answer.status_code)
        assert len(db_rows(settings, "SELECT 1 FROM audit WHERE action LIKE 'shelf.%'")) == before


def _fresh_required_once_restricted(api: AuthEnv) -> bool:
    from shelfhold.identity.public import RESERVED_ACTIONS

    names = ("shelf.max_age_rating", "shelf.allowed_profiles", "shelf.choose_family",
             "shelf.withdraw")  # fmt: skip
    return all(
        RESERVED_ACTIONS[n].needs_fresh_auth(restricted_profile_exists=True, hardened=False)
        and not RESERVED_ACTIONS[n].needs_fresh_auth(
            restricted_profile_exists=False, hardened=False
        )
        for n in names
    )
