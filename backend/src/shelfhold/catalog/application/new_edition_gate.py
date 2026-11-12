"""The gate of the new Edition (EF-BIB-055, EF-BIB-037, passe 11 L4): the one door that files an
Edition that arrives on a shelf.

Every road by which an Edition reaches a Work goes through here: the ingest of a source, an
attach by external id and its undo, `relink`. On a shelf below 18+ the admission of
`RatingService` decides, and what it refuses is not filed: the Edition stays unfiled and waits in
"À classer" (`to_rate`). It enters only after an admin rates it (`rate_edition`) or rates the Work
that holds it, and is then filed explicitly (`ShelfService.file_edition`, the same admission).

One exception, asked for by name: a Work nobody rated at all may wait *as a whole* on the shelf
of its follow or of its adopted library ("retenir tant que non classé", EF-EXP-050, EF-EXP-048).
That Edition is held, never admitted: the barrier of the planner refuses it until the Work is
rated. An Edition new to a Work that was rated is never held, whatever the caller asks.

The caller owns the transaction.
"""

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.catalog.application.rating import RatingService
from shelfhold.catalog.domain.age import UNRATED_FLOOR, Admission, ceiling_of
from shelfhold.infrastructure.schema import catalog, export

__all__ = ["Filing", "file_new_edition"]


@dataclass(frozen=True)
class Filing:
    shelf_id: str | None  # where the Edition is filed now; `None`: unfiled
    waiting: bool  # it waits for a rating ("À classer")
    reason: str | None = None  # the `Admission` value that kept it out, or held it


def _family(connection: Connection, shelf_id: str) -> bool:
    ceiling = connection.execute(
        sa.select(export.shelf.c.max_age_rating).where(export.shelf.c.id == shelf_id)
    ).one()[0]
    age = ceiling_of(ceiling)
    return age is not None and age < UNRATED_FLOOR


def _work_rated(connection: Connection, edition_id: str) -> bool:
    work, edition = catalog.work, catalog.edition
    rated_at = connection.execute(
        sa.select(work.c.rated_at)
        .select_from(edition.join(work, work.c.id == edition.c.work_id))
        .where(edition.c.id == edition_id)
    ).scalar_one()
    return rated_at is not None


def _set(connection: Connection, edition_id: str, shelf_id: str | None) -> None:
    connection.execute(
        sa.update(catalog.edition)
        .where(catalog.edition.c.id == edition_id)
        .values(shelf_id=shelf_id)
    )


def file_new_edition(
    connection: Connection,
    edition_id: str,
    shelf_id: str | None,
    *,
    hold_unrated_work: bool = False,
    rating: RatingService | None = None,
    keep_filed: bool = False,
) -> Filing:
    """File an Edition that arrives on `shelf_id` (`None`: unfiled), if the shelf takes it.

    A shelf of 18+ or without ceiling takes it at once. Below 18+, only an Edition the admission
    allows (rated by an admin, under the ceiling, no suggested age above) is filed; any other one
    is left unfiled and waits. `hold_unrated_work` lets an Edition of a Work nobody rated wait on
    the shelf instead (the planner still refuses it); it never applies to a rated Work.

    `keep_filed`: a book joins an Edition that may already sit on a shelf. That Edition is never
    moved, whatever the admission says: the withdrawal of a shelf below 18+ is never automatic
    (a reserved action, `shelf withdraw`). The refusal is only reported (`waiting`, `reason`): the
    Edition shows in `status` as `withdrawal_pending` and the export refuses it.
    """
    if keep_filed:
        current = connection.execute(
            sa.select(catalog.edition.c.shelf_id).where(catalog.edition.c.id == edition_id)
        ).scalar()
        if current is not None:
            if _family(connection, current):
                verdict = (rating or RatingService()).admission_of(
                    connection, edition_id, shelf_id=current
                )
                if verdict is not Admission.ALLOWED:
                    return Filing(current, waiting=True, reason=verdict.value)
            return Filing(current, waiting=False)
    if shelf_id is None:
        _set(connection, edition_id, None)
        return Filing(None, waiting=False)
    if not _family(connection, shelf_id):
        _set(connection, edition_id, shelf_id)
        return Filing(shelf_id, waiting=False)
    verdict = (rating or RatingService()).admission_of(connection, edition_id, shelf_id=shelf_id)
    if verdict is Admission.ALLOWED:
        _set(connection, edition_id, shelf_id)
        return Filing(shelf_id, waiting=False)
    if (
        hold_unrated_work
        and verdict is Admission.UNRATED_EDITION
        and not _work_rated(connection, edition_id)
    ):
        _set(connection, edition_id, shelf_id)
        return Filing(shelf_id, waiting=True, reason=verdict.value)
    _set(connection, edition_id, None)
    return Filing(None, waiting=True, reason=verdict.value)
