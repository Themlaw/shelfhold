"""M5 L18, second avis (contradicteur) après la relecture adverse : PoC de ce qu'elle a laissé.

Les PoC du second avis : rouges sur dc449080, ils passent une fois les trous fermés.
"""

from __future__ import annotations

import threading
import time
import types
from collections.abc import Iterator
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.imaging import Reencoded
from shelfhold.library.application.thumbnails import MAX_HELD
from tests import page_formats_env as pf
from tests.content_env import FakeArchives
from tests.integration.test_l48_opds import make_world
from tests.integration.test_m5_l18_pse import Pse, png_pages

pytestmark = pytest.mark.req("EF-INT-007")


# -- S1: the conversions that wait are not bounded in number of threads -----------------


class _Watch:
    """Counts the request threads that wait at the same time for a conversion slot."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.waiting = 0
        self.peak = 0


@pytest.fixture
def waiting(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[tuple[Pse, _Watch]]:
    watch = _Watch()
    real = threading.BoundedSemaphore

    class Counted:
        def __init__(self, value: int) -> None:
            self._inner = real(value)

        def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
            if self._inner.acquire(blocking=False):
                return True
            with watch.lock:
                watch.waiting += 1
                watch.peak = max(watch.peak, watch.waiting)
            try:
                return self._inner.acquire(blocking, timeout if timeout is not None else -1)
            finally:
                with watch.lock:
                    watch.waiting -= 1

        def release(self) -> None:
            self._inner.release()

    monkeypatch.setattr(
        "shelfhold.interfaces.http_api.opds.pse.threading",
        types.SimpleNamespace(BoundedSemaphore=Counted),
    )

    def convert(data: bytes, params: Any, caps: Any) -> Reencoded:
        time.sleep(1.5)  # une conversion lente : le worker a jusqu'à ses limites de temps
        return Reencoded(b"\xff\xd8\xff\xe0" + data[:16], "JPEG", 64, 48)

    monkeypatch.setattr("shelfhold.library.application.renditions.convert_in_worker", convert)
    src = FakeArchives()
    monkeypatch.setattr("shelfhold.main.readable_copies", lambda *_a, **_k: src)
    with make_world(monkeypatch, settings) as world:
        yield Pse(world, src), watch


@pytest.mark.req("EF-INT-007")
def test_second_review_waiting_conversions_hold_a_bounded_number_of_threads(
    waiting: tuple[Pse, _Watch],
) -> None:
    pse, watch = waiting
    pages = {f"p{n}.webp": pf.webp((n * 12, 10, 10)) for n in range(1, 25)}
    item = pse.stream("ok", pages)
    assert pse.page(pse.world.kid, item, 0).status_code == 200  # natif : aucune conversion
    codes: list[int] = []

    def ask(n: int) -> None:
        codes.append(pse.page(pse.world.kid, item, n, Accept="image/jpeg").status_code)

    threads = [threading.Thread(target=ask, args=(n,)) for n in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert watch.peak <= MAX_HELD, (watch.peak, sorted(codes))


# -- S2: le plafond R5 ne compte que les offres faites, pas les archives ouvertes ---------------


@pytest.fixture
def pse(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[Pse]:
    src = FakeArchives()
    monkeypatch.setattr("shelfhold.main.readable_copies", lambda *_a, **_k: src)
    with make_world(monkeypatch, settings) as world:
        yield Pse(world, src)


@pytest.mark.req("EF-INT-007")
def test_second_review_one_feed_opens_at_most_fifty_archives_even_without_pages(
    pse: Pse,
) -> None:
    for edition in ("ok", "ok_second"):
        for _ in range(40):
            pse.stream(edition, {"readme.txt": b"no page here"})
    pse.stream("ok", png_pages(1))  # le contrôle : un tome lisible est bien proposé
    opened: list[str] = []
    real = pse.src.open

    def counting(carrier_type: str, carrier_id: str, scope: str = "full") -> Any:
        opened.append(carrier_id)
        return real(carrier_type, carrier_id, scope)

    pse.src.open = counting  # type: ignore[method-assign]
    got = pse.world.kid.get(f"/opds/works/{pse.world.seed.works['ok']}")
    assert got.status_code == 200
    assert len(opened) <= 50, len(opened)
