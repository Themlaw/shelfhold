"""Harness of the relay from the e-reader to Kavita (M4 L50, ADR-0053 §5).

The whole chain in one process: the application with its kosync routes and its block of a series
(`tests.integration.test_l48_opds.make_world`), the exported chapters on a real disk
(`tests.bundle_env`), a simulated Kavita (`tests.fakes.kavita`, which follows `ReaderController` of
the tag v0.9.1.4) holding the same chapters, and the production adapter of the synchronisation
(`KavitaProgress`) with each member's own key, sealed by the application's own keyring. A key is
drawn at random by the simulated server, never written as a literal. The synchronisation's clock is
the real one plus an offset a test moves by hand: the positions the routes write carry the real
clock too, and the order of the two is the order of events.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from shelfhold.export.targets.kavita import AdminKey, KavitaClient
from shelfhold.export.targets.kavita.progress import KavitaProgress
from shelfhold.identity.adapters.destination_accounts import DestinationAccountStore
from shelfhold.identity.application.ports import AuditEntry
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.reading.application.sync import MemberOutcome, sync_destination, sync_member
from tests.bundle_env import Chapter, Exported, Library, library_for
from tests.fakes.kavita import API_KEY, BASE_URL, FakeKavita, build_fetcher
from tests.integration.test_l48_opds import World
from tests.sync_env import LIBRARY_ID, _Events, store_open

REMOTE = "/m"


class SkewedClock:
    """The real clock, plus an offset: what the synchronisation sees as now."""

    def __init__(self) -> None:
        self.offset = timedelta(0)

    def now(self) -> datetime:
        return datetime.now(UTC) + self.offset

    def advance(self, **delta: float) -> None:
        self.offset += timedelta(**delta)


@dataclass
class Relay:
    world: World
    lib: Library
    fake: FakeKavita
    clock: SkewedClock
    store: DestinationAccountStore
    keyring: Callable[[], Any]
    remote: KavitaProgress
    items: list[Exported] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)  # profile name -> Kavita account
    keys: dict[str, str] = field(default_factory=dict)  # profile name -> its key (random)
    series_id: int = 0

    @property
    def engine(self) -> sa.Engine:
        return self.world.seed.engine

    @property
    def destination(self) -> str:
        return self.lib.destination_id

    # ---------------------------------------------------------------- chapters
    def add_chapter(self, chapter: Chapter) -> Exported:
        item = self.lib.add(chapter)
        remote = f"{REMOTE}/{item.rel_path}"
        self.fake.chapter_pages[remote] = sum(1 for n, _ in chapter.pages if not n.endswith(".xml"))
        if not self.items:
            self.series_id = self.fake.add_series(
                "Series", f"{REMOTE}/Series", [remote], library_id=LIBRARY_ID
            )
        else:
            self.fake.files[self.series_id].append(remote)
        self.items.append(item)
        self.anchor(item)
        return item

    def anchor(self, item: Exported, *, later: bool = False) -> None:
        """What the exporter leaves of a content: an alias row whose `content_hash` is the file's
        (the anchor of a position, and of what Kavita's page is recorded on). A replaced file gets
        a later one."""
        digest = "sha256:" + hashlib.sha256(item.path.read_bytes()).hexdigest()
        with write_transaction(self.engine) as c:
            c.execute(
                sa.insert(export.document_alias).values(
                    export_item_id=item.item_id, file_hash=digest, content_hash=digest,
                    created_at=datetime.now(UTC) + timedelta(minutes=1 if later else 0),
                )
            )  # fmt: skip

    def replace_chapter(self, item: Exported, chapter: Chapter) -> None:
        """The file replaced by the exporter (new bytes, manifest and content following)."""
        self.lib.rewrite(item, chapter, record=True)
        self.anchor(item, later=True)

    def remote_path(self, item: Exported) -> str:
        return f"{REMOTE}/{item.rel_path}"

    def chapter_id(self, item: Exported) -> int:
        return self.fake.chapter_id(self.remote_path(item))

    def set_kavita_pages(self, item: Exported, pages: int) -> None:
        self.fake.chapter_pages[self.remote_path(item)] = pages

    # ---------------------------------------------------------------- people
    def user_id(self, name: str) -> str:
        with self.engine.connect() as c:
            return str(
                c.execute(sa.text("SELECT id FROM user WHERE username = :n"), {"n": name}).scalar_one()
            )

    def link(self, name: str, kavita_name: str | None = None) -> str:
        account = kavita_name or f"acct-{name}"
        secret = self.fake.add_member_key(account, roles=("Login",), age_rating=None)
        now = datetime.now(UTC)
        self.store.save(
            self.user_id(name), self.destination, account, secret, declared=None, now=now,
            entry=lambda replaced: AuditEntry(None, "destination_account.linked", now),
        )  # fmt: skip
        self.names[name], self.keys[name] = account, secret
        return secret

    def revoke(self, name: str) -> None:
        with write_transaction(self.engine) as connection:
            connection.execute(
                sa.text(
                    "UPDATE destination_account SET revoked_at = :t, secret_v = NULL, "
                    "secret_kid = NULL, secret_ct = NULL, secret_nonce = NULL, "
                    "token_fingerprint = NULL WHERE user_id = :u AND shelf_destination_id = :d"
                ),
                {"t": datetime.now(UTC), "u": self.user_id(name), "d": self.destination},
            )

    # ---------------------------------------------------------------- runs
    def sync_all(self, *, verify: bool = True) -> dict[str, MemberOutcome]:
        found = sync_destination(
            self.engine, self.remote, destination_id=self.destination, clock=self.clock,
            page_map_keyring=self.keyring if verify else None,
        )  # fmt: skip
        by_id = {self.user_id(n): n for n in self.names}
        return {by_id.get(u, u): o for u, o in found.items()}

    def sync(self, name: str, *, verify: bool = True) -> MemberOutcome:
        return sync_member(
            self.engine, self.remote, user_id=self.user_id(name), destination_id=self.destination,
            clock=self.clock, page_map_keyring=self.keyring if verify else None,
        )  # fmt: skip

    # ---------------------------------------------------------------- what Kavita holds
    def kavita_page(self, name: str, item: Exported) -> int:
        got = self.fake.member_progress.get((self.names[name], self.chapter_id(item)))
        return 0 if got is None else int(got["page"])

    def read_in_kavita(self, name: str, item: Exported, page: int) -> None:
        self.fake.read_in_kavita(self.names[name], self.chapter_id(item), page)

    def writes(self, name: str | None = None) -> list[Any]:
        found = [r for r in self.fake.requests_to("/api/reader/progress") if r.method == "POST"]
        return [r for r in found if name is None or r.headers.get("x-api-key") == self.keys[name]]

    def calls_of(self, name: str) -> list[Any]:
        return [r for r in self.fake.requests_to("/api/reader/") if r.headers.get("x-api-key") == self.keys[name]]

    # ---------------------------------------------------------------- what Shelfhold holds
    def member_rows(self) -> list[Any]:
        with self.engine.connect() as c:
            return list(
                c.execute(
                    sa.text(
                        "SELECT m.rank, m.export_item_id, m.page_map_state, m.kavita_pages "
                        "FROM bundle_member m JOIN bundle_version v ON v.id = m.version_id "
                        "WHERE v.purged_at IS NULL ORDER BY v.created_at, m.rank"
                    )
                )
            )

    def states(self) -> dict[str, str]:
        """The state of the page map of each chapter (by export item id)."""
        return {str(r.export_item_id): str(r.page_map_state) for r in self.member_rows()}

    def positions(self, name: str, source: str | None = None) -> list[Any]:
        query, params = "SELECT * FROM reading_position WHERE user_id = :u", {"u": self.user_id(name)}
        if source is not None:
            query, params["s"] = query + " AND source = :s", source
        with self.engine.connect() as c:
            rows = c.execute(sa.text(query + " ORDER BY server_ts, id"), params).all()
        return [{**r._mapping, "locator": json.loads(r.locator)} for r in rows]

    def unit_state(self, name: str, item: Exported) -> str:
        with self.engine.connect() as c:
            found = c.execute(
                sa.text("SELECT state FROM reading_progress WHERE user_id = :u AND unit_id = :n"),
                {"u": self.user_id(name), "n": item.unit_id},
            ).scalar()
        return "unread" if found is None else str(found)


def build_relay(world: World, *, key: str = "ok") -> Relay:
    """An empty Kavita, a destination of the Edition with the synchronisation on and no chapter."""
    root = world.env.settings.data_dir.parent / f"relay-{key}"
    lib = library_for(world.seed.engine, root, world.seed.editions[key])
    engine = world.seed.engine
    fake = FakeKavita()
    clock = SkewedClock()
    fake.wall = clock.now
    fake.key_expires_at = datetime.now(UTC) + timedelta(days=365)
    keyring = world.env.app.state.kosync_keyring
    vault = SecretVault(keyring())
    store = DestinationAccountStore(engine, lambda: vault)
    with write_transaction(engine) as c:
        target = c.execute(
            sa.select(export.shelf_destination.c.target_id).where(
                export.shelf_destination.c.id == lib.destination_id
            )
        ).scalar_one()
        c.execute(
            sa.update(export.export_target)
            .where(export.export_target.c.id == target)
            .values(config={"url": BASE_URL})
        )
        c.execute(
            sa.update(export.shelf_destination)
            .where(export.shelf_destination.c.id == lib.destination_id)
            .values(sync_progress=True)
        )
        remote_library = new_ulid()
        c.execute(
            sa.insert(export.remote_library).values(
                id=remote_library, export_target_id=target, remote_ref=str(LIBRARY_ID)
            )
        )
        c.execute(
            sa.insert(export.shelf_destination_remote_library).values(
                shelf_destination_id=lib.destination_id, export_target_id=target,
                remote_library_id=remote_library,
            )
        )  # fmt: skip
    admin = KavitaClient(build_fetcher(fake), BASE_URL, AdminKey(API_KEY), events=_Events(), clock=clock)
    remote = KavitaProgress(
        engine,
        admin_client=lambda _target: admin,
        fetcher_for=lambda _url: build_fetcher(fake),
        open_token=lambda user, destination: store_open(store, user, destination),
        sleep=lambda _s: None,
        structure_ttl_s=0.0,
    )  # fmt: skip
    return Relay(world, lib, fake, clock, store, keyring, remote)
