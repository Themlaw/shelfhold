"""Harness of the read state pushed back to Suwayomi (M4 L17, EF-SRC-026).

The migrated database (`reading_env`), a simulated Suwayomi (`tests.fakes.suwayomi`, whose
`updateChapters` follows `ChapterMutation.kt` of the tag v2.4.2366) reached through the production
adapter (`SuwayomiReadState`), and chapters that exist on both sides: a Unit of the catalog bound
to a series of the simulated server, with a copy in `/store`. Nothing sleeps, nothing leaves the
process; the clock is moved by hand.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import sqlalchemy as sa

from shelfhold.acquisition.application.read_state_push import PushReport, push_read_state
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, platform, replica
from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService
from shelfhold.plugin_host.adapters.meta.suwayomi.read_state import SuwayomiReadState
from tests.fakes.suwayomi import BASE_URL, FakeSuwayomi, FakeSuwayomiFetcher
from tests.fakes_clock import ManualClock
from tests.reading_env import ReadingEnv, reading_env

START = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
SOURCE_KEY = "suwayomi:7001"
SERIES_URL = "/series-alpha"
PAGES = 20
_HASH = "sha256:" + "c3" * 32


@dataclass(frozen=True)
class Chapter:
    unit: str
    item: str  # the exported file that covers the Unit
    suwayomi_id: int
    url: str


@dataclass
class ReadStateEnv:
    reading: ReadingEnv
    fake: FakeSuwayomi
    fetcher: FakeSuwayomiFetcher
    clock: ManualClock
    binding: str
    chapters: list[Chapter] = field(default_factory=list)
    _count: int = 0

    @property
    def engine(self) -> sa.Engine:
        return self.reading.engine

    # ---------------------------------------------------------------- people
    def user(self, key: str, role: str = "member", **extra: Any) -> str:
        return self.reading.user(key, role=role, **extra)

    def set_owner(self, username: str | None) -> None:
        with write_transaction(self.engine) as connection:
            connection.execute(
                sa.delete(platform.app_setting).where(
                    platform.app_setting.c.key == "source.read_state_owner"
                )
            )
            if username is not None:
                connection.execute(
                    sa.insert(platform.app_setting).values(
                        key="source.read_state_owner", value=username
                    )
                )

    # ---------------------------------------------------------------- chapters
    def chapter(self, *, copied: bool = True) -> Chapter:
        """A Unit bound to a chapter of the simulated series (a new one on the server)."""
        self._count += 1
        number = self._count
        url = f"{SERIES_URL}/ch-{number:03d}"
        sid = 9000 + number
        manga = next(m for m in self.fake.state["mangas"] if m["url"] == SERIES_URL)
        manga["chapters"].append(
            {"id": sid, "url": url, "name": f"Chapter {number}", "chapterNumber": float(number),
             "sourceOrder": 100 + number, "scanlator": "Synthetic Team A",
             "uploadDate": "1760000000000"}
        )  # fmt: skip
        (unit,) = self.reading.units("ok", 1, first=100 + number)
        item = self.reading.file("ok", [unit], bounds=[(0, PAGES - 1)])
        with write_transaction(self.engine) as connection:
            connection.execute(
                sa.insert(catalog.unit_variant).values(
                    id=new_ulid(), unit_id=unit, source_binding_id=self.binding,
                    remote_unit_id=f"{SOURCE_KEY}:{url}", lang="fr",
                )
            )  # fmt: skip
            if copied:
                connection.execute(
                    sa.insert(replica.replica).values(
                        id=new_ulid(), unit_id=unit, location="store", rel_path=f"u{number}.cbz"
                    )
                )  # fmt: skip
        made = Chapter(unit, item, sid, url)
        self.chapters.append(made)
        return made

    # ---------------------------------------------------------------- what a profile read
    def traversal(self, user: str, chapter: Chapter) -> None:
        """A page-by-page reading: the Unit is marked read (`read_units`)."""
        self.reading.position(
            user, chapter.item, page=PAGES - 1, percent=1.0, server_ts=self.clock.now(),
            client_ts=self.clock.now(), device_id="reader-1", read_units=[chapter.unit],
            content_hash=_HASH,
        )  # fmt: skip

    def server_read(self, user: str, chapter: Chapter) -> None:
        """A state a server holds (Kavita says « read »): taken as it is."""
        self.reading.position(
            user, chapter.item, page=PAGES, percent=1.0, server_ts=self.clock.now(),
            client_ts=self.clock.now(), source="server", device_id="server:kavita",
            content_hash=_HASH,
        )  # fmt: skip

    def peek(self, user: str, chapter: Chapter) -> None:
        """A look at the last page: a position, never a reading."""
        self.reading.position(
            user, chapter.item, page=PAGES - 1, percent=1.0, server_ts=self.clock.now(),
            client_ts=self.clock.now(), device_id="reader-1", content_hash=_HASH,
        )  # fmt: skip

    def state(self, user: str, chapter: Chapter) -> str:
        with self.engine.connect() as connection:
            row = connection.execute(
                sa.text("SELECT state FROM reading_progress WHERE user_id=:u AND unit_id=:n"),
                {"u": user, "n": chapter.unit},
            ).first()
        return "unread" if row is None else str(row[0])

    # ---------------------------------------------------------------- Suwayomi
    def is_read_there(self, chapter: Chapter) -> bool:
        manga = next(m for m in self.fake.state["mangas"] if m["url"] == SERIES_URL)
        return bool(next(c for c in manga["chapters"] if c["id"] == chapter.suwayomi_id).get(
            "isRead", False
        ))  # fmt: skip

    def read_there(self, chapter: Chapter) -> None:
        manga = next(m for m in self.fake.state["mangas"] if m["url"] == SERIES_URL)
        next(c for c in manga["chapters"] if c["id"] == chapter.suwayomi_id)["isRead"] = True

    def calls(self) -> int:
        return len(self.fake.requests)

    def graphql_fields(self) -> set[str]:
        return {f for r in self.fake.requests for f in getattr(r, "fields", ())}

    def positions(self) -> int:
        with self.engine.connect() as connection:
            return int(
                connection.execute(sa.text("SELECT count(*) FROM reading_position")).scalar_one()
            )

    def notes(self) -> list[SimpleNamespace]:
        with self.engine.connect() as connection:
            rows = connection.execute(sa.text("SELECT * FROM read_state_push_note"))
            return [SimpleNamespace(**r._mapping) for r in rows]

    # ---------------------------------------------------------------- runs
    def push(
        self, *, awake: Callable[[], bool] = lambda: True, batch: int = 100,
        should_stop: Callable[[], bool] = lambda: False,
    ) -> PushReport:  # fmt: skip
        import asyncio

        remote = SuwayomiReadState(SuwayomiService(BASE_URL), self.fetcher)
        return asyncio.run(
            push_read_state(
                self.engine,
                remote,
                clock=self.clock,
                awake=awake,
                should_stop=should_stop,
                batch_size=batch,
            )
        )


@contextmanager
def read_state_env(tmp_path: Path) -> Iterator[ReadStateEnv]:
    with reading_env(tmp_path) as base:
        clock = ManualClock(START)
        fake = FakeSuwayomi(auth_mode="none")
        with write_transaction(base.engine) as connection:
            item = new_ulid()
            connection.execute(
                sa.insert(acquisition.source_item).values(
                    id=item, plugin_id="suwayomi", source_key=SOURCE_KEY, remote_id=SERIES_URL
                )
            )
            work = base.seed.works["ok"]
            binding = new_ulid()
            connection.execute(
                sa.insert(acquisition.source_binding).values(
                    id=binding, work_id=work, edition_id=base.seed.editions["ok"],
                    source_item_id=item, priority=1, active=True,
                )
            )  # fmt: skip
        base.user("root", role="admin")
        yield ReadStateEnv(base, fake, FakeSuwayomiFetcher(fake), clock, binding)
