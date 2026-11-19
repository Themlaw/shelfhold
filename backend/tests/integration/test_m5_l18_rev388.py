"""M5 L18, independent review of #388: what the first review and the second opinion left
(EF-INT-007, EF-BIB-015). Written in English so the typos scan passes.

C1. The rendition cache is shared by every profile (keyed by the bytes read, not by the reader),
and a page already converted is served without a slot, while a page to convert takes one of the
`MAX_HELD` threads. A profile that fills the threads itself (its own pages, slow to convert) thus
gets a 200 for a page that **another** profile already read as a JPEG and a 429 for a page nobody
read: an exact oracle, with no timing, on what another profile reads.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.imaging import Reencoded
from shelfhold.interfaces.http_api.opds.pse import CONVERSIONS, MAX_HELD
from tests import page_formats_env as pf
from tests.content_env import FakeArchives
from tests.integration.test_l48_opds import make_world
from tests.integration.test_m5_l18_pse import Pse

pytestmark = pytest.mark.req("EF-INT-007")


class _Slow:
    """The converter of the test: the pages in `held` convert only once `gate` is set."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.held: set[bytes] = set()
        self.entered = 0
        self.lock = threading.Lock()

    def __call__(self, data: bytes, params: Any, caps: Any) -> Reencoded:
        if bytes(data) in self.held:
            with self.lock:
                self.entered += 1
            self.gate.wait(30)
        return Reencoded(b"\xff\xd8\xff\xe0" + bytes(data[:16]), "JPEG", 64, 48)


@pytest.fixture
def slow(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[tuple[Pse, _Slow]]:
    convert = _Slow()
    monkeypatch.setattr("shelfhold.library.application.renditions.convert_in_worker", convert)
    monkeypatch.setattr("shelfhold.interfaces.http_api.opds.pse._WAIT_S", 30.0)
    src = FakeArchives()
    monkeypatch.setattr("shelfhold.main.readable_copies", lambda *_a, **_k: src)
    with make_world(monkeypatch, settings) as world:
        try:
            yield Pse(world, src), convert
        finally:
            convert.gate.set()


@pytest.mark.req("EF-INT-007")
@pytest.mark.xfail(
    strict=True,
    reason="C1 (#388): a cached rendition needs no slot, a conversion waits for a thread: "
    "200 against 429 tells what another profile read",
)
def test_rev388_a_profile_cannot_tell_which_pages_another_profile_read(
    slow: tuple[Pse, _Slow],
) -> None:
    pse, convert = slow
    shared = {f"p{n}.webp": pf.webp((n * 9, 20, 20)) for n in range(1, 4)}
    item = pse.stream("ok", shared)  # a volume the member and the child both see
    # the member reads page 1 as a JPEG: a rendition is made and kept, for every profile
    assert pse.page(pse.world.mum, item, 1, Accept="image/jpeg").status_code == 200

    # the child's own volume, whose conversions never end: they take every thread
    own = {f"q{n}.webp": pf.webp((5, n * 9, 40)) for n in range(1, MAX_HELD + 1)}
    convert.held = set(own.values())
    mine = pse.stream("ok", own)
    threads = [
        threading.Thread(
            target=pse.page, args=(pse.world.kid, mine, n), kwargs={"Accept": "image/jpeg"}
        )
        for n in range(MAX_HELD)
    ]
    for t in threads:
        t.start()
    try:
        deadline = time.monotonic() + 20
        while convert.entered < CONVERSIONS and time.monotonic() < deadline:
            time.sleep(0.02)
        assert convert.entered == CONVERSIONS
        time.sleep(0.5)  # the other threads reach their wait for a slot
        seen_by_other = pse.page(pse.world.kid, item, 1, Accept="image/jpeg").status_code
        seen_by_none = pse.page(pse.world.kid, item, 2, Accept="image/jpeg").status_code
    finally:
        convert.gate.set()
        for t in threads:
            t.join(30)
    # whatever the answer under load, it must not depend on what another profile read
    assert seen_by_other == seen_by_none, (seen_by_other, seen_by_none)
