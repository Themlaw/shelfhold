"""EF-SRC-037 (late better-rank arrival) through EF-EXP-016: the door of the follow cycle, by Unit.

Every guard of the same-path upgrade holds on this door too: a read Unit, a suspect revision and
an irreplaceable Unit are left exactly as they are, and a Unit nothing was exported for has
nothing to upgrade.
"""

import pytest
import sqlalchemy as sa

from shelfhold.export.application.upgrade import UpgradeOutcome
from tests.export_env import export_env
from tests.upgrade_env import (
    better_copy,
    bin_files,
    export_one,
    make_irreplaceable,
    mark_read,
    service,
)

pytestmark = pytest.mark.req("EF-SRC-037")


def test_unit_upgrade_replaces_the_served_file_at_its_path() -> None:
    with export_env() as env:
        exported = export_one(env)
        old = exported.target.read_bytes()
        content_id = better_copy(env, exported)
        results = service(env).upgrade_unit(exported.unit_id, content_id)
        assert [r.outcome for r in results] == [UpgradeOutcome.UPGRADED]
        assert env.files() == [exported.target] and exported.target.read_bytes() != old
        assert len(bin_files(env)) == 1  # the old version is in the bin


def test_do_not_upgrade_read_units() -> None:
    with export_env() as env:
        exported = export_one(env)
        old = exported.target.read_bytes()
        content_id = better_copy(env, exported)
        mark_read(env, exported)
        results = service(env).upgrade_unit(exported.unit_id, content_id)
        assert [r.outcome for r in results] == [UpgradeOutcome.SKIPPED_READ]
        assert exported.target.read_bytes() == old and bin_files(env) == []


def test_suspect_revision_is_never_promoted_by_a_late_upgrade() -> None:
    with export_env() as env:
        exported = export_one(env)
        old = exported.target.read_bytes()
        content_id = better_copy(env, exported)
        with env.engine.begin() as connection:
            variant = connection.execute(
                sa.text("SELECT variant_id FROM content WHERE id = :c"), {"c": content_id}
            ).scalar_one()
            connection.execute(
                sa.text(
                    "INSERT INTO variant_revision (variant_id, revision, seen_at, page_count, "
                    "quarantined_at, quarantine_reason) VALUES (:v, 1, :t, 1, :t, 'fewer_pages')"
                ),
                {"v": variant, "t": env.clock.now()},
            )
        results = service(env).upgrade_unit(exported.unit_id, content_id)
        assert [r.outcome for r in results] == [UpgradeOutcome.SUSPECT_REVISION]
        assert exported.target.read_bytes() == old


def test_an_irreplaceable_unit_is_never_replaced_without_its_typed_identifier() -> None:
    with export_env() as env:
        exported = export_one(env)
        old = exported.target.read_bytes()
        content_id = better_copy(env, exported)
        make_irreplaceable(env)
        results = service(env).upgrade_unit(exported.unit_id, content_id)
        assert [r.outcome for r in results] == [UpgradeOutcome.NEEDS_CONFIRMATION]
        assert exported.target.read_bytes() == old


def test_a_unit_nothing_was_exported_for_has_nothing_to_upgrade() -> None:
    with export_env() as env:
        exported = export_one(env)
        content_id = better_copy(env, exported)
        other = env.add_chapter(exported.series, "2")  # never exported
        assert service(env).upgrade_unit(other, content_id) == []
