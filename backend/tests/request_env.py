"""L35: a migrated database, three profiles, and the ports of the request service replaced by
fakes that count what they were asked (no source, no service, no network)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from shelfhold.acquisition.application.requests import (
    Estimate,
    PreparedAcquisition,
    Requester,
    RequestRefusedError,
    RequestService,
)
from shelfhold.catalog.public import RatingRefusedError, RatingService
from shelfhold.config import Settings
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import acquisition
from shelfhold.sdk.age import AgeRating
from tests.auth_env import T0, ManualClock

ADMIN_ID = "01J0000000000000000000ADMX"
MEMBER_ID = "01J0000000000000000000MEMB"
OTHER_ID = "01J0000000000000000000PEER"
CHILD_ID = "01J0000000000000000000KKKK"
SIBLING_ID = "01J0000000000000000000SBSB"


def principal(
    user_id: str, role: str, *, can_rate: bool = False,
    credential: Credential = Credential.SESSION, fresh: bool = True,
) -> Principal:  # fmt: skip
    return Principal(
        user_id=user_id, username=role, role=role, credential=credential,
        session_id=new_ulid() if credential is Credential.SESSION else None,
        key_id=new_ulid() if credential is Credential.API_KEY else None,
        auth_time=T0 if fresh else None, can_rate=can_rate,
    )  # fmt: skip


def admin(**kw: Any) -> Principal:
    return principal(ADMIN_ID, "admin", can_rate=True, **kw)


def requester(user_id: str, role: str, locale: str = "en") -> Requester:
    return Requester(user_id=user_id, role=role, locale=locale)


@dataclass
class FakePorts:
    """What the service asks of the rest of the application, counted."""

    kinds: dict[str, str] = field(default_factory=dict)  # source key -> "manga" | "novel"
    estimate_of: Estimate = field(default_factory=lambda: Estimate(units=3, size_bytes=1000))
    follows: list[tuple[str, str, str | None, str]] = field(default_factory=list)
    acquired: list[tuple[str, str, str | None, str]] = field(default_factory=list)
    filed: list[tuple[str, str, str]] = field(default_factory=list)
    shelf: str | None = None
    engine: sa.Engine | None = None
    fail_follow: Exception | None = None
    fail_start: Exception | None = None

    def kind_of(self, source_key: str) -> str:
        return self.kinds.get(source_key, "novel")

    def estimate(self, source_key: str, remote_id: str) -> Estimate:
        return self.estimate_of

    def follow(
        self, source_key: str, remote_id: str, *, requester_user_id: str | None, visibility: str
    ) -> None:  # fmt: skip
        if self.fail_follow is not None:
            raise self.fail_follow
        self.follows.append((source_key, remote_id, requester_user_id, visibility))

    def prepare(
        self, source_key: str, remote_id: str, *, requester_user_id: str | None, visibility: str
    ) -> PreparedAcquisition:
        """A novel's Work and Edition exist before anything is fetched."""
        assert self.engine is not None
        with self.engine.begin() as connection:
            work = bind_work(connection, source_key, remote_id)
            edition = connection.execute(
                sa.text("SELECT id FROM edition WHERE work_id = :w"), {"w": work}
            ).scalar_one()
        handle = f"{source_key}|{remote_id}|{requester_user_id}|{visibility}"
        return PreparedAcquisition(work, edition, handle)

    def start(self, handle: str | None) -> None:
        if self.fail_start is not None:
            raise self.fail_start
        assert handle is not None
        source_key, remote_id, user, visibility = handle.split("|")
        self.acquired.append((source_key, remote_id, None if user == "None" else user, visibility))

    def proposed_shelf(self, edition_id: str) -> str | None:
        return self.shelf

    def settle(
        self, work_id: str, edition_id: str | None, rating: AgeRating | None,
        shelf_id: str | None, *, principal: Principal,
    ) -> str | None:  # fmt: skip
        """The shelf first (it may refuse), then the rating, as the real port does in one go."""
        shelf = shelf_id or (self.proposed_shelf(edition_id) if edition_id else None)
        if edition_id is not None and shelf is not None:
            self.file(edition_id, shelf, approver_id=principal.user_id)
        if rating is not None:
            assert self.engine is not None
            with self.engine.begin() as connection:
                try:
                    RatingService().rate(connection, work_id, rating, principal=principal, now=T0)
                except RatingRefusedError:
                    raise RequestRefusedError("not_allowed") from None
        return shelf

    def file(self, edition_id: str, shelf_id: str, *, approver_id: str) -> None:
        self.filed.append((edition_id, shelf_id, approver_id))


def bind_work(
    connection: sa.Connection, source_key: str, remote_id: str, *, lang: str = "en",
    rated: bool = False,
) -> str:  # fmt: skip
    """A Work, an Edition and the binding that makes `remote_id` of `source_key` its source."""
    from tests.integration.test_schema_catalog import (
        add_binding,
        add_edition,
        add_source_item,
        add_work,
    )

    extra: dict[str, Any] = {"age_rating": 6, "rated_at": T0} if rated else {}
    work_id = add_work(connection, **extra)
    edition_id = add_edition(connection, work_id, lang=lang)
    item_id = add_source_item(connection, remote_id=remote_id)
    connection.execute(
        sa.update(acquisition.source_item)
        .where(acquisition.source_item.c.id == item_id)
        .values(source_key=source_key)
    )
    add_binding(connection, work_id, edition_id, item_id)
    return work_id


@dataclass
class Rig:
    engine: sa.Engine
    clock: ManualClock
    ports: FakePorts
    service: RequestService
    settings: Settings

    def rows(self, sql: str, **params: Any) -> list[Any]:
        with self.engine.connect() as connection:
            return list(connection.execute(sa.text(sql), params))

    def audit(self, action: str) -> list[Any]:
        return self.rows("SELECT actor_id, target FROM audit WHERE action = :a", a=action)

    def state_of(self, request_id: str) -> str:
        return str(self.rows("SELECT state FROM request WHERE id = :i", i=request_id)[0][0])

    def work(self, *, editions: bool = True, **extra: Any) -> str:
        from tests.integration.test_schema_catalog import add_edition, add_work

        with self.engine.begin() as connection:
            work_id = add_work(connection, **extra)
            if editions:
                add_edition(connection, work_id)
            return work_id


def add_users(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        for uid, name, role, ceiling, rate in (
            (ADMIN_ID, "admin", "admin", None, 1),
            (MEMBER_ID, "member", "member", None, 0),
            (OTHER_ID, "other", "member", None, 0),
            (CHILD_ID, "kid", "child", 6, 0),
            (SIBLING_ID, "sib", "child", 10, 0),
        ):
            connection.execute(
                'INSERT INTO "user" (id, username, role, can_rate, max_age_rating) '
                "VALUES (?, ?, ?, ?, ?)", (uid, name, role, rate, ceiling),
            )  # fmt: skip
        connection.commit()
    finally:
        connection.close()


@contextmanager
def rig(settings: Settings, *, ports: FakePorts | None = None) -> Iterator[Rig]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings)
    add_users(settings.database_path)
    engine = create_db_engine(settings.database_path)
    clock = ManualClock()
    fakes = ports or FakePorts()
    fakes.engine = engine
    try:
        yield Rig(engine, clock, fakes, RequestService(engine, clock, fakes), settings)
    finally:
        engine.dispose()
