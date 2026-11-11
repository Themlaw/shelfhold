"""L12 (EF-BIB-008): provenance and lock per field, over the `FieldProposal` of the SDK (L11).

Each value of a field of an entity keeps where it comes from. A value an admin locked is never
replaced by a provider, whatever it proposes and however sure it says it is: the proposal is
ignored and the provenance of the locked value is kept.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.catalog.public import (
    FieldKey,
    lock_field,
    propose_fields,
    unlock_field,
)
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.schema import catalog, metadata
from shelfhold.sdk.metadata import FieldProposal, Provenance
from tests.integration.test_schema_catalog import add_edition, add_work

pytestmark = pytest.mark.req("EF-BIB-008")

NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(tmp_path / "catalog.db")
    metadata.create_all(engine)
    yield engine
    engine.dispose()


def proposal(field: str, value: object, *, entity: str = "work", by: str = "openlibrary",
             confidence: float = 0.9, lang: str | None = None) -> FieldProposal:  # fmt: skip
    return FieldProposal(
        entity_type=entity,  # type: ignore[arg-type]
        field=field,
        lang=lang,
        value=value,  # type: ignore[arg-type]
        confidence=confidence,
        provenance=Provenance(provider=by, fetched_at=NOW),
    )


def stored(connection: Connection, entity_id: str, field: str) -> tuple[object, ...]:
    table = catalog.metadata_field
    row = connection.execute(
        sa.select(
            table.c.value, table.c.provider, table.c.provenance, table.c.confidence,
            table.c.locked,
        ).where(table.c.entity_id == entity_id, table.c.field == field)
    ).one()  # fmt: skip
    return tuple(row)


def test_field_provenance_and_lock(engine: Engine) -> None:
    with engine.begin() as connection:
        work = add_work(connection)
        lock_field(connection, FieldKey("work", work, "title"), "Le Titre", now=NOW)
        report = propose_fields(
            connection,
            {"work": work},
            [proposal("title", "Another title", confidence=1.0), proposal("summary", "Un résumé")],
            now=NOW,
        )
        assert report.kept_locked == (FieldKey("work", work, "title"),)
        assert report.written == (FieldKey("work", work, "summary"),)
        # the locked value and its provenance are kept
        assert stored(connection, work, "title") == ('"Le Titre"', "admin", "manual", None, True)
        assert stored(connection, work, "summary") == (
            '"Un résumé"', "openlibrary", "provider", 0.9, False,
        )  # fmt: skip


def test_unlocked_field_takes_the_new_proposal_with_its_provenance(engine: Engine) -> None:
    with engine.begin() as connection:
        work = add_work(connection)
        propose_fields(connection, {"work": work}, [proposal("summary", "a", by="one")], now=NOW)
        again = propose_fields(
            connection, {"work": work}, [proposal("summary", "a", by="one")], now=NOW
        )
        assert again.written == ()  # the same value from the same provider: nothing written
        propose_fields(connection, {"work": work}, [proposal("summary", "b", by="two")], now=NOW)
        assert stored(connection, work, "summary") == ('"b"', "two", "provider", 0.9, False)
        lock_field(connection, FieldKey("work", work, "summary"), "c", now=NOW)
        propose_fields(connection, {"work": work}, [proposal("summary", "d", by="two")], now=NOW)
        assert stored(connection, work, "summary")[0] == '"c"'
        unlock_field(connection, FieldKey("work", work, "summary"))
        assert stored(connection, work, "summary") == ('"c"', "admin", "manual", None, False)
        propose_fields(connection, {"work": work}, [proposal("summary", "d", by="two")], now=NOW)
        assert stored(connection, work, "summary")[:2] == ('"d"', "two")


def test_proposals_go_to_the_entity_of_their_type_only(engine: Engine) -> None:
    """A proposal names its entity type; one the host did not ask about is not stored."""
    with engine.begin() as connection:
        work = add_work(connection)
        edition = add_edition(connection, work)
        report = propose_fields(
            connection,
            {"edition": edition},
            [
                proposal("title", "x"),  # about the Work: not asked
                proposal("publisher", "Ed.", entity="edition", lang="fr"),
            ],
            now=NOW,
        )
        assert report.written == (FieldKey("edition", edition, "publisher", "fr"),)
        assert report.skipped == 1
        table = catalog.metadata_field
        assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar() == 1
