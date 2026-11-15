"""A small library for the reading tests (M4 L14a): profiles, a Work with Units, files that
cover them, and positions, over a database made by `create_all` (the migrated one is proved equal
in `test_migrated_ddl_equals_create_all`)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, identity, metadata, reading, storage
from tests.integration.test_schema_catalog import add_unit
from tests.integration.test_schema_storage_export import add_destination, add_target
from tests.visibility_env import Seed, seed_database

ARGON = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"
OLD_HASH = "sha256:" + "a1" * 32
NEW_HASH = "sha256:" + "b2" * 32
T0 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)


def hours(n: float) -> datetime:
    return T0 + timedelta(hours=n)


@dataclass
class ReadingEnv:
    engine: Engine
    seed: Seed
    users: dict[str, str] = field(default_factory=dict)

    def user(self, key: str, role: str = "member", **extra: Any) -> str:
        """One profile per key (a child gets a ceiling of 10)."""
        values: dict[str, Any] = {
            "id": new_ulid(), "username": key, "role": role, "can_rate": role == "admin",
            "password_hash": ARGON, **extra,
        }  # fmt: skip
        if role == "child" and "max_age_rating" not in extra:
            values["max_age_rating"] = 10
        with self.engine.begin() as connection:
            connection.execute(sa.insert(identity.user).values(**values))
        self.users[key] = str(values["id"])
        return str(values["id"])

    def units(self, edition_key: str, count: int) -> list[str]:
        with self.engine.begin() as connection:
            return [
                add_unit(connection, self.seed.editions[edition_key], str(n + 1))
                for n in range(count)
            ]

    def file(
        self, edition_key: str, units: list[str], *, bounds: list[tuple[int, int]] | None = None,
        content_hash: str | None = None,
    ) -> str:  # fmt: skip
        """An exported file that covers `units` in order; `bounds` are their pages (first, last).
        With `content_hash`, its current content has that hash."""
        with self.engine.begin() as connection:
            content_id = None
            if content_hash is not None:
                content_id = _content(connection, content_hash)
            item = new_ulid()
            connection.execute(
                sa.insert(export.export_item).values(
                    id=item, edition_id=self.seed.editions[edition_key], kind="volume",
                    content_id=content_id,
                )
            )  # fmt: skip
            for position, unit in enumerate(units):
                start, end = bounds[position] if bounds else (None, None)
                connection.execute(
                    sa.insert(export.unit_coverage).values(
                        carrier_type="export_item", carrier_id=item, unit_id=unit,
                        position=position, extent="full", origin="manual",
                        start_locator=None if start is None else {"page": start},
                        end_locator=None if end is None else {"page": end},
                    )
                )  # fmt: skip
        return item

    def position(
        self, user: str, carrier: str, *, percent: float | None = None, page: int | None = None,
        content_hash: str = NEW_HASH, client_ts: datetime = T0, server_ts: datetime | None = None,
        carrier_type: str = "export_item",
    ) -> str:  # fmt: skip
        """A position written straight into the table (what a client reported, server date
        included), the way an earlier version or a restore would have left it."""
        row_id = new_ulid()
        with self.engine.begin() as connection:
            connection.execute(
                sa.insert(reading.reading_position).values(
                    id=row_id, user_id=user, carrier_type=carrier_type, carrier_id=carrier,
                    content_hash=content_hash, locator={} if page is None else {"page": page},
                    percent=percent, client_ts=client_ts, server_ts=server_ts or client_ts,
                )
            )  # fmt: skip
        return row_id

    def alias(self, item: str, old_hash: str, content_hash: str) -> None:
        """`document_alias` remembers the file hash an old content had (a rewrite of the file)."""
        with self.engine.begin() as connection:
            connection.execute(
                sa.insert(export.document_alias).values(
                    export_item_id=item, file_hash=old_hash, content_hash=old_hash
                )
            )
            connection.execute(
                sa.insert(export.document_alias).values(
                    export_item_id=item, file_hash=content_hash, content_hash=content_hash
                )
            )

    def destination(self) -> str:
        """A binding of a shelf to a server (a tree transport)."""
        with self.engine.begin() as connection:
            target = add_target(connection)
            return add_destination(connection, self.seed.shelves["family"], target)

    def account(self, user: str, destination: str) -> str:
        """The account of a member on that binding, with a sealed token (random bytes: no secret
        is ever written in a test)."""
        account_id = new_ulid()
        with self.engine.begin() as connection:
            if connection.execute(sa.text("SELECT 1 FROM secret_key WHERE kid = 'k1'")).first() is None:
                connection.execute(sa.text("INSERT INTO secret_key (kid, state) VALUES ('k1', 'active')"))
            connection.execute(
                sa.insert(identity.destination_account).values(
                    id=account_id, user_id=user, shelf_destination_id=destination,
                    remote_user=f"remote-{account_id[-6:]}", secret_kid="k1", secret_v=1,
                    secret_nonce=os.urandom(12), secret_ct=os.urandom(32),
                    token_fingerprint=account_id[-12:],
                )
            )  # fmt: skip
        return account_id

    def progress(self, user: str) -> dict[str, sa.Row[Any]]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.text("SELECT * FROM reading_progress WHERE user_id = :u"), {"u": user}
            )
            return {str(r.unit_id): r for r in rows}


def _content(connection: Connection, content_hash: str) -> str:
    content_id = new_ulid()
    connection.execute(
        sa.insert(storage.content).values(
            id=content_id, kind="images", media_type="application/vnd.comicbook+zip",
            format="cbz", retention="keep", path=f"x/{content_id}.cbz",
            original_sha256=content_hash, content_hash=content_hash, store_provenance="received",
            size=10,
        )
    )  # fmt: skip
    return content_id


@contextmanager
def reading_env(tmp_path: Path) -> Iterator[ReadingEnv]:
    path = tmp_path / "reading.db"
    engine = create_db_engine(path)
    metadata.create_all(engine)
    engine.dispose()
    seed = seed_database(path)
    try:
        yield ReadingEnv(seed.engine, seed)
    finally:
        seed.engine.dispose()
