"""L10: a family shelf (ceiling below 18+) over the synthetic export environment.

The shelf row, its Kavita destination and the pipeline are those of `export_env`; what a test adds
is the vocabulary of the parental barrier: rating a Work, a suggested age, and the Kavita version
the target was last seen at (a family shelf never trusts a version nobody read).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import sqlalchemy as sa

from shelfhold.catalog.public import RatingService
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.sdk.age import AgeRating
from tests.export_env import ExportEnv, Series, export_env

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
SAFE_KAVITA = "0.9.1.4"
RATING = RatingService()


def admin() -> Principal:
    return Principal(
        user_id=new_ulid(), username="admin", role="admin", credential=Credential.SESSION,
        session_id=new_ulid(), auth_time=NOW, can_rate=True,
    )  # fmt: skip


@contextmanager
def family_env(ceiling: int = 13, *, kavita: str | None = SAFE_KAVITA) -> Iterator[ExportEnv]:
    with export_env(max_age_rating=ceiling) as env:
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(export.export_target)
                .where(export.export_target.c.id == env.target_id)
                .values(version_seen=kavita)
            )
        yield env


def rate(env: ExportEnv, series: Series, age: int, *, x: bool = False) -> None:
    """The admin rates the Work: its Editions count as rated with it."""
    with env.engine.begin() as connection:
        RATING.rate(connection, series.work_id, AgeRating(age, x=x), principal=admin(), now=NOW)


def rate_edition(env: ExportEnv, edition_id: str, age: int, *, x: bool = False) -> None:
    with env.engine.begin() as connection:
        RATING.rate_edition(connection, edition_id, AgeRating(age, x=x), principal=admin(), now=NOW)


def suggest(env: ExportEnv, edition_id: str, age: int, *, x: bool = False) -> None:
    """A file, a sub-source or a provider suggests an age: never a rating."""
    with env.engine.begin() as connection:
        connection.execute(
            sa.insert(catalog.age_suggestion).values(
                id=new_ulid(), entity_type="edition", entity_id=edition_id, value=age, x=x,
                provenance="sub_source", seen_at=NOW,
            )
        )  # fmt: skip


def add_edition_to(env: ExportEnv, series: Series, lang: str = "en") -> str:
    """A later Edition of the same Work, filed on the shelf and never rated."""
    from tests.integration.test_schema_catalog import add_edition

    with env.engine.begin() as connection:
        return add_edition(connection, series.work_id, lang=lang, shelf_id=env.shelf_id)


def second_edition(env: ExportEnv, series: Series, lang: str = "en") -> Series:
    """Another Edition of the same Work, with its own binding, filed on the shelf."""
    from tests.integration.test_schema_catalog import (
        add_binding,
        add_edition,
        add_source_item,
    )

    with env.engine.begin() as connection:
        edition = add_edition(connection, series.work_id, lang=lang, shelf_id=env.shelf_id)
        item = add_source_item(connection, remote_id=f"series-{new_ulid()}")
        binding = add_binding(connection, series.work_id, edition, item)
    return Series(series.work_id, edition, binding)


def add_adult_shelf(env: ExportEnv, *, subpath: str = "Adultes") -> tuple[str, str]:
    """An adult shelf of the same Kavita with a library of its own: (shelf, destination)."""
    from shelfhold.export.domain.naming import DEFAULT_TEMPLATE

    shelf_id, destination_id, library_id = new_ulid(), new_ulid(), new_ulid()
    (env.lib.manga / subpath).mkdir(exist_ok=True)
    with env.engine.begin() as connection:
        connection.execute(
            sa.insert(export.shelf).values(
                id=shelf_id, name="Adultes", phrase="Adultes", max_age_rating=99
            )
        )
        connection.execute(
            sa.insert(export.shelf_destination).values(
                id=destination_id,
                shelf_id=shelf_id,
                target_id=env.target_id,
                transport="tree",
                egress="foyer",
                root_subpath=subpath,
                template=DEFAULT_TEMPLATE.dumps(),
                template_origin=f"default@{DEFAULT_TEMPLATE.version}",
                kavita_library_type="manga",
            )
        )
        connection.execute(
            sa.insert(export.remote_library).values(
                id=library_id, export_target_id=env.target_id, remote_ref="2"
            )
        )
        connection.execute(
            sa.insert(export.shelf_destination_remote_library).values(
                shelf_destination_id=destination_id,
                remote_library_id=library_id,
                export_target_id=env.target_id,
            )
        )
    return shelf_id, destination_id


def unrate(env: ExportEnv, edition_id: str) -> None:
    """The Edition is new since the last rating: `rated_at` was never set for it."""
    with env.engine.begin() as connection:
        connection.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.id == edition_id)
            .values(rated_at=None)
        )
