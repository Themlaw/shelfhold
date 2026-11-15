"""Harness of the progress synchronisation with Kavita (M4 L14b, EF-EXP-026, EF-LEC-014).

The migrated database (`reading_env`), a simulated Kavita (`tests.fakes.kavita`, whose progress
routes follow `ReaderController` of the tag v0.9.1.4) reached through the production fetcher, the
production adapter (`KavitaProgress`) and the real sealed keys of the members: a member's key is
drawn at random by the simulated server and sealed by the real vault, never written as a literal.
Nothing sleeps: the adapter's pause is a recorded no-op and the clock is moved by hand.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import sqlalchemy as sa

from shelfhold.export.targets.kavita import AdminKey, KavitaClient
from shelfhold.export.targets.kavita.progress import KavitaProgress
from shelfhold.identity.adapters.destination_accounts import DestinationAccountStore
from shelfhold.identity.application.ports import AuditEntry
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from shelfhold.infrastructure.secrets.diagnostics import boot_check
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.reading.application.sync import MemberOutcome, sync_destination, sync_member
from tests.fakes.kavita import API_KEY, BASE_URL, FakeKavita, build_fetcher
from tests.fakes_clock import ManualClock
from tests.reading_env import ReadingEnv, reading_env

START = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
LOCAL_ROOT = "/library"
REMOTE_ROOT = "/data/comics"
LIBRARY_ID = 7
SYNTHETIC_PASSWORD = "synthetic-password-for-tests"  # noqa: S105 (never a real one)


@dataclass(frozen=True)
class Chapter:
    """One exported file Kavita indexed as a chapter."""

    carrier: str  # export_item id
    units: list[str]
    path: str  # the file's path in Kavita
    chapter_id: int
    pages: int


@dataclass
class SyncEnv:
    reading: ReadingEnv
    fake: FakeKavita
    clock: ManualClock
    store: DestinationAccountStore
    remote: KavitaProgress
    destination: str
    target: str
    sleeps: list[float] = field(default_factory=list)
    keys: dict[str, str] = field(default_factory=dict)  # user id -> the member's key (random)
    names: dict[str, str] = field(default_factory=dict)  # user id -> the Kavita account name
    _series: dict[str, int] = field(default_factory=dict)
    _count: int = 0

    @property
    def engine(self) -> sa.Engine:
        return self.reading.engine

    # ---------------------------------------------------------------- people
    def member(
        self, key: str, *, role: str = "member", kavita_name: str | None = None, link: bool = True,
        destination: str | None = None, **extra: Any,
    ) -> str:  # fmt: skip
        """A profile and, with `link`, its own key on the destination: a random key of a Kavita
        account of its own, sealed by the vault (what L12 does when a member pastes it)."""
        user = self.reading.user(key, role=role, **extra)
        if link:
            self.link(user, kavita_name or f"acct-{key}", destination=destination)
        return user

    def link(self, user: str, kavita_name: str, *, destination: str | None = None) -> str:
        secret = self.fake.add_member_key(kavita_name, roles=("Login",), age_rating=None)
        dest = destination or self.destination
        self.store.save(
            user, dest, kavita_name, secret, declared=None, now=START,
            entry=lambda replaced: AuditEntry(None, "destination_account.linked", START),
        )  # fmt: skip
        self.keys[user] = secret
        self.names[user] = kavita_name
        return secret

    def revoke(self, user: str, destination: str | None = None) -> None:
        with write_transaction(self.engine) as connection:
            connection.execute(
                sa.text(
                    "UPDATE destination_account SET revoked_at = :t, secret_v = NULL, "
                    "secret_kid = NULL, secret_ct = NULL, secret_nonce = NULL, "
                    "token_fingerprint = NULL "
                    "WHERE user_id = :u AND shelf_destination_id = :d"
                ),
                {"t": START, "u": user, "d": destination or self.destination},
            )

    # ---------------------------------------------------------------- files
    def chapter(
        self, series: str, number: int, *, pages: int = 20, edition_key: str = "ok",
        units: list[str] | None = None, bounds: list[tuple[int, int]] | None = None,
        ext: str = "cbz",
    ) -> Chapter:  # fmt: skip
        """A file of the shelf, held by Kavita as a chapter of `series` (one Unit by default)."""
        if units is None:
            units = self.reading.units(edition_key, 1, first=self._count + 1)
        self._count += len(units)
        item = self.reading.file(
            edition_key, units, bounds=bounds or [(0, pages - 1)] * len(units),
            content_hash="sha256:" + f"{self._count:02x}" * 32,
        )  # fmt: skip
        rel = f"{series}/{series} - {number:03d}.{ext}"
        with write_transaction(self.engine) as connection:
            connection.execute(
                sa.insert(export.export_record).values(
                    shelf_destination_id=self.destination,
                    export_item_id=item,
                    locator_type="path",
                    rel_path=rel,
                    state="resolved",
                )
            )
        path = f"{REMOTE_ROOT}/manga/{rel}"
        self.fake.chapter_pages[path] = pages
        if series in self._series:
            self.fake.files[self._series[series]].append(path)
        else:
            self._series[series] = self.fake.add_series(
                series, f"{REMOTE_ROOT}/manga/{series}", [path], library_id=LIBRARY_ID
            )
        return Chapter(item, units, path, self.fake.chapter_id(path), pages)

    # ---------------------------------------------------------------- positions
    def device_position(
        self, user: str, chapter: Chapter, page: int, *, device: str = "reader-1",
        client_ts: datetime | None = None, server_ts: datetime | None = None,
    ) -> str:  # fmt: skip
        """A report of a reader on the file (KOReader, the integrated reader): a `device` position
        at the instant of the (hand-moved) server clock."""
        now = self.clock.now()
        return self.reading.position(
            user,
            chapter.carrier,
            page=page,
            percent=page / chapter.pages,
            content_hash=self.content_hash(chapter),
            client_ts=client_ts or now,
            server_ts=server_ts or now,
            device_id=device,
        )

    def content_hash(self, chapter: Chapter) -> str:
        with self.engine.connect() as connection:
            return str(
                connection.execute(
                    sa.text(
                        "SELECT c.content_hash FROM export_item i JOIN content c "
                        "ON c.id = i.content_id WHERE i.id = :i"
                    ),
                    {"i": chapter.carrier},
                ).scalar()
            )

    # ---------------------------------------------------------------- Kavita
    def kavita_page(self, user: str, chapter: Chapter) -> int:
        got = self.fake.member_progress.get((self.names[user], chapter.chapter_id))
        return 0 if got is None else int(got["page"])

    def read_in_kavita(self, user: str, chapter: Chapter, page: int) -> None:
        self.fake.read_in_kavita(self.names[user], chapter.chapter_id, page)

    def calls(self, key: str | None = None, *, fragment: str = "/api/reader/") -> list[Any]:
        found = self.fake.requests_to(fragment)
        return [r for r in found if key is None or r.headers.get("x-api-key") == key]

    # ---------------------------------------------------------------- runs
    def sync(self, user: str, destination: str | None = None) -> MemberOutcome:
        return sync_member(
            self.engine, self.remote, user_id=user,
            destination_id=destination or self.destination, clock=self.clock,
        )  # fmt: skip

    def sync_all(self, destination: str | None = None) -> dict[str, MemberOutcome]:
        return sync_destination(
            self.engine, self.remote, destination_id=destination or self.destination,
            clock=self.clock,
        )  # fmt: skip

    # ---------------------------------------------------------------- what is stored
    def positions(self, user: str, *, source: str | None = None) -> list[SimpleNamespace]:
        query = "SELECT * FROM reading_position WHERE user_id = :u"
        params: dict[str, Any] = {"u": user}
        if source is not None:
            query += " AND source = :s"
            params["s"] = source
        with self.engine.connect() as connection:
            found = connection.execute(sa.text(query + " ORDER BY server_ts, id"), params)
            return [
                SimpleNamespace(**{**row._mapping, "locator": json.loads(row.locator)})
                for row in found
            ]

    def state(self, user: str, chapter: Chapter) -> str:
        with self.engine.connect() as connection:
            states = [
                str(r[0])
                for r in connection.execute(
                    sa.text(
                        "SELECT state FROM reading_progress WHERE user_id = :u AND unit_id = :n"
                    ),
                    {"u": user, "n": chapter.units[0]},
                )
            ]
        return states[0] if states else "unread"

    def second_destination(self) -> str:
        """Another binding to the same Kavita (another shelf), with the same library."""
        with write_transaction(self.engine) as connection:
            shelf = new_ulid()
            connection.execute(
                sa.insert(export.shelf).values(id=shelf, name=f"s-{shelf}", phrase="Comics")
            )
            dest = new_ulid()
            connection.execute(
                sa.insert(export.shelf_destination).values(
                    id=dest,
                    shelf_id=shelf,
                    target_id=self.target,
                    transport="tree",
                    egress="foyer",
                    root_subpath="comics",
                    template="{series}",
                    template_origin="default@1",
                    sync_progress=True,
                )
            )
        return dest


@contextmanager
def sync_env(tmp_path: Path, *, enabled: bool = True) -> Iterator[SyncEnv]:
    with reading_env(tmp_path) as env:
        settings = _settings(tmp_path)
        clock = ManualClock(START)
        fake = FakeKavita()
        fake.wall = clock.now
        fake.key_expires_at = START.replace(year=2027)
        fetcher = build_fetcher(fake)
        vault = SecretVault(boot_check(settings))
        store = DestinationAccountStore(env.engine, lambda: vault)
        target = new_ulid()
        shelf = env.seed.shelves["family"]
        dest = new_ulid()
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.insert(export.export_target).values(
                    id=target, kind="kavita", name=f"kavita-{target}",
                    config={"url": BASE_URL},
                    path_map=[{"local": LOCAL_ROOT, "remote": REMOTE_ROOT, "mode": "rw"}],
                )
            )  # fmt: skip
            connection.execute(
                sa.insert(export.shelf_destination).values(
                    id=dest,
                    shelf_id=shelf,
                    target_id=target,
                    transport="tree",
                    egress="foyer",
                    root_subpath="manga",
                    template="{series}/{series} - {number}",
                    template_origin="default@1",
                    sync_progress=enabled,
                )
            )
            library = new_ulid()
            connection.execute(
                sa.insert(export.remote_library).values(
                    id=library, export_target_id=target, remote_ref=str(LIBRARY_ID)
                )
            )
            connection.execute(
                sa.insert(export.shelf_destination_remote_library).values(
                    shelf_destination_id=dest, export_target_id=target, remote_library_id=library
                )
            )
        admin = KavitaClient(fetcher, BASE_URL, AdminKey(API_KEY), events=_Events(), clock=clock)
        sleeps: list[float] = []
        remote = KavitaProgress(
            env.engine,
            admin_client=lambda _target: admin,
            fetcher_for=lambda _url: build_fetcher(fake),
            open_token=lambda user, destination: store_open(store, user, destination),
            sleep=sleeps.append,
            structure_ttl_s=0.0,  # a test moves Kavita's files at will
        )
        yield SyncEnv(env, fake, clock, store, remote, dest, target, sleeps)


def store_open(store: DestinationAccountStore, user: str, destination: str) -> Any:
    from shelfhold.identity.application.destination_accounts import SecretToken

    value = store.open(user, destination)
    return None if value is None else SecretToken(value)


class _Events:
    def emit(self, code: str, **detail: Any) -> None:  # the port of the client: nobody listens
        return None


def _settings(tmp_path: Path) -> Any:
    from shelfhold.config import load_settings

    return load_settings(
        {
            "SHELFHOLD_DISK_PAUSE_PERCENT": "0",
            "SHELFHOLD_DISK_PAUSE_GIB": "0",
            "SHELFHOLD_DATA_DIR": str(tmp_path / "data"),
            "SHELFHOLD_STORE_DIR": str(tmp_path / "store"),
        }
    ).values


def rows(db_path: Path, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(db_path)) as db:
        return db.execute(sql, params).fetchall()
