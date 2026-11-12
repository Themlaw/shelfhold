"""Review of #173, blocker: the upgrade at the same path is held by the age barrier too.

A file already served from an Edition a shelf below 18+ no longer admits (a suggested age above the
rating, a rating raised, an Edition rated above, an Edition withdrawn) is never replaced by new
content: all three doors (`upgrade`, `upgrade_unit` for the follow cycle and the runtime job,
`replace_volume` for the pipeline) go through the same refusal.
"""

import io
from collections.abc import Callable

import pytest
import sqlalchemy as sa

from shelfhold.export.application.upgrade import UpgradeOutcome
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.ops.status import collect
from tests.export_env import ExportEnv
from tests.family_env import family_env, rate, rate_edition, suggest
from tests.upgrade_env import (
    Exported,
    better_copy,
    bin_files,
    request,
    service,
)

pytestmark = pytest.mark.req("EF-BIB-055", "EF-BIB-037", "EF-EXP-016")


def served_on_family_shelf(env: ExportEnv) -> tuple[Exported, str]:
    """A chapter of a Work rated 10 and exported to the shelf of 13+; its Edition's id."""
    series = env.new_series()
    unit_id = env.add_chapter(series, "1")
    rate(env, series, 10)
    env.pipeline.run(env.destination_id, job_id=new_ulid())
    (target,) = env.files()
    with env.engine.connect() as connection:
        row = connection.execute(
            sa.select(export.export_record).where(
                export.export_record.c.shelf_destination_id == env.destination_id
            )
        ).one()
    exported = Exported(
        series, unit_id, row.export_item_id, target, str(row.file_hash), str(row.content_hash)
    )
    return exported, series.edition_id


def raise_work(env: ExportEnv, exported: Exported, _edition: str) -> None:
    rate(env, exported.series, 16)  # M1b: the Work's age was raised after the export


def rate_edition_above(env: ExportEnv, _exported: Exported, edition: str) -> None:
    rate_edition(env, edition, 16)


def suggest_above(env: ExportEnv, _exported: Exported, edition: str) -> None:
    suggest(env, edition, 18)  # the source says "mixed"


def suggest_x(env: ExportEnv, _exported: Exported, edition: str) -> None:
    suggest(env, edition, 18, x=True)


def withdraw(env: ExportEnv, _exported: Exported, edition: str) -> None:
    with env.engine.begin() as connection:
        connection.execute(
            sa.update(catalog.edition).where(catalog.edition.c.id == edition).values(shelf_id=None)
        )


CHANGES: list[Callable[[ExportEnv, Exported, str], None]] = [
    raise_work, rate_edition_above, suggest_above, suggest_x, withdraw,
]  # fmt: skip


def door_upgrade(env: ExportEnv, exported: Exported, content_id: str) -> UpgradeOutcome:
    return service(env).upgrade(request(env, exported, content_id)).outcome


def door_unit(env: ExportEnv, exported: Exported, content_id: str) -> UpgradeOutcome:
    """What the follow cycle (`follow.py`) and the runtime job call."""
    (result,) = service(env).upgrade_unit(exported.unit_id, content_id)
    return result.outcome


def door_volume(env: ExportEnv, exported: Exported, _content_id: str) -> UpgradeOutcome:
    """What the pipeline calls (`pipeline.py`)."""
    return (
        service(env)
        .replace_volume(
            env.destination_id,
            exported.export_item_id,
            data=io.BytesIO(b"x" * 10),
            file_hash="sha256:" + "1" * 64,
            content_hash="sha256:" + "2" * 64,
            source_hash="sha256:" + "3" * 64,
            spine_count=2,
        )
        .outcome
    )


DOORS: list[Callable[[ExportEnv, Exported, str], UpgradeOutcome]] = [
    door_upgrade, door_unit, door_volume,
]  # fmt: skip


@pytest.mark.parametrize("change", CHANGES, ids=lambda c: c.__name__)
@pytest.mark.parametrize("door", DOORS, ids=lambda d: d.__name__)
def test_a_served_file_of_an_edition_the_shelf_no_longer_admits_is_not_replaced(
    door: Callable[..., UpgradeOutcome], change: Callable[..., None]
) -> None:
    with family_env(13) as env:
        exported, edition = served_on_family_shelf(env)
        old = exported.target.read_bytes()
        content_id = better_copy(env, exported)
        change(env, exported, edition)
        assert door(env, exported, content_id) is UpgradeOutcome.REFUSED_AGE
        assert exported.target.read_bytes() == old and bin_files(env) == []
        # the refusal is not a line of the log alone: `status` shows the Edition, with its exit
        with env.engine.connect() as connection:
            waiting = [
                (i.state, i.subject, i.command)
                for i in collect(connection)
                if i.state == "withdrawal_pending"
            ]
        assert waiting == [("withdrawal_pending", edition, "shelfhold shelf withdraw")]


@pytest.mark.parametrize("door", [door_upgrade, door_unit], ids=lambda d: d.__name__)
def test_an_edition_the_shelf_still_admits_is_upgraded_as_before(
    door: Callable[..., UpgradeOutcome],
) -> None:
    with family_env(13) as env:
        exported, _edition = served_on_family_shelf(env)
        old = exported.target.read_bytes()
        assert door(env, exported, better_copy(env, exported)) is UpgradeOutcome.UPGRADED
        assert exported.target.read_bytes() != old


def test_an_adult_shelf_has_nothing_to_ask() -> None:
    from tests.export_env import export_env
    from tests.upgrade_env import export_one

    with export_env() as env:  # no ceiling
        exported = export_one(env)
        assert door_upgrade(env, exported, better_copy(env, exported)) is UpgradeOutcome.UPGRADED


def test_a_restore_of_an_upgrade_is_refused_too_when_the_edition_is_above_the_rating() -> None:
    with family_env(13) as env:
        exported, edition = served_on_family_shelf(env)
        svc = service(env)
        done = svc.upgrade(request(env, exported, better_copy(env, exported)))
        assert done.outcome is UpgradeOutcome.UPGRADED and done.upgrade_id is not None
        suggest(env, edition, 18)
        served = exported.target.read_bytes()
        assert svc.restore(done.upgrade_id).outcome is UpgradeOutcome.REFUSED_AGE
        assert exported.target.read_bytes() == served


def pause_shelf(env: ExportEnv, exported: Exported, _edition: str) -> None:
    with env.engine.begin() as connection:
        connection.execute(sa.update(export.shelf).values(paused_reason="audit"))


def read_only_target(env: ExportEnv, exported: Exported, _edition: str) -> None:
    from shelfhold.export.application.target_health import TargetHealth
    from shelfhold.export.application.target_state import save_health
    from shelfhold.export.domain.kavita_compat import TargetMode

    with env.engine.begin() as connection:
        save_health(
            connection, env.target_id, TargetHealth(version_mode=TargetMode.DEGRADED_READONLY)
        )


@pytest.mark.parametrize("closing", [pause_shelf, read_only_target], ids=lambda c: c.__name__)
@pytest.mark.parametrize("door", DOORS, ids=lambda d: d.__name__)
def test_a_closed_destination_is_told_apart_from_the_age(
    door: Callable[..., UpgradeOutcome], closing: Callable[..., None]
) -> None:
    """The label says what closed the door: a paused or read-only destination is not an age."""
    with family_env(13) as env:
        exported, edition = served_on_family_shelf(env)
        old = exported.target.read_bytes()
        content_id = better_copy(env, exported)
        closing(env, exported, edition)
        assert door(env, exported, content_id) is UpgradeOutcome.REFUSED_DESTINATION
        assert exported.target.read_bytes() == old and bin_files(env) == []


def test_the_destination_is_told_before_the_age() -> None:
    with family_env(13) as env:
        exported, edition = served_on_family_shelf(env)
        suggest(env, edition, 18)
        pause_shelf(env, exported, edition)
        result = door_upgrade(env, exported, better_copy(env, exported))
        assert result is UpgradeOutcome.REFUSED_DESTINATION
