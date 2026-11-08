"""ENF-OPS-009: technical retention. Events and finished jobs 90 days, audit one year (kept,
pseudonymised when the user is deleted), the recycle bin 30 days through `Fsops`."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.config import Settings
from shelfhold.identity.adapters.store import SqlIdentityStore
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import identity, metadata, platform
from shelfhold.jobs.retention import RetentionPolicy, expire_audit, expire_events_and_jobs
from shelfhold.retention_jobs import RECYCLE_KIND, RETENTION_KIND, sweep_recycle
from tests.l41_env import instance

NOW = datetime(2026, 11, 8, 4, tzinfo=UTC)


def days_ago(n: int, extra_hours: int = 0) -> datetime:
    return NOW - timedelta(days=n, hours=-extra_hours)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(tmp_path / "shelfhold.db")
    metadata.create_all(engine)
    yield engine
    engine.dispose()


def add_event(engine: Engine, at: datetime) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                sa.insert(platform.event).values(
                    type="job.finished", schema_version=1, payload={}, occurred_at=at,
                    visibility="ops",
                )
            ).inserted_primary_key[0]
        )  # fmt: skip


def add_job(engine: Engine, status: str, finished: datetime | None) -> str:
    job_id = new_ulid()
    with engine.begin() as conn:
        conn.execute(
            sa.insert(platform.job_view).values(
                id=job_id, kind="fetch", status=status, created_at=finished or NOW,
                finished_at=finished, dedupe_key=job_id,
            )
        )  # fmt: skip
    return job_id


@pytest.mark.req("ENF-OPS-009")
def test_events_and_jobs_purged_after_90_days(engine: Engine) -> None:
    old, edge, recent = (add_event(engine, days_ago(d)) for d in (91, 89, 1))
    done_old = add_job(engine, "succeeded", days_ago(91))
    failed_old = add_job(engine, "failed", days_ago(120))
    done_recent = add_job(engine, "succeeded", days_ago(10))
    dead_letter = add_job(engine, "dead_letter", days_ago(200))  # waits for a human: kept
    queued = add_job(engine, "queued", None)
    with engine.begin() as conn:
        conn.execute(
            sa.insert(platform.notification_pending).values(
                id=new_ulid(), event_id=old, channel="apprise", next_try_at=NOW
            )
        )
    report = expire_events_and_jobs(engine, now=NOW, policy=RetentionPolicy())
    assert (report.events, report.jobs) == (1, 2)
    with engine.connect() as conn:
        events = set(conn.execute(sa.select(platform.event.c.id)).scalars())
        jobs = set(conn.execute(sa.select(platform.job_view.c.id)).scalars())
        pending = conn.execute(sa.select(sa.func.count()).select_from(platform.notification_pending))
        assert pending.scalar_one() == 0  # cascades with its event
    assert events == {edge, recent}
    assert jobs == {done_recent, dead_letter, queued}
    assert not {done_old, failed_old} & jobs


@pytest.mark.req("ENF-OPS-009")
def test_retention_is_configurable() -> None:
    assert RetentionPolicy.from_settings(Settings()) == RetentionPolicy(90, 90, 365)
    custom = Settings(retention_events_days=30, retention_audit_days=400)
    assert RetentionPolicy.from_settings(custom) == RetentionPolicy(30, 30, 400)


def add_audit(engine: Engine, actor: str | None, at: datetime, target: str | None = None) -> str:
    audit_id = new_ulid()
    with engine.begin() as conn:
        conn.execute(
            sa.insert(identity.audit).values(
                id=audit_id, actor_id=actor, action="api_key.create", target=target, at=at
            )
        )
    return audit_id


def add_user(engine: Engine, name: str) -> str:
    user_id = new_ulid()
    with engine.begin() as conn:
        conn.execute(
            sa.insert(identity.user).values(id=user_id, username=name, role="admin", can_rate=True)
        )
    return user_id


@pytest.mark.req("ENF-OPS-009", "EF-USR-013")
def test_audit_kept_one_year_pseudonymized_on_user_delete(engine: Engine) -> None:
    gone, other = add_user(engine, "leaving"), add_user(engine, "staying")
    mine_recent = add_audit(engine, gone, days_ago(100))
    mine_older = add_audit(engine, gone, days_ago(300), target=gone)
    theirs = add_audit(engine, other, days_ago(100))
    too_old = add_audit(engine, other, days_ago(366))
    event_id = add_event(engine, days_ago(5))
    with engine.begin() as conn:
        conn.execute(sa.update(platform.event).values(actor_id=gone))
    store = SqlIdentityStore(engine)

    pseudonym = store.delete_user(gone)

    with engine.connect() as conn:
        rows = {r.id: r for r in conn.execute(sa.select(identity.audit))}
        users = set(conn.execute(sa.select(identity.user.c.username)).scalars())
        event_actor = conn.execute(
            sa.select(platform.event.c.actor_id).where(platform.event.c.id == event_id)
        ).scalar_one()
    assert users == {"staying"}
    assert pseudonym.startswith("deleted:") and gone not in pseudonym
    assert rows[mine_recent].actor_id == rows[mine_older].actor_id == pseudonym  # still linked
    assert rows[mine_older].target == pseudonym and rows[mine_older].action == "api_key.create"
    assert rows[mine_older].at == days_ago(300) and rows[mine_older].id == mine_older
    assert rows[theirs].actor_id == other
    assert event_actor == pseudonym
    assert store.delete_user(gone) is None  # nobody left to erase

    removed = expire_audit(engine, now=NOW, policy=RetentionPolicy())
    assert removed == 1
    with engine.connect() as conn:
        left = set(conn.execute(sa.select(identity.audit.c.id)).scalars())
    assert left == {mine_recent, mine_older, theirs}
    assert too_old not in left


def make_bin_entry(root: Path, name: str, age_days: int) -> Path:
    entry = root / ".shelfhold" / "recycle" / name
    entry.mkdir(parents=True)
    (entry / "chapter.cbz").write_bytes(b"synthetic")
    stamp = (datetime.now(UTC) - timedelta(days=age_days)).timestamp()  # Fsops reads the real clock
    os.utime(entry, (stamp, stamp))
    return entry


@pytest.mark.req("ENF-OPS-009", "EF-USR-024")
def test_recycle_purged_after_30_days(settings: Settings, tmp_path: Path) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        old = make_bin_entry(inst.library, "a" * 32, 31)
        fresh = make_bin_entry(inst.library, "b" * 32, 29)
        report = sweep_recycle(inst.comp)
        assert report.removed == 1 and report.kept == 1
        assert not old.exists()
        assert fresh.exists()
        with inst.engine.connect() as conn:  # the deletion left its intention, closed
            ops = conn.execute(sa.text("SELECT op, state FROM fs_intent")).all()
        assert [(r.op, r.state) for r in ops] == [("delete", "done")]


@pytest.mark.req("ENF-OPS-009")
def test_retention_jobs_are_registered_and_classed(settings: Settings, tmp_path: Path) -> None:
    from shelfhold.jobs.huey_app import JobPools
    from shelfhold.retention_jobs import register_retention_jobs
    from tests.fakes_clock import ManualClock

    with instance(settings, tmp_path) as inst:
        pools = JobPools(
            settings.data_dir, engine=inst.engine, clock=ManualClock(NOW), seed="r"
        )
        register_retention_jobs(pools, inst.comp)
        assert not pools.is_windowed(RETENTION_KIND)  # the database alone: no disk woken
        assert pools.is_windowed(RECYCLE_KIND)  # the bins are on /library: window only
