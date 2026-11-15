"""Relecture adverse de M4 L14b (#292) : PoC rouges, en `xfail` strict, des trous trouvés.

Chaque test dit ce que la synchronisation devrait tenir ; tant qu'il échoue, le trou est là.
Sources lues : Kavita `v0.9.1.4` (`OPDSController.cs`, `SeriesService.GetSeriesDetail`,
`ReaderService.SaveReadingProgress`) et KOReader (`plugins/opds.koplugin/opdspse.lua`).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import sqlalchemy as sa

from shelfhold.export.targets.kavita import AdminKey, KavitaClient
from shelfhold.infrastructure.db import write_transaction
from shelfhold.reading.application import sync as sync_module
from shelfhold.reading.public import undo_read_mark
from shelfhold.runtime_jobs import progress_sync_handler
from tests.fakes.kavita import API_KEY, BASE_URL, build_fetcher
from tests.fakes_clock import ManualClock
from tests.sync_env import START, sync_env

pytestmark = pytest.mark.req("EF-EXP-026", "EF-LEC-014")


def koreader_page_stream(pages: int, kavita_page: int) -> int:
    """What Kavita keeps after KOReader opens a chapter with « Page Stream » (OPDS-PSE).

    `opdspse.lua` : `getLastPage` lit `pageNum` (get-progress), puis
    `viewer:switchToImageNum(last_page + 1)` ; l'image `key` est demandée en `{pageNumber} =
    key - 1`, donc l'index `kavita_page`. `OPDSController` (client `Koreader`) ajoute 1 quand
    `totalPages - pageNumber < 2`, puis `SaveReadingProgress` plafonne au nombre de pages."""
    requested = kavita_page
    offset = 1 if pages - requested < 2 else 0
    return min(requested + offset, pages)


# ------------------------------------------------------------------ B1 : blanchiment du « lu »


def test_b1_a_look_pushed_to_kavita_is_not_washed_by_opening_the_koreader_stream(
    tmp_path: Path,
) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.device_position(reader, chapter, chapter.pages)  # un coup d'oeil à la fin
        env.clock.advance(minutes=1)
        env.sync(reader)
        assert env.kavita_page(reader, chapter) == chapter.pages - 2  # « la page, pas la marque »
        # Le membre ouvre ce chapitre dans KOReader par le flux de Kavita, sans tourner une page.
        kept = koreader_page_stream(chapter.pages, env.kavita_page(reader, chapter))
        env.read_in_kavita(reader, chapter, kept)
        env.clock.advance(minutes=10)
        env.sync(reader)
        assert env.state(reader, chapter) != "read"


# ------------------------------------------------------------------ M1 : nouvelle liaison


def test_m1_a_member_relinked_to_another_kavita_account_starts_afresh(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader", kavita_name="first-account")
        chapter = env.chapter("Orchard Road", 1)
        env.device_position(reader, chapter, 15)
        env.clock.advance(minutes=1)
        env.sync(reader)
        assert env.kavita_page(reader, chapter) == 15
        env.link(reader, "second-account")  # L12 : une autre clé, un autre compte
        env.clock.advance(minutes=10)
        assert env.sync(reader).code == "ok"
        assert env.kavita_page(reader, chapter) == 15  # « un compte relié repart de zéro »


# ------------------------------------------------------------------ M2 : coût d'un passage


def test_m2_an_idle_pass_neither_writes_per_chapter_nor_reopens_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapters = [env.chapter("Orchard Road", n) for n in range(1, 6)]
        env.device_position(reader, chapters[0], 7)
        env.clock.advance(minutes=1)
        env.sync(reader)
        env.clock.advance(minutes=10)

        writes: list[int] = []
        real_write = write_transaction

        def counting(engine: sa.Engine) -> Any:
            writes.append(1)
            return real_write(engine)

        opened: list[int] = []
        real_open = env.remote._open_token

        def counting_open(user: str, destination: str) -> Any:
            opened.append(1)
            return real_open(user, destination)

        monkeypatch.setattr(sync_module, "write_transaction", counting)
        monkeypatch.setattr(env.remote, "_open_token", counting_open)
        assert env.sync(reader).code == "ok"  # rien n'a bougé, ni ici ni dans Kavita
        assert len(chapters) == 5
        assert len(writes) <= 1, f"{len(writes)} transactions d'écriture pour un passage à vide"
        assert len(opened) <= 1, f"clé ouverte {len(opened)} fois"


# ------------------------------------------------------------------ m1 : series-detail réel


def test_m1_a_chapter_listed_twice_by_series_detail_is_synchronised_once() -> None:
    import asyncio

    from tests.fakes.kavita import FakeKavita

    client = KavitaClient(
        build_fetcher(FakeKavita()), BASE_URL, AdminKey(API_KEY),
        events=SimpleNamespace(emit=lambda *a, **k: None), clock=ManualClock(START),
    )  # fmt: skip
    chapter = {"id": 41, "volumeId": 9, "pages": 20,
               "files": [{"filePath": "/data/comics/manga/S/S v01 c003.cbz"}]}  # fmt: skip
    answer = {  # `GetSeriesDetail` : `Chapters` = tous les chapitres hors spéciaux et hors -100000
        "specials": [], "chapters": [chapter], "storylineChapters": [],
        "volumes": [{"id": 9, "chapters": [chapter]}],
    }  # fmt: skip

    async def send(route: Any, **_: Any) -> Any:
        return SimpleNamespace(data=answer)

    client._send = send  # type: ignore[method-assign]
    ids = [c.chapter_id for c in asyncio.run(client.series_chapter_info(1))]
    assert ids == [41]


# ------------------------------------------------------------------ m2 : pause et maintenance


def test_m2_a_stop_requested_during_a_destination_stops_the_next_member(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        first, second = env.member("first"), env.member("second")
        chapter = env.chapter("Orchard Road", 1)
        env.device_position(first, chapter, 4)
        env.device_position(second, chapter, 6)
        env.clock.advance(minutes=1)
        ctx = SimpleNamespace(counters={}, stop_requested=False, stop=lambda: False)
        ctx.record = ctx.counters.update
        ctx.add = lambda **c: [ctx.counters.update({k: ctx.counters.get(k, 0) + v})
                               for k, v in c.items()]  # fmt: skip

        def maintenance(_request: Any) -> None:  # la pause arrive pendant le premier membre
            ctx.stop_requested = True

        env.fake.hook = maintenance
        progress_sync_handler(env.engine, lambda: env.remote, env.clock)(cast(Any, ctx))
        assert env.calls(env.keys[second], fragment="/api/") == []


# ------------------------------------------------------------------ vérifié : l'annulation tient


def test_an_undone_kavita_read_is_not_imposed_again_while_kavita_stays_put(tmp_path: Path) -> None:
    """Contrôle (vert) : Kavita ne ré-impose pas un « lu » annulé ici tant qu'il ne bouge pas ;
    l'annulation n'est pas non plus propagée à Kavita, qui garde « lu »."""

    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.read_in_kavita(reader, chapter, chapter.pages)
        env.sync(reader)
        assert env.state(reader, chapter) == "read"
        with write_transaction(env.engine) as connection:
            assert undo_read_mark(connection, viewer_id=reader, subject_id=reader,
                                  unit_id=chapter.units[0], now=env.clock.now()) == 1  # fmt: skip
        assert env.state(reader, chapter) != "read"
        for _ in range(3):
            env.clock.advance(minutes=10)
            env.sync(reader)
        assert env.state(reader, chapter) != "read"
        assert env.kavita_page(reader, chapter) == chapter.pages  # Kavita garde sa marque
