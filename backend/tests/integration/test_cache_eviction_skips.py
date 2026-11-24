"""The quota's hand of the cache sweep skips what it cannot take (EF-BIB-018, D17): an entry whose
file will not go, or that a request has pinned since the decision, stays with its row, and the
sweep goes on to the next. Synthetic data only."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import Engine

from shelfhold.infrastructure.schema import storage
from shelfhold.library.application import cache_quotas
from shelfhold.library.application.cache_quotas import purge_caches
from shelfhold.library.application.renditions import RenditionService
from tests.fakes_clock import ManualClock
from tests.integration.test_l12_cache_retention import (
    NOW,
    _rows,
    add_rendition,
    add_thumbnail,
    data,
    engine,
    policy,
)

pytestmark = pytest.mark.req("EF-BIB-018")

__all__ = ["data", "engine"]  # the fixtures of the retention tests


def test_a_thumbnail_that_will_not_go_stays_with_its_row(
    engine: Engine, data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_thumbnail(engine, data, "stuck", used_age_days=5, bytes_=1000)

    def refuse(_root: Path, _rel: str) -> bool:
        raise OSError("busy")

    monkeypatch.setattr(cache_quotas, "erase_cache_path", refuse)
    sweep = purge_caches(engine, data, ManualClock(NOW), policy(thumbnails_quota_bytes=1))

    assert sweep.thumbnails_files == 0 and sweep.thumbnails_rows == 0
    assert _rows(engine, storage.thumbnail) == 1  # the file stayed, so did its row


def test_a_rendition_that_will_not_go_stays_with_its_row(
    engine: Engine, data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept = add_rendition(engine, data, "stuck", age_days=5)

    def refuse(_root: Path, _rel: str) -> bool:
        raise OSError("busy")

    monkeypatch.setattr(cache_quotas, "erase_cache_path", refuse)
    sweep = purge_caches(engine, data, ManualClock(NOW), policy(renditions_quota_bytes=1))

    assert sweep.renditions_files == 0 and sweep.renditions_rows == 0
    assert (data / kept.rel).exists() and _rows(engine, storage.rendition) == 1


def test_a_rendition_pinned_since_the_decision_waits_for_the_next_pass(
    engine: Engine, data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned = add_rendition(engine, data, "late", age_days=5)

    @contextmanager
    def taken(_content_hash: str) -> Iterator[bool]:
        yield False  # a request took the file after the pass began

    monkeypatch.setattr(RenditionService, "erasable", staticmethod(taken))
    sweep = purge_caches(engine, data, ManualClock(NOW), policy(renditions_quota_bytes=1))

    assert sweep.renditions_files == 0
    assert (data / pinned.rel).exists() and _rows(engine, storage.rendition) == 1
