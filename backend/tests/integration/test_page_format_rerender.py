"""M4 L53: a chapter written to a destination before the normalization still has its AVIF pages.
`library normalize-pages` finds it and asks the same-path upgrade to render it again from the
library: the old file goes to the bin, the new one takes its path, and a second pass changes
nothing."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from shelfhold.export.application.page_policy import PagePolicy
from shelfhold.export.application.page_rerender import stale_served_files
from shelfhold.export.application.rendering import CbzRenderer
from shelfhold.export.application.upgrade import (
    SamePathUpgrade,
    UpgradeOutcome,
    UpgradeRequest,
)
from shelfhold.export.domain.physical_roots import RECEIVED_DIR
from shelfhold.infrastructure.content_trust import KEEP_PAGE_KINDS
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from shelfhold.library.public import PassthroughStore
from tests import page_formats_env as pf
from tests.export_env import ExportEnv, export_env
from tests.integration.test_schema_catalog import add_unit, add_variant
from tests.locks_env import work_lock
from tests.null_tombstones import NONE_PURGED

pytestmark = pytest.mark.skipif(not pf.has_avif(), reason="this Pillow has no AVIF")
WITH_AVIF = PagePolicy(keep=KEEP_PAGE_KINDS | {"avif"})  # how a chapter used to be written


def avif_chapter(env: ExportEnv) -> tuple[str, str, str]:
    series = env.new_series()
    with write_transaction(env.engine) as connection:
        unit = add_unit(connection, series.edition_id, "1")
        variant = add_variant(connection, unit, series.binding_id, f"ch-{new_ulid()}")
    PassthroughStore(
        env.engine,
        env.lib.fsops,
        env.lib.locate,
        env.lib.data,
        tombstones=NONE_PURGED,
        hold_work=work_lock(env.engine),
    ).ingest(
        unit_id=unit,
        variant_id=variant,
        staged=env.lib.stage(pf.cbz([("001.avif", pf.avif()), ("002.avif", pf.avif((1, 2, 3)))])),
        dest=str(env.lib.manga / RECEIVED_DIR / f"{unit}.cbz"),
    )
    with env.engine.connect() as connection:
        content = connection.execute(
            sa.text("SELECT id FROM content WHERE unit_id = :u"), {"u": unit}
        ).scalar_one()
    return series.edition_id, unit, str(content)


def upgrader(env: ExportEnv) -> SamePathUpgrade:
    return SamePathUpgrade(
        env.engine,
        env.lib.fsops,
        locate=env.lib.locate,
        data_dir=env.lib.data,
        clock=env.clock,
        renderer=CbzRenderer(env.lib.fsops, staging_dir=env.lib.data / "tmp"),
    )


@pytest.mark.req("EF-EXP-046", "EF-EXP-016")
def test_a_served_file_with_avif_pages_is_found_rendered_again_and_then_left_alone() -> None:
    with export_env() as env:
        _, _, content = avif_chapter(env)
        env.pipeline._renderer = CbzRenderer(  # how it was written before this lot
            env.lib.fsops, staging_dir=env.lib.data / "tmp", pages=WITH_AVIF
        )
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        (served,) = env.files()
        assert pf.names(served.read_bytes())[:2] == ["001.avif", "002.avif"]
        found = stale_served_files(env.engine, env.lib.fsops, lambda _target: None)
        assert [f.local for f in found] == [str(served)]
        with env.engine.connect() as connection:
            item = connection.execute(sa.select(export.export_item.c.id)).scalar_one()
        asked = UpgradeRequest(env.destination_id, item, content, "better_version", rerender=True)
        assert upgrader(env).upgrade(asked).outcome is UpgradeOutcome.UPGRADED
        assert pf.names(served.read_bytes())[:2] == ["001.jpg", "002.jpg"]
        assert stale_served_files(env.engine, env.lib.fsops, lambda _target: None) == []
        assert upgrader(env).upgrade(asked).outcome is UpgradeOutcome.UP_TO_DATE  # nothing moves


@pytest.mark.req("EF-EXP-046")
def test_the_ordinary_upgrade_does_not_rewrite_a_file_for_the_same_content() -> None:
    with export_env() as env:
        _, _, content = avif_chapter(env)
        env.pipeline._renderer = CbzRenderer(
            env.lib.fsops, staging_dir=env.lib.data / "tmp", pages=WITH_AVIF
        )
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        with env.engine.connect() as connection:
            item = connection.execute(sa.select(export.export_item.c.id)).scalar_one()
        plain = UpgradeRequest(env.destination_id, item, content, "better_version")
        assert upgrader(env).upgrade(plain).outcome is UpgradeOutcome.UP_TO_DATE


@pytest.mark.req("EF-EXP-046")
def test_the_same_path_upgrade_gives_the_renderer_the_target_of_the_destination() -> None:
    with export_env() as env:
        _, _, content = avif_chapter(env)
        env.pipeline._renderer = CbzRenderer(
            env.lib.fsops, staging_dir=env.lib.data / "tmp", pages=WITH_AVIF
        )
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        with env.engine.connect() as connection:
            item = connection.execute(sa.select(export.export_item.c.id)).scalar_one()
            target = connection.execute(
                sa.select(export.shelf_destination.c.target_id)
            ).scalar_one()
        seen: list[str | None] = []
        upgrade = upgrader(env)
        real = upgrade._renderer.render

        def render(source: str, xml: bytes, *, target_id: str | None = None) -> object:
            seen.append(target_id)
            return real(source, xml, target_id=target_id)

        upgrade._renderer.render = render  # type: ignore[method-assign]
        upgrade.upgrade(
            UpgradeRequest(env.destination_id, item, content, "better_version", rerender=True)
        )
        assert seen == [target]  # the policy of that destination applies, not the default one
