"""PR #373 (M5 L20a), relecture adverse: what the renditions still let through.

Each test is a hole, kept red (`xfail(strict=True)`) until the fix lands: it then passes and the
strict marker fails the run, so the marker is removed with the fix. Synthetic images only."""

import struct
import sys
from collections.abc import Collection

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection

from shelfhold.infrastructure.imaging import ImageRejected
from shelfhold.infrastructure.schema import storage
from shelfhold.library.public import rendition_rels
from tests import page_formats_env as pf
from tests.export_env import export_env
from tests.integration.test_m5_l20a_renditions import (
    JPEG,
    Counting,
    _content,
    _files,
    _rows,
    _service,
)
from tests.purge_env import ADMIN, TITLE, make_purge_env


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _ispe(width: int, height: int) -> bytes:
    return _box(b"ispe", b"\0\0\0\0" + struct.pack(">II", width, height))


def _avif(*boxes: bytes, padding: int = 0) -> bytes:
    """An AVIF head: `ftyp`, an optional `free` box (legal anywhere at the top level), then a
    `meta > iprp > ipco` holding the given property boxes."""
    meta = _box(b"meta", b"\0\0\0\0" + _box(b"iprp", _box(b"ipco", b"".join(boxes))))
    free = _box(b"free", b"\0" * padding) if padding else b""
    return _box(b"ftyp", b"avif\0\0\0\0avifmif1") + free + meta


@pytest.mark.req("EF-BIB-015", "ENF-SEC-003")
@pytest.mark.parametrize(
    "bomb",
    [
        pytest.param(_avif(_ispe(100_000, 100_000), padding=70_000), id="meta-past-64-KiB"),
        pytest.param(_avif(_ispe(8, 8), _ispe(100_000, 100_000)), id="decoy-ispe-first"),
    ],
)
def test_rev373_avif_bomb_is_refused_on_the_header_wherever_its_size_is(bomb: bytes) -> None:
    """The decision of L20a: the pixels a header announces are refused at stage `header`, without
    starting a worker. A `meta` box placed after 64 KiB (a `free` box first) is never read, and
    only the first `ispe` counts (a grid declares one per tile and one for the whole): both reach
    the worker, which then refuses them; the bound before it is skipped."""
    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        counting = Counting()
        with pytest.raises(ImageRejected) as refused:
            _service(env, counting).render(_content(env, unit), bomb, JPEG)
        assert (refused.value.code, refused.value.stage) == ("image.too_large", "header")
        assert counting.calls == 0


@pytest.mark.req("EF-BIB-015", "EF-BIB-052")
def test_rev373_a_render_that_lands_after_the_late_list_leaves_no_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0048: after `purge --now`, nothing made from the purged file stays on disk. A render
    in flight that records its row after the purge's second listing (`late`) but before the rows
    go keeps its file: the row is erased with the content, the file is named by no row, and
    `collect_orphans`, the only sweep, is called by nothing (and would wait a day)."""
    with export_env() as env:
        purge = make_purge_env(env.lib)
        unit = env.add_chapter(env.new_series(TITLE), "1")
        content = _content(env, unit)
        service = _service(env)
        real = rendition_rels
        late_calls = 0

        def late_then_render(connection: Connection, unit_ids: Collection[str]) -> list[str]:
            nonlocal late_calls
            listed = real(connection, unit_ids)
            if sys._getframe(1).f_code.co_name == "_erase_derived":  # the recount at erase time
                late_calls += 1
                service.render(content, pf.webp(), JPEG)  # a render finishes right after it
            return listed

        monkeypatch.setattr("shelfhold.export.application.purge.rendition_rels", late_then_render)
        report = purge.purge.purge_unit(unit, now=purge.clock.now(), principal=ADMIN, typed=unit)

        assert report.complete and late_calls == 1
        with env.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).where(storage.content.c.id == content)
                ).scalar_one()
                == 0
            )
        assert _rows(env) == []
        assert _files(env) == []  # the converted image of the purged file is still there
