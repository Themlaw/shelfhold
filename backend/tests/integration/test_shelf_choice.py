"""L33 (EF-EXP-002, EF-UX-007): the shelf chosen per Edition with a default from the kind, one
Edition per remote library, the pre-created shelves and the ceiling of "Famille" (13+ at most).

Every refusal of the lot has a test that tries to break it: the default of a kind never reaches
a family shelf for an Edition nobody rated or rated above the ceiling, the cap of "Famille" holds
at creation and at modification, and a second shelf aiming at the same remote library never gets
an Edition the library already holds under another shelf, even with two writers at once."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.precreate import AdoptedLibrary, precreate_shelves
from shelfhold.export.application.shelves import ShelfService
from shelfhold.export.domain.bindings import FamilyCeilingTooHighError
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from tests.auth_env import auth_env
from tests.export_env import ExportEnv, Series, add_second_shelf
from tests.family_env import NOW, family_env, rate, rate_edition


def shelf_of(env: ExportEnv, edition_id: str) -> str | None:
    with env.engine.connect() as connection:
        return connection.execute(
            sa.select(catalog.edition.c.shelf_id).where(catalog.edition.c.id == edition_id)
        ).scalar_one()


def make_family(env: ExportEnv, ceiling: int = 13) -> str:
    return ShelfService(env.engine).create_shelf(
        name="Famille", phrase="Pour les enfants", max_age_rating=ceiling
    )


def default_kind_to(env: ExportEnv, shelf_name: str, kind: str = "manga") -> None:
    with env.engine.begin() as connection:
        connection.execute(
            sa.update(catalog.work_kind)
            .where(catalog.work_kind.c.key == kind)
            .values(default_shelf=shelf_name)
        )


def unshelved(env: ExportEnv) -> Series:
    series = env.new_series()
    with env.engine.begin() as connection:
        connection.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.id == series.edition_id)
            .values(shelf_id=None)
        )
    return series


def own_series_in_library(env: ExportEnv, edition_id: str, destination_id: str) -> None:
    """The Edition already has a series in the remote library, made by that destination."""
    series_id = new_ulid()
    with env.engine.begin() as connection:
        connection.execute(
            sa.insert(export.export_series).values(
                id=series_id, shelf_destination_id=destination_id,
                remote_library_id=env.remote_library_id, normalized_name=f"s-{series_id}",
                name_raw="Dr Stone",
            )
        )  # fmt: skip
        connection.execute(
            sa.insert(export.edition_export_series).values(
                edition_id=edition_id, remote_library_id=env.remote_library_id,
                export_series_id=series_id, assigned_at=NOW, assigned_by="first_export",
            )
        )  # fmt: skip


@contextmanager
def two_shelves_one_library() -> Iterator[tuple[ExportEnv, str, str, str]]:
    with family_env(18) as env:
        other, _destination = add_second_shelf(env, max_age_rating=None)
        yield env, env.shelf_id, other, env.destination_id


# -- the shelf is chosen per Edition, the default comes from the kind -------------------------


@pytest.mark.req("EF-EXP-002")
def test_shelf_chosen_per_edition_default_from_kind() -> None:
    with two_shelves_one_library() as (env, adults, other, _dest):
        default_kind_to(env, "Adultes 2")
        service = ShelfService(env.engine)
        first, second = unshelved(env), unshelved(env)
        assert service.proposed_shelf(first.edition_id) == other  # the default of the kind
        assert service.file_default(first.edition_id).filed
        assert shelf_of(env, first.edition_id) == other
        assert service.file_edition(second.edition_id, adults).filed  # the choice is per Edition
        assert shelf_of(env, second.edition_id) == adults
        assert shelf_of(env, first.edition_id) == other  # the other Edition did not move


@pytest.mark.req("EF-EXP-002")
def test_default_never_replaces_an_edition_already_shelved() -> None:
    with two_shelves_one_library() as (env, adults, other, _dest):
        default_kind_to(env, "Adultes 2")
        series = env.new_series()  # already on `adults`
        result = ShelfService(env.engine).file_default(series.edition_id)
        assert not result.filed and result.reason == "already_shelved"
        assert shelf_of(env, series.edition_id) == adults != other


@pytest.mark.req("EF-EXP-002")
def test_default_without_a_shelf_for_the_kind_files_nothing() -> None:
    with family_env(18) as env:
        series = unshelved(env)
        result = ShelfService(env.engine).file_default(series.edition_id)
        assert not result.filed and result.reason == "no_default_shelf"
        assert shelf_of(env, series.edition_id) is None


@pytest.mark.req("EF-EXP-002", "EF-BIB-055")
def test_family_default_from_kind_never_takes_an_unrated_edition() -> None:
    with family_env(18) as env:
        make_family(env)
        default_kind_to(env, "Famille")
        series = unshelved(env)  # nobody rated it
        service = ShelfService(env.engine)
        assert service.proposed_shelf(series.edition_id) is None
        assert not service.file_default(series.edition_id).filed
        assert shelf_of(env, series.edition_id) is None


@pytest.mark.req("EF-EXP-002", "EF-BIB-055")
def test_family_default_from_kind_never_takes_an_edition_above_the_ceiling() -> None:
    with family_env(18) as env:
        family = make_family(env)
        default_kind_to(env, "Famille")
        service = ShelfService(env.engine)
        above = unshelved(env)
        rate(env, above, 16)
        assert service.proposed_shelf(above.edition_id) is None
        assert not service.file_default(above.edition_id).filed
        assert shelf_of(env, above.edition_id) is None
        adult_x = unshelved(env)
        rate(env, adult_x, 18, x=True)
        assert not service.file_default(adult_x.edition_id).filed
        fine = unshelved(env)
        rate(env, fine, 10)
        assert service.file_default(fine.edition_id).filed
        assert shelf_of(env, fine.edition_id) == family


@pytest.mark.req("EF-EXP-002", "EF-BIB-055")
def test_family_default_does_not_take_a_work_rated_but_an_unrated_edition() -> None:
    with family_env(18) as env:
        make_family(env)
        default_kind_to(env, "Famille")
        series = unshelved(env)
        rate(env, series, 10)
        from tests.family_env import add_edition_to

        later = add_edition_to(env, series)  # a new Edition of a rated Work, nobody rated it
        assert not ShelfService(env.engine).file_default(later).filed
        assert shelf_of(env, later) is None
        rate_edition(env, later, 10)
        assert ShelfService(env.engine).file_default(later).filed


# -- Famille: 13+ at most, at creation and at modification --------------------------------------


@pytest.mark.req("EF-UX-007", "EF-EXP-047")
@pytest.mark.parametrize("ceiling", [16, 18, 99, None])
def test_family_shelf_capped_13_plus_at_creation(ceiling: int | None) -> None:
    with family_env(18) as env:
        service = ShelfService(env.engine)
        with pytest.raises(FamilyCeilingTooHighError):
            service.create_shelf(name="Famille", phrase="f", max_age_rating=ceiling)
        assert [r.name for r in env.rows(export.shelf)].count("Famille") == 0  # nothing stored
        for ok in (0, 10, 12, 13):
            name = "Famille"
            sid = service.create_shelf(name=name, phrase="f", max_age_rating=ok)
            with env.engine.begin() as connection:
                connection.execute(sa.delete(export.shelf).where(export.shelf.c.id == sid))


@pytest.mark.req("EF-UX-007", "EF-EXP-047")
@pytest.mark.parametrize("ceiling", [16, 18, 99, None])
def test_family_shelf_capped_13_plus_at_modification(ceiling: int | None) -> None:
    with family_env(18) as env:
        family = make_family(env)
        service = ShelfService(env.engine)
        with pytest.raises(FamilyCeilingTooHighError):
            service.set_max_age(family, ceiling)
        with env.engine.connect() as connection:
            stored = connection.execute(
                sa.select(export.shelf.c.max_age_rating).where(export.shelf.c.id == family)
            ).scalar_one()
        assert stored == 13  # unchanged
        service.set_max_age(family, 10)  # lowering is always allowed


@pytest.mark.req("EF-UX-007", "EF-EXP-047")
def test_cap_of_family_is_not_applied_to_other_shelves() -> None:
    with family_env(18) as env:
        service = ShelfService(env.engine)
        service.create_shelf(name="Adultes bis", phrase="a", max_age_rating=None)
        other = service.create_shelf(name="Ados", phrase="a", max_age_rating=16)
        service.set_max_age(other, 18)


@pytest.mark.req("EF-UX-007", "EF-EXP-047")
def test_family_cap_routes_refuse_above_13_on_create_and_modify(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as api:
        assert api.login().status_code == 200
        refused = api.post(
            "/api/v1/shelves", json={"name": "Famille", "phrase": "f", "max_age_rating": 16}
        )
        assert (refused.status_code, refused.json()["code"]) == (
            422,
            "shelf.family_ceiling_too_high",
        )
        none = api.post("/api/v1/shelves", json={"name": "Famille", "phrase": "f"})
        assert none.status_code == 422
        made = api.post(
            "/api/v1/shelves", json={"name": "Famille", "phrase": "f", "max_age_rating": 13}
        )
        assert made.status_code == 201
        url = f"/api/v1/shelves/{made.json()['id']}/max-age"
        for ceiling in (16, 18, 99, None):
            above = api.post(url, json={"max_age_rating": ceiling})
            assert (above.status_code, above.json()["code"]) == (
                422,
                "shelf.family_ceiling_too_high",
            )
        assert api.post(url, json={"max_age_rating": 12}).status_code == 200


# -- one Edition per remote library ---------------------------------------------------------------


@pytest.mark.req("EF-EXP-002")
def test_one_edition_per_remote_library() -> None:
    """Collision: the library already holds the Edition under the first shelf, the second shelf
    aims at the same library and is refused."""
    with two_shelves_one_library() as (env, adults, other, destination):
        series = env.new_series()
        own_series_in_library(env, series.edition_id, destination)
        service = ShelfService(env.engine)
        refused = service.file_edition(series.edition_id, other)
        assert not refused.filed and refused.reason == "remote_library_taken"
        assert shelf_of(env, series.edition_id) == adults
        assert service.file_edition(series.edition_id, adults).filed  # its own shelf: fine
        free = env.new_series()  # another Edition, nothing in the library: the other shelf is fine
        assert service.file_edition(free.edition_id, other).filed


@pytest.mark.req("EF-EXP-002")
def test_the_default_of_a_kind_never_bypasses_the_library_rule() -> None:
    with two_shelves_one_library() as (env, _adults, other, destination):
        default_kind_to(env, "Adultes 2")
        series = unshelved(env)
        own_series_in_library(env, series.edition_id, destination)
        result = ShelfService(env.engine).file_default(series.edition_id)
        assert not result.filed and result.reason == "remote_library_taken"
        assert shelf_of(env, series.edition_id) is None != other


@pytest.mark.req("EF-EXP-002")
def test_one_edition_per_remote_library_under_two_simultaneous_writers() -> None:
    """Two writers file the same Edition at once onto two shelves aiming at the library that
    holds it under a third: both are refused, and the Edition stays where it was."""
    with family_env(18) as env:
        second, _d2 = add_second_shelf(env, max_age_rating=None)
        third_id, third_destination = new_ulid(), new_ulid()
        with env.engine.begin() as connection:
            connection.execute(
                sa.insert(export.shelf).values(id=third_id, name="Trois", phrase="t")
            )
            connection.execute(
                sa.insert(export.shelf_destination).values(
                    id=third_destination, shelf_id=third_id, target_id=env.target_id,
                    transport="tree", egress="foyer", root_subpath="Kavita3",
                    template="x", template_origin="default@1", kavita_library_type="manga",
                )
            )  # fmt: skip
            connection.execute(
                sa.insert(export.shelf_destination_remote_library).values(
                    shelf_destination_id=third_destination,
                    remote_library_id=env.remote_library_id,
                    export_target_id=env.target_id,
                )
            )
        series = env.new_series()
        own_series_in_library(env, series.edition_id, env.destination_id)
        service = ShelfService(env.engine)
        barrier = threading.Barrier(2)
        results: list[object] = []

        def writer(shelf_id: str) -> None:
            barrier.wait()
            results.append(service.file_edition(series.edition_id, shelf_id))

        threads = [threading.Thread(target=writer, args=(s,)) for s in (second, third_id)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(results) == 2  # no writer died on a locked database
        assert [r.filed for r in results] == [False, False]  # type: ignore[attr-defined]
        assert shelf_of(env, series.edition_id) == env.shelf_id


@pytest.mark.req("EF-EXP-002")
def test_two_simultaneous_filings_leave_one_shelf_and_one_audit_each() -> None:
    with two_shelves_one_library() as (env, adults, other, _dest):
        series = unshelved(env)
        service = ShelfService(env.engine)
        barrier = threading.Barrier(2)
        results: list[object] = []

        def writer(shelf_id: str) -> None:
            barrier.wait()
            results.append(service.file_edition(series.edition_id, shelf_id))

        threads = [threading.Thread(target=writer, args=(s,)) for s in (adults, other)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(results) == 2 and all(r.filed for r in results)  # type: ignore[attr-defined]
        assert shelf_of(env, series.edition_id) in (adults, other)
        with env.engine.connect() as connection:
            audited = connection.execute(
                sa.text("SELECT count(*) FROM audit WHERE action = 'edition.file'")
            ).scalar_one()
        assert audited == 2


# -- the pre-created shelves ----------------------------------------------------------------------


def shelves_by_name(env: ExportEnv) -> dict[str, sa.Row[tuple[object, ...]]]:
    return {r.name: r for r in env.rows(export.shelf)}


@pytest.mark.req("EF-UX-007", "EF-EXP-002")
def test_precreated_shelves_include_family_capped_13_and_say_it_in_plain_words() -> None:
    with family_env(18) as env:
        made = precreate_shelves(env.engine)
        assert {"Manga", "Famille"} <= set(made.created)
        by_name = shelves_by_name(env)
        assert by_name["Famille"].max_age_rating == 13
        assert by_name["Famille"].hold_until_rated is True
        assert by_name["Manga"].phrase == "Les mangas vont dans Kavita \u203a Manga"
        assert by_name["Manga"].max_age_rating is None
        kinds = {r.key: r.default_shelf for r in env.rows(catalog.work_kind)}
        assert kinds["manga"] == "Manga"
        assert "Famille" not in kinds.values()  # a kind never defaults to the family shelf


@pytest.mark.req("EF-UX-007", "EF-EXP-002")
def test_precreation_is_idempotent_and_never_overwrites_a_shelf() -> None:
    with family_env(18) as env:
        precreate_shelves(env.engine)
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(export.shelf)
                .where(export.shelf.c.name == "Manga")
                .values(phrase="Mon texte", max_age_rating=16, hold_until_rated=True,
                        retention_mode="keep", allowed_profiles=["x"])
            )  # fmt: skip
            connection.execute(
                sa.update(export.shelf)
                .where(export.shelf.c.name == "Famille")
                .values(max_age_rating=10, allowed_profiles=["kid"])
            )
            connection.execute(
                sa.update(catalog.work_kind)
                .where(catalog.work_kind.c.key == "manhwa")
                .values(default_shelf="Mes manhwas")
            )
        before = [tuple(r) for r in env.rows(export.shelf)]
        again = precreate_shelves(env.engine)
        assert again.created == ()
        assert [tuple(r) for r in env.rows(export.shelf)] == before
        kinds = {r.key: r.default_shelf for r in env.rows(catalog.work_kind)}
        assert kinds["manhwa"] == "Mes manhwas"  # a default already set is never replaced


@pytest.mark.req("EF-UX-007")
def test_precreation_keeps_an_existing_family_shelf_even_above_the_cap() -> None:
    """A shelf that exists (made before the cap, by hand) is never rewritten: not by precreation."""
    with family_env(18) as env:
        with env.engine.begin() as connection:
            connection.execute(
                sa.insert(export.shelf).values(
                    id=new_ulid(), name="Famille", phrase="vieux", max_age_rating=18
                )
            )
        precreate_shelves(env.engine)
        assert shelves_by_name(env)["Famille"].max_age_rating == 18


@pytest.mark.req("EF-UX-007", "EF-EXP-002")
def test_precreated_shelves_one_per_adopted_kavita_library() -> None:
    with family_env(18) as env:
        libraries = (
            AdoptedLibrary("Light novels", "light_novel"),
            AdoptedLibrary("Mes BD", "comic"),
            AdoptedLibrary("Famille", "manga"),
        )
        made = precreate_shelves(env.engine, adopted=libraries)
        by_name = shelves_by_name(env)
        for library in libraries:
            assert library.name in by_name
        assert (
            by_name["Mes BD"].phrase == "Les contenus de « Mes BD » vont dans Kavita \u203a Mes BD"
        )
        assert by_name["Famille"].max_age_rating == 13  # a library named so gets the cap
        assert by_name["Light novels"].max_age_rating is None
        assert "Mes BD" in made.created
        count = len(env.rows(export.shelf))
        precreate_shelves(env.engine, adopted=libraries)
        assert len(env.rows(export.shelf)) == count  # one shelf per library, never two


@pytest.mark.req("EF-UX-007")
def test_precreation_runs_twice_at_once_without_a_duplicate_or_an_error() -> None:
    with family_env(18) as env:
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def run() -> None:
            barrier.wait()
            try:
                precreate_shelves(env.engine)
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []
        names = [r.name for r in env.rows(export.shelf)]
        assert len(names) == len(set(names)) and "Famille" in names


@pytest.mark.req("EF-UX-007", "EF-EXP-002")
def test_routes_precreate_and_file_by_default(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as api:
        assert api.client.post("/api/v1/shelves/precreate").status_code == 401
        assert api.login().status_code == 200
        done = api.post("/api/v1/shelves/precreate", json={})
        assert done.status_code == 201 and "Famille" in done.json()["created"]
        again = api.post("/api/v1/shelves/precreate", json={})
        assert again.status_code == 201 and again.json()["created"] == []
        custom = api.post(
            "/api/v1/shelves/precreate",
            json={"adopted": [{"name": "Famille 2", "kind": "novel"}]},
        )
        assert custom.json()["created"] == ["Famille 2"]
