"""M5 L18, relecture adverse (contradicteur): PoC des trous trouvés dans OPDS-PSE.

Each test was red on the reviewed commit; only R3 (the owner's decision, D-P1) stays
`xfail(strict=True)`, the others pass. The helpers are those of `test_m5_l18_pse.py`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.imaging import Reencoded
from shelfhold.infrastructure.schema import export
from shelfhold.reading.application.content_access import ContentAccess
from tests import page_formats_env as pf
from tests.content_env import FakeArchives, add_carrier
from tests.integration.test_l48_opds import ABSENT_ULID, make_world, parse
from tests.integration.test_m5_l18_pse import Pse, png_pages, shape, stream_links

pytestmark = pytest.mark.req("EF-INT-007")


@pytest.fixture
def pse(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[Pse]:
    """The world of `test_m5_l18_pse.py` (its fixture, as it is)."""
    src = FakeArchives()
    monkeypatch.setattr("shelfhold.main.readable_copies", lambda *_a, **_k: src)
    with make_world(monkeypatch, settings) as world:
        yield Pse(world, src)


# -- R1: the composition lost the one ThumbnailService (its worker bound) ----------------------


@pytest.fixture
def counted(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[tuple[Pse, list[int]]]:
    from shelfhold.library.public import ThumbnailService

    made: list[int] = []

    class Counted(ThumbnailService):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            made.append(1)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("shelfhold.main.ThumbnailService", Counted)
    src = FakeArchives()
    monkeypatch.setattr("shelfhold.main.readable_copies", lambda *_a, **_k: src)
    with make_world(monkeypatch, settings) as world:
        yield Pse(world, src), made


@pytest.mark.req("EF-INT-007")
def test_review_the_thumbnail_service_is_one_per_process(counted: tuple[Pse, list[int]]) -> None:
    pse, made = counted
    path = f"/opds/covers/{'0' * 64}/160"
    pse.world.kid.get(path)
    after_first = len(made)
    for _ in range(3):
        pse.world.kid.get(path)
    assert len(made) == after_first  # the same service answers every request


# -- R2: conversions are not bounded in number --------------------------------------------------


@pytest.fixture
def slow(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[tuple[Pse, list[int]]]:
    peak: list[int] = [0]
    running = [0]
    lock = threading.Lock()

    def convert(data: bytes, params: Any, caps: Any) -> Reencoded:
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        try:
            time.sleep(0.6)
        finally:
            with lock:
                running[0] -= 1
        return Reencoded(b"\xff\xd8\xff\xe0" + data[:16], "JPEG", 64, 48)

    monkeypatch.setattr("shelfhold.library.application.renditions.convert_in_worker", convert)
    src = FakeArchives()
    monkeypatch.setattr("shelfhold.main.readable_copies", lambda *_a, **_k: src)
    with make_world(monkeypatch, settings) as world:
        yield Pse(world, src), peak


@pytest.mark.req("EF-INT-007")
def test_review_page_conversions_are_bounded_like_thumbnails(slow: tuple[Pse, list[int]]) -> None:
    pse, peak = slow
    pages = {f"p{n}.webp": pf.webp((n * 30, 10, 10)) for n in range(1, 7)}
    item = pse.stream("ok", pages)
    assert pse.page(pse.world.kid, item, 0).status_code == 200  # the token is known: no burst
    codes: list[tuple[int, str]] = []

    def ask(n: int) -> None:
        got = pse.page(pse.world.kid, item, n, Accept="image/jpeg")
        codes.append((got.status_code, got.headers.get("content-type", "")))

    threads = [threading.Thread(target=ask, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] <= 2, (peak[0], codes)  # the bound of the thumbnail workers (CONCURRENT_WORKERS)


# -- R3: D-P1, the whole volume of any export_item, exported or not -----------------------------


@pytest.mark.xfail(
    strict=True,
    reason="R3: PSE offre et sert en entier tout export_item CBZ, sans enregistrement d'export "
    "vivant : ce n'est pas « le fichier que le bloc OPDS remet » (plan.load_candidates exige un "
    "record LIVE, un arbre et un kind de ITEM_KINDS) ; la justification de D-P1 ne tient pas",
)
@pytest.mark.req("EF-INT-007")
def test_review_an_item_never_exported_is_not_streamed_whole(pse: Pse) -> None:
    item = pse.stream("ok", png_pages(3))
    with pse.world.seed.engine.connect() as connection:
        records = connection.execute(
            sa.select(sa.func.count())
            .select_from(export.export_record)
            .where(export.export_record.c.export_item_id == item)
        ).scalar_one()
    assert records == 0  # never written to any destination
    feed = parse(pse.world.kid.get(f"/opds/works/{pse.world.seed.works['ok']}"))
    assert stream_links(feed) == []
    assert pse.page(pse.world.kid, item, 2).status_code == 404


# -- R4: the bytes are read by the item, not from the content that was judged (X4) -------------


@pytest.mark.req("EF-INT-007")
def test_review_the_page_is_read_from_the_content_that_was_judged(
    pse: Pse, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = pse.world
    item = pse.stream("ok", png_pages(2))
    hidden_content = add_carrier(world.seed.engine, world.seed.editions["above"], format="cbz")
    secret = {"p1.png": png_pages(1)["p1.png"] + b"hidden"}
    pse.src.by_content[hidden_content] = secret
    original = ContentAccess.resolve

    def resolve_then_repoint(self: ContentAccess, *args: Any) -> Any:
        found = original(self, *args)  # the check passes on the visible content...
        with write_transaction(world.seed.engine) as connection:  # ...then the item moves
            connection.execute(
                sa.update(export.export_item)
                .where(export.export_item.c.id == item)
                .values(content_id=hidden_content)
            )
        pse.src.item_content[item] = hidden_content
        return found

    monkeypatch.setattr(ContentAccess, "resolve", resolve_then_repoint)
    got = pse.page(world.kid, item, 0)
    assert not got.content.endswith(b"hidden")  # never the bytes of a content nobody judged


# -- R5: the cost of a feed grows with the Editions, not 50 ------------------------------------


@pytest.mark.req("EF-INT-007")
def test_review_one_feed_opens_at_most_fifty_archives(pse: Pse) -> None:
    for edition in ("ok", "ok_second"):
        for _ in range(30):
            pse.stream(edition, png_pages(1))
    opened: list[str] = []
    real = pse.src.open

    def counting(carrier_type: str, carrier_id: str, scope: str = "full") -> Any:
        opened.append(carrier_id)
        return real(carrier_type, carrier_id, scope)

    pse.src.open = counting  # type: ignore[method-assign]
    got = pse.world.kid.get(f"/opds/works/{pse.world.seed.works['ok']}")
    assert got.status_code == 200
    assert len(opened) <= 50


# a sanity control, not a hole: the fixtures above build the world the L18 tests build
def test_review_control_the_world_streams(pse: Pse) -> None:
    item = pse.stream("ok", png_pages(1))
    assert pse.page(pse.world.kid, item, 0).status_code == 200
    assert shape(pse.page(pse.world.kid, ABSENT_ULID, 0))[0] == 404
