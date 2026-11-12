"""A migrated database with works, titles and credited names for the search tests (M3 L17)."""

import sqlalchemy as sa

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog
from tests.catalog_env import Harness


def credit(h: Harness, work_id: str, *names: tuple[str, str, str]) -> str:
    """One contributor credited on the Work, known by `(name, lang, kind)` each."""
    contributor_id = new_ulid()
    h.connection.execute(
        sa.insert(catalog.contributor).values(
            id=contributor_id, kind="person", primary_name=names[0][0]
        )
    )
    for name, lang, kind in names:
        h.connection.execute(
            sa.insert(catalog.contributor_name).values(
                contributor_id=contributor_id, name=name, lang=lang, kind=kind
            )
        )
    h.connection.execute(
        sa.insert(catalog.contribution).values(
            contributor_id=contributor_id, entity_type="work", entity_id=work_id,
            role="aut", position=0,
        )
    )  # fmt: skip
    return contributor_id


def alt_title(h: Harness, work_id: str, title: str, lang: str = "und") -> None:
    h.connection.execute(
        sa.insert(catalog.work_title).values(work_id=work_id, title=title, lang=lang, kind="alt")
    )


def rate(h: Harness, work_id: str, age: int | None, *, adult: bool = False) -> None:
    h.connection.execute(
        sa.update(catalog.work)
        .where(catalog.work.c.id == work_id)
        .values(age_rating=age, is_adult=adult)
    )


def index_rows(h: Harness) -> list[tuple[object, ...]]:
    """Both index tables, whole, in a stable order: what `rebuild` must reproduce."""
    rows: list[tuple[object, ...]] = []
    for table in ("search_fts", "search_trigram"):
        rows += [
            (table, *r)
            for r in h.connection.execute(
                sa.text(
                    f"SELECT entity_type, entity_id, digest, title, names FROM {table} "  # noqa: S608
                    "ORDER BY entity_id"
                )
            )
        ]
    return rows
