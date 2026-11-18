"""M5 L20a, second adverse review: what the two first readings left open.

Each test is a hole, kept red (`xfail(strict=True)`) until the fix lands; run with `--runxfail`
to see each one fail for its own reason (the convention of `test_m5_l20a_rev373.py`). What the
first reviews settled is not replayed here: the AVIF header walk (X2), the render that races the
purge (X1), the caps the worker checks again. Synthetic images only.
"""

import struct
import sys
from collections.abc import Collection

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.imaging import ImageCaps, ImageRejected, Reencoded
from shelfhold.infrastructure.schema import storage
from shelfhold.library.domain.failures import TombstonedContentError
from shelfhold.library.domain.rendition import RenditionParams
from shelfhold.library.public import rendition_rels
from tests import page_formats_env as pf
from tests.export_env import export_env
from tests.integration.test_m5_l20a_renditions import JPEG, _content, _files, _rows, _service
from tests.purge_env import ADMIN, TITLE, make_purge_env, spread


def _stub(data: bytes, params: RenditionParams, caps: ImageCaps) -> Reencoded:
    """A converter that answers without decoding a pixel: every proof below is about what the
    parent decides *before* a converter is ever started."""
    return Reencoded(b"the stub answers", params.kind.upper(), 1, 1)


# -- the WebP bound reads the first chunk, wherever the size really sits ----------------------


def _chunk(fourcc: bytes, payload: bytes) -> bytes:
    return fourcc + struct.pack("<I", len(payload)) + payload


def _webp_size_behind(decoy: bytes) -> bytes:
    """A WebP whose first chunk is `decoy` and whose `VP8X`, behind it, announces a canvas of
    65535 x 65535: the four and a quarter billion pixels a decoder may be asked to hold."""
    vp8x = _chunk(
        b"VP8X", b"\0\0\0\0" + (65_534).to_bytes(3, "little") + (65_534).to_bytes(3, "little")
    )
    body = b"WEBP" + decoy + vp8x
    return b"RIFF" + struct.pack("<I", len(body)) + body


@pytest.mark.req("EF-BIB-015", "ENF-SEC-003")
@pytest.mark.xfail(strict=True)
@pytest.mark.parametrize(
    "decoy",
    [
        pytest.param(_chunk(b"ALPH", b"\0" * 8), id="alph-first"),
        pytest.param(_chunk(b"ANIM", b"\0" * 6), id="anim-first"),
        pytest.param(_chunk(b"ICCP", b"\0" * 8), id="iccp-first"),
    ],
)
def test_a_webp_that_announces_its_size_behind_another_chunk_is_refused_before_the_worker(
    decoy: bytes,
) -> None:
    """The decision of L20a: the pixels a header announces are refused at stage `header`, without
    a worker. The AVIF side was taught to walk the whole file (X2); the WebP side still looks at
    the first chunk only, so a `VP8X` behind an `ALPH`, an `ANIM` or an `ICCP` is never read,
    `announced_size` answers None, and the bomb goes to the converter: the bound the decision
    promises was skipped, and only the worker is left to refuse it."""
    with export_env() as env:
        unit = env.add_chapter(env.new_series(TITLE), "1")
        with pytest.raises(ImageRejected) as refused:
            _service(env, _stub).render(_content(env, unit), _webp_size_behind(decoy), JPEG)
        assert (refused.value.code, refused.value.stage) == ("image.too_large", "header")
        assert _rows(env) == [] and _files(env) == []


# -- a rendition is read back for a source whose purge has begun -------------------------------


@pytest.mark.req("EF-BIB-015", "EF-BIB-052")
@pytest.mark.xfail(strict=True)
def test_a_rendition_is_not_read_back_once_the_purge_of_its_source_has_begun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tombstone comes first in a purge, and `render` honours it. `cached` never asks: from
    the tombstone to the closing, the source still has its row and its rendition is intact, and
    a caller that asks the cache before rendering is handed the converted bytes of a file whose
    purge has begun. `render` refuses in that same window, so the refusal is one line away."""
    with export_env() as env:
        purge = make_purge_env(env.lib)
        series = env.new_series(TITLE)
        unit, _ = spread(env, series)
        content = _content(env, unit)
        service = _service(env, _stub, tombstones=purge.tombstones)
        service.render(content, pf.webp(), JPEG)  # the row and the file are in place

        window: dict[str, object] = {}

        def read_back(connection: Connection, unit_ids: Collection[str]) -> list[str]:
            listed = rendition_rels(connection, unit_ids)
            if sys._getframe(1).f_code.co_name == "_erase_derived":  # the tombstone is placed
                with pytest.raises(TombstonedContentError):  # render refuses, as decided
                    service.render(content, pf.webp(), JPEG)
                window["cached"] = service.cached(content, JPEG, pf.webp())
            return listed

        monkeypatch.setattr("shelfhold.export.application.purge.rendition_rels", read_back)
        report = purge.purge.purge_unit(unit, now=purge.clock.now(), principal=ADMIN, typed=unit)

        assert report.complete
        assert window["cached"] is None  # nothing is handed back once the purge has begun
        assert _rows(env) == [] and _files(env) == []


# -- the Unit that holds the same bytes never makes its rendition again -----------------------


@pytest.mark.req("EF-BIB-015", "EF-BIB-052")
@pytest.mark.xfail(strict=True)
def test_the_unit_that_holds_the_same_bytes_makes_its_rendition_again() -> None:
    """The purge of a rendition follows its source's *file*, whichever Unit holds it: the
    decision of L20a says the other Unit that holds the same bytes has it made again, and the
    listing of the purge says the same. The tombstone the purge records cites the file's hash,
    and `render` refuses by that hash alone: the Unit that was never purged, whose file shares
    the bytes, is refused for good, and the rendition it was promised is never made again."""
    with export_env() as env:
        purge = make_purge_env(env.lib)
        series = env.new_series(TITLE)
        gone, _ = spread(env, series)  # the Unit the admin purges
        survivor = env.add_chapter(series, "2")  # another Unit, another place
        with env.engine.connect() as connection:
            same = connection.execute(
                sa.select(storage.content.c.original_sha256).where(
                    storage.content.c.unit_id == gone
                )
            ).scalar_one()
        # one file, received by two Units: no constraint ties `original_sha256` to one row, and
        # the decision names this very state ("même si une autre Unité tient les mêmes octets").
        # The helper's archive embeds the chapter's number, so the second row is pointed at the
        # first file's hash directly: the state is the one the purge will meet.
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.update(storage.content)
                .where(storage.content.c.unit_id == survivor)
                .values(original_sha256=same)
            )
        service = _service(env, _stub, tombstones=purge.tombstones)
        service.render(_content(env, survivor), pf.webp(), JPEG)  # what the purge will take

        report = purge.purge.purge_unit(gone, now=purge.clock.now(), principal=ADMIN, typed=gone)

        assert report.complete
        assert _rows(env) == [] and _files(env) == []  # taken with the purged file, as decided
        made_again = service.render(_content(env, survivor), pf.webp(), JPEG)
        assert made_again.kind == "jpeg"  # the survivor has it made again
