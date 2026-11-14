"""L14: a migrated database with five profiles, a real bus, the router and the sender wired as in
production, and a sink that records what would leave (no channel, no network, no sleep: the clock
is moved by hand)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.events.bus import EventBus, PublishedEvent
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, identity, platform
from shelfhold.integrations.apprise.render import Message
from shelfhold.integrations.notify import NotificationRouter, NotificationSender
from tests.auth_env import T0, ManualClock
from tests.request_env import ADMIN_ID, CHILD_ID, MEMBER_ID, OTHER_ID, SIBLING_ID, rig

__all__ = [
    "ADMIN_ID", "CHILD_ID", "MEMBER_ID", "OTHER_ID", "SIBLING_ID", "T0", "Env", "Sink", "world",
]  # fmt: skip


@dataclass
class Sink:
    sent: list[tuple[str, Message]] = field(default_factory=list)
    ok: bool = True

    def __call__(self, user_id: str, message: Message) -> bool:
        if not self.ok:
            return False
        self.sent.append((user_id, message))
        return True

    def channel(self) -> str:
        """Everything that left, whoever it was for: in production every message goes to the same
        channels, so this is what any reader of them sees."""
        return "\n".join(f"{m.title}\n{m.body}" for _u, m in self.sent)

    def to(self, user_id: str) -> list[Message]:
        return [m for u, m in self.sent if u == user_id]

    def text(self, user_id: str) -> str:
        return "\n".join(f"{m.title}\n{m.body}" for m in self.to(user_id))


@dataclass
class Env:
    engine: sa.Engine
    clock: ManualClock
    bus: EventBus
    router: NotificationRouter
    sender: NotificationSender
    sink: Sink
    withdrawals: list[int]  # what `pending_withdrawals` answers now (a one-item list)

    # -- the catalogue --------------------------------------------------------------------------
    def work(self, title: str, *, age: int | None = 6) -> str:
        from tests.integration.test_schema_catalog import add_edition, add_work

        extra: dict[str, Any] = {"age_rating": age, "rated_at": T0} if age is not None else {}
        with write_transaction(self.engine) as connection:
            work_id = add_work(connection, title=title, sort_title=title.lower(), **extra)
            add_edition(connection, work_id)
        return work_id

    def series(self, title: str, work_ids: list[str]) -> str:
        series_id = new_ulid()
        with write_transaction(self.engine) as connection:
            connection.execute(
                sa.insert(catalog.series).values(
                    id=series_id, title=title, sort_title=title.lower()
                )
            )
            for position, work_id in enumerate(work_ids, start=1):
                connection.execute(
                    sa.insert(catalog.series_member).values(
                        series_id=series_id, work_id=work_id, position=str(position)
                    )
                )
        return series_id

    def follow(self, user_id: str, work_id: str) -> None:
        with write_transaction(self.engine) as connection:
            connection.execute(
                sa.insert(identity.user_state).values(
                    user_id=user_id, work_id=work_id, followed=True
                )
            )

    def rate(self, work_id: str, age: int | None) -> None:
        with write_transaction(self.engine) as connection:
            connection.execute(
                sa.update(catalog.work)
                .where(catalog.work.c.id == work_id)
                .values(age_rating=age, rated_at=T0 if age is not None else None)
            )

    def profile(self, user_id: str, **columns: Any) -> None:
        with write_transaction(self.engine) as connection:
            connection.execute(
                sa.update(identity.user).where(identity.user.c.id == user_id).values(**columns)
            )

    # -- events ---------------------------------------------------------------------------------
    def publish(
        self, event_type: str, payload: dict[str, Any], *, actor: str | None = None,
        visibility: str = "ops",
    ) -> PublishedEvent:  # fmt: skip
        with write_transaction(self.engine) as connection:
            published = self.bus.publish(
                connection, event_type, payload, actor_id=actor, visibility=visibility
            )
        self.bus.deliver([published])
        return published

    def chapter(self, work_id: str, *, actor: str | None = None, visibility: str = "ops") -> None:
        self.publish(
            "unit.stored",
            {"unit_id": new_ulid(), "edition_id": new_ulid(), "work_id": work_id},
            actor=actor, visibility=visibility,
        )  # fmt: skip

    def request_arrived(self, requester: str, work_id: str, *, visibility: str = "private") -> str:
        """An approved request that has arrived, as `RequestService.arrived` leaves it."""
        request_id = new_ulid()
        with write_transaction(self.engine) as connection:
            connection.execute(
                sa.insert(identity.request).values(
                    id=request_id, requester_user_id=requester, target_kind="work_id",
                    target_value=work_id, state="arrived", visibility=visibility,
                    work_id=work_id,
                )
            )  # fmt: skip
        self.publish(
            "ops.request_arrived", {"request_id": request_id}, actor=requester,
            visibility=visibility,
        )  # fmt: skip
        return request_id

    def outage(self, event_type: str = "target.unreachable") -> None:
        self.publish(event_type, {"reason": "unreachable"})

    # -- time -----------------------------------------------------------------------------------
    def at(self, hour: int, minute: int = 0, *, day: int = 5) -> None:
        self.clock.current = T0.replace(day=day, hour=hour, minute=minute)

    def run(self) -> None:
        self.sender.run_once()

    def pending(self) -> list[Any]:
        with self.engine.connect() as connection:
            return list(connection.execute(sa.select(identity.notification_digest)))

    def advance(self, **delta: float) -> None:
        self.clock.advance(timedelta(**delta))

    def settings_rows(self, key: str) -> list[Any]:
        with self.engine.connect() as connection:
            return list(
                connection.execute(
                    sa.select(platform.app_setting).where(platform.app_setting.c.key == key)
                )
            )


@contextmanager
def world(settings: Settings, *, sink: Sink | None = None) -> Iterator[Env]:
    with rig(settings) as base:
        clock = base.clock
        bus = EventBus(clock)
        out = sink or Sink()
        withdrawals = [0]
        router = NotificationRouter(base.engine, clock)
        router.subscribe(bus)
        sender = NotificationSender(
            base.engine,
            clock,
            deliver=out,
            pending_withdrawals=lambda _c: withdrawals[0],
        )
        yield Env(base.engine, clock, bus, router, sender, out, withdrawals)
