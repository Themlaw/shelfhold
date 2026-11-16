"""M4 L53 (EF-EXP-046, ADR-0054): a chapter with a page nothing can read is set aside.

It never leaves as received (no reader would show it): it stays out of the destination, shows on
the Health page with a closed code, and is not decoded again at every cycle. A page that kills the
decoder is not retried either, until a back-off has passed.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.domain.physical_roots import RECEIVED_DIR
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.imaging import ImageRejected
from shelfhold.infrastructure.imaging.errors import WORKER_FAILED
from shelfhold.infrastructure.packaging.cbz import normalize
from shelfhold.infrastructure.schema import storage
from shelfhold.library.public import PassthroughStore, page_issue_counts
from tests import page_formats_env as pf
from tests.export_env import ExportEnv, export_env
from tests.integration.test_schema_catalog import add_unit, add_variant
from tests.locks_env import work_lock
from tests.null_tombstones import NONE_PURGED

pytestmark = pytest.mark.skipif(not pf.has_avif(), reason="this Pillow has no AVIF")


def _unit(env: ExportEnv, pages: list[tuple[str, bytes]]) -> str:
    series = env.new_series("Dr Stone")
    with write_transaction(env.engine) as connection:
        unit = add_unit(connection, series.edition_id, "1")
        variant = add_variant(connection, unit, series.binding_id, f"ch-{new_ulid()}")
    PassthroughStore(
        env.lib.engine, env.lib.fsops, env.lib.locate, env.lib.data,
        tombstones=NONE_PURGED, hold_work=work_lock(env.engine),
    ).ingest(
        unit_id=unit, variant_id=variant, staged=env.lib.stage(pf.cbz(pages)),
        dest=str(env.lib.manga / RECEIVED_DIR / f"{unit}.cbz"),
    )  # fmt: skip
    from shelfhold.export.application.export_items import create_export_item

    with write_transaction(env.engine) as connection:
        create_export_item(
            connection, edition_id=series.edition_id, unit_id=unit, kind="unit", frozen_attrs={}
        )
    return unit


def _issues(env: ExportEnv) -> list[Any]:
    with env.engine.connect() as connection:
        return list(connection.execute(sa.select(storage.page_issue)))


def _spy(env: ExportEnv) -> list[str]:
    calls: list[str] = []
    real = env.pipeline._renderer.render

    def render(*args: Any, **kwargs: Any) -> Any:
        calls.append("render")
        return real(*args, **kwargs)

    env.pipeline._renderer.render = render
    return calls


@pytest.mark.req("EF-EXP-046", "ENF-SEC-003")
def test_an_unreadable_page_sets_the_chapter_aside_and_it_is_not_decoded_again() -> None:
    with export_env() as env:
        unit = _unit(env, [("1.avif", pf.avif()), ("2.avif", pf.truncated_avif())])
        calls = _spy(env)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert env.files() == []  # never sent as received (an AVIF chapter)
        (row,) = _issues(env)
        assert (row.unit_id, row.code) == (unit, "page.undecodable")
        with env.engine.connect() as connection:
            assert page_issue_counts(connection) == [("page.undecodable", 1)]
        assert calls == ["render"]
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert calls == ["render"]  # set aside: not rendered, not decoded, again


@pytest.mark.req("EF-EXP-046", "ENF-SEC-003")
def test_a_page_that_kills_the_decoder_is_set_aside_with_a_back_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def killed(*_: Any, **__: Any) -> Any:
        raise ImageRejected(WORKER_FAILED, "worker_signal_11", "worker")

    monkeypatch.setattr(normalize, "reencode_image", killed)
    with export_env() as env:
        _unit(env, [("1.avif", pf.avif())])
        calls = _spy(env)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        (row,) = _issues(env)
        assert (row.code, row.failures) == ("pages.worker_failed", 1)
        assert env.files() == []
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert calls == ["render"]  # the back-off holds: the decoder is not asked a second time
