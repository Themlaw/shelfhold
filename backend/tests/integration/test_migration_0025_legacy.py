"""Rows the old `rate(work)` left behind, once migration 0025 ran and the Work is lowered with the
new code: the Edition an X suggestion was set on never enters the shelf of the children."""

import importlib
from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from shelfhold.catalog.domain.age import Admission
from shelfhold.infrastructure.schema import catalog
from shelfhold.sdk.age import AgeRating
from tests.catalog_env import Harness, harness
from tests.integration.test_rating_service import (
    NOW,
    SERVICE,
    X,
    admin,
    shelf,
    suggest,
)


@pytest.fixture
def h() -> Iterator[Harness]:
    with harness() as inside:
        yield inside


pytestmark = pytest.mark.req("EF-BIB-055")

MIGRATION = importlib.import_module(
    "shelfhold.infrastructure.migrations.versions.0025_reopen_edition_age_suggestions"
)


def _legacy(
    h: Harness,
    state: str,
    work_age: AgeRating,
) -> Admission:
    edition = h.edition()
    family = shelf(h, 10)
    suggest(h, edition.id, X, "sub_source")
    SERVICE.rate(h.connection, edition.work_id, work_age, principal=admin(), now=NOW)
    h.connection.execute(  # what the code before the fix wrote
        sa.update(catalog.age_suggestion)
        .where(catalog.age_suggestion.c.entity_id == edition.id)
        .values(state=state)
    )
    statement = next(
        c for c in MIGRATION.upgrade.__code__.co_consts
        if isinstance(c, str) and "UPDATE age_suggestion" in c
    )  # fmt: skip
    h.connection.execute(sa.text(statement))
    SERVICE.rate(h.connection, edition.work_id, AgeRating(10), principal=admin(), now=NOW)
    return SERVICE.admission_of(h.connection, edition.id, shelf_id=family)


@pytest.mark.parametrize(
    ("state", "work_age"),
    [("accepted", X), ("rejected", X), ("rejected", AgeRating(10)), ("accepted", AgeRating(10))],
)
def test_legacy_rows_keep_blocking_after_the_migration(
    h: Harness, state: str, work_age: AgeRating
) -> None:
    assert _legacy(h, state, work_age) is Admission.SUGGESTION_ABOVE
