"""M4 milestone review, axis "parental" (contradicteur de jalon M4): chains of lots that bring
content nobody rated, or rated above a child, where a child reaches it.

Each case is red today (`xfail(strict=True)`) and turns green with the fix it names.

J-PAR-1. A source migration (L27) moves a rated Edition, filed on a shelf below 18+, to a
sub-source that says it is adult-only (`content_warning = "nsfw"`). Every other way content of a
source joins an Edition turns that word into an age suggestion (`content_warning_age`): the
discovery of a series, and the acquisition of a book that joins an Edition already on a shelf
("the export refuses the Edition, so the new file does not reach the shelf"). `relink`, which the
migration applies, writes none: the Edition stays admitted, and every chapter the new source
brings is written into the library of the children.

J-PAR-2. Adopting a foreign library (L36, EF-EXP-045) files each series it finds under the Edition
of the shelf that has the same series name (`Adopter._known_editions`), keeping that Edition's
rating. A file of the foreign library that nobody looked at, with no age in it, becomes a Unit of
an Edition rated for children; D-P3 (L39) opens the adult shelf to a child for every Edition rated
under her ceiling, so the catalogue, OPDS and kosync show it to her.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.public import SourceMigrations
from shelfhold.catalog.public import Admission, ListedUnit, RatingService, visible_unit_ids
from shelfhold.identity.domain.visibility import ViewerScope
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, export
from tests.adopt_env import adoption
from tests.export_env import export_env
from tests.family_env import rate
from tests.integration.test_schema_catalog import add_binding, add_source_item, add_variant
from tests.reading_env import ReadingEnv, reading_env

pytestmark = pytest.mark.req("EF-BIB-055")

NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
CHILD = ViewerScope("ceiling", 10)  # a child of ten; the shelves under test name nobody


def admin() -> Principal:
    return Principal(
        user_id=new_ulid(), username="admin", role="admin", credential=Credential.LOCAL_CLI,
        session_id=None, auth_time=None, can_rate=True,
    )  # fmt: skip


# -- J-PAR-1: a source migration to an adult-only sub-source ------------------------------------


@dataclass
class FamilySeries:
    """A Work rated 6, its Edition filed on a shelf of ceiling 13, chapters 1..5 held from a
    sub-source that says it is safe."""

    env: ReadingEnv
    edition: str
    shelf: str
    old_binding: str

    def admission(self) -> Admission:
        with self.env.engine.connect() as c:
            return RatingService().admission_of(c, self.edition, shelf_id=self.shelf)


@pytest.fixture
def family_series(tmp_path: Path) -> Iterator[FamilySeries]:
    with reading_env(tmp_path) as env:
        edition = env.seed.editions["ok"]
        work = env.seed.works["ok"]
        units = env.units("ok", 5)
        with write_transaction(env.engine) as c:
            shelf = new_ulid()
            c.execute(
                sa.insert(export.shelf).values(
                    id=shelf, name="Kids", phrase="kids", max_age_rating=13, allowed_profiles=[]
                )
            )
            c.execute(
                sa.update(catalog.edition).where(catalog.edition.c.id == edition)
                .values(shelf_id=shelf)
            )  # fmt: skip
            c.execute(
                sa.insert(acquisition.sub_source).values(
                    [
                        {"plugin_id": "suwayomi", "source_key": "suwayomi:1", "name": "Old",
                         "content_warning": "safe", "enabled": True},
                        {"plugin_id": "suwayomi", "source_key": "suwayomi:7002", "name": "New",
                         "content_warning": "nsfw", "enabled": True},
                    ]
                )
            )  # fmt: skip
            item = add_source_item(c, "/old-source")  # under the sub-source `suwayomi:1`
            old = add_binding(
                c, work, edition, item, active=False, disabled_reason="removed_upstream"
            )
            for number, unit in enumerate(units, start=1):
                add_variant(c, unit, old, f"old-{number}")
        yield FamilySeries(env, edition, shelf, old)


def _migrate(series: FamilySeries, *, source_key: str) -> Any:
    listed = tuple(ListedUnit(str(n), n - 1) for n in range(1, 9))
    migrations = SourceMigrations()
    with write_transaction(series.env.engine) as c:
        proposal = migrations.propose(
            c, series.edition, plugin_id="suwayomi", source_key=source_key,
            remote_id="/adult-only", listed=listed, replaces_binding_id=series.old_binding,
            principal=admin(), now=NOW,
        )  # fmt: skip
    with write_transaction(series.env.engine) as c:
        validated = migrations.validate(
            c, series.edition, proposal.id, digest=proposal.digest, overrides=(),
            principal=admin(), now=NOW,
        )  # fmt: skip
    with write_transaction(series.env.engine) as c:
        return migrations.apply(
            c, series.edition, validated.id, digest=validated.digest, principal=admin(), now=NOW
        )


@pytest.mark.req("EF-BIB-014", "EF-BIB-055")
def test_a_migration_to_an_adult_only_source_holds_the_edition_out_of_the_family_shelf(
    family_series: FamilySeries,
) -> None:
    assert family_series.admission() is Admission.ALLOWED  # rated 6, shelf 13: it is in
    outcome = _migrate(family_series, source_key="suwayomi:7002")
    assert outcome.binding_id  # the new binding is active: its chapters 6..8 will be fetched
    with family_series.env.engine.connect() as c:
        suggested = c.execute(
            sa.select(catalog.age_suggestion.c.x).where(
                catalog.age_suggestion.c.entity_type == "edition",
                catalog.age_suggestion.c.entity_id == family_series.edition,
                catalog.age_suggestion.c.state == "open",
            )
        ).all()
    # what the sub-source says is a suggestion, as at the discovery and at the acquisition of a
    # book that joins an Edition already filed: the one door of the shelf then refuses it, and
    # the exporter's last look (`barrier_holds`) writes nothing of it into the children's library
    assert [tuple(r) for r in suggested] == [(True,)]
    assert family_series.admission() is Admission.SUGGESTION_ABOVE


# -- J-PAR-2: an adopted foreign file joins an Edition rated for children -----------------------


@pytest.mark.req("EF-EXP-045", "EF-BIB-055")
@pytest.mark.xfail(
    strict=True,
    reason="M4 jalon J-PAR-2: adoption files a foreign series under the rated Edition of the same "
    "name, and a child sees the adopted chapter nobody rated",
)
def test_an_adopted_foreign_chapter_is_not_shown_to_a_child_before_an_admin_rates_it() -> None:
    with export_env() as env:  # an adult shelf (18+), its allowed list empty: open to everyone
        series = env.new_series("Tale of Orchard")
        held = env.add_chapter(series, "1")
        rate(env, series, 10)
        with env.engine.connect() as c:
            assert visible_unit_ids(c, CHILD, [held]) == {held}  # D-P3: rated 10, shelf open
        a = adoption(env)
        # the foreign library has a folder of that name; its file says no age at all
        a.add_series("Tale of Orchard", ["Tale of Orchard - Ch.2.cbz"])
        a.adopt()
        with env.engine.connect() as c:
            adopted = c.execute(
                sa.select(catalog.unit.c.id).where(
                    catalog.unit.c.edition_id == series.edition_id,
                    catalog.unit.c.number_norm == "2",
                )
            ).scalar_one()  # filed under the rated Edition, by its name alone
            shown = visible_unit_ids(c, CHILD, [adopted])
        assert shown == set()
