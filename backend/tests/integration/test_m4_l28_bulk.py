"""M4 L28 (EF-BIB-023, EF-BIB-036, EF-USR-024): bulk rating of the Works that wait in "À classer",
through the queue, with a preview and an explicit selection.

A real instance (database, job pools, the production rating service); nothing is simulated. The
order is the thing a preview freezes: the identifiers of the Works that wait and were chosen, the
age, a digest. Applying it queues **one** job that names only the order; the job reads the order
again, checks the selection under the write lock, and rates exactly those Works or none.

Every "never" is an attempt to break it: a Work outside the selection, a Work that was rated or
does not exist, an order of another profile, a forged or replayed or expired order, an actor who
lost the right between the order and the job, a key, a member, a child, a stale session.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, platform
from shelfhold.jobs.public import JobPools
from shelfhold.ops.bulk import BULK_RATE_JOB_KIND, BulkOps, register_bulk_jobs
from shelfhold.ops.errors import (
    CodedRefusal,
    InvalidChoiceError,
    StalePreviewError,
    UnknownSubjectError,
)
from shelfhold.ops.job_actions import reserved_job_action
from tests.fakes_clock import ManualClock
from tests.integration.test_family_shelves_real import family
from tests.integration.test_schema_catalog import add_edition, add_work
from tests.l50_env import Seeded, seeded

pytestmark = pytest.mark.req("EF-BIB-023", "EF-BIB-036", "EF-USR-024")

ADMIN = "01JADMIN00000000000000000A"
OTHER_ADMIN = "01JADMIN00000000000000000B"


def _principal(
    user_id: str, now: Any, *, role: str = "admin", credential: Credential = Credential.SESSION,
    stale: bool = False, can_rate: bool = True,
) -> Principal:  # fmt: skip
    return Principal(
        user_id, "root", role, credential,
        session_id=new_ulid() if credential is Credential.SESSION else None,
        key_id=None if credential is Credential.SESSION else new_ulid(),
        auth_time=now - timedelta(hours=3) if stale else now,
        can_rate=can_rate,
        scopes=("admin",) if credential is Credential.API_KEY else (),
    )  # fmt: skip


class _Rig:
    def __init__(self, env: Seeded, pools: JobPools, ops: BulkOps, clock: ManualClock) -> None:
        self.env, self.pools, self.ops, self.clock = env, pools, ops, clock
        self.engine = env.inst.engine

    def admin(self, **kw: Any) -> Principal:
        return _principal(ADMIN, self.clock.now(), **kw)

    def waiting(self, *titles: str) -> list[str]:
        """Works with an Edition nobody rated, on no shelf, while a family shelf exists: they
        wait in "À classer" (`rating_pending`)."""
        ids = []
        with write_transaction(self.engine) as connection:
            for title in titles:
                work = add_work(connection, title=title, sort_title=title.lower())
                add_edition(connection, work, lang="fr", shelf_id=None)
                ids.append(work)
        return ids

    def rated_at(self, work_id: str) -> Any:
        with self.engine.connect() as connection:
            return connection.execute(
                sa.select(catalog.work.c.rated_at).where(catalog.work.c.id == work_id)
            ).scalar_one()

    def age(self, work_id: str) -> int | None:
        with self.engine.connect() as connection:
            value = connection.execute(
                sa.select(catalog.work.c.age_rating).where(catalog.work.c.id == work_id)
            ).scalar_one()
        return None if value is None else int(value)

    def jobs(self) -> list[Any]:
        with self.engine.connect() as connection:
            return list(
                connection.execute(
                    sa.select(platform.job_view).where(platform.job_view.c.kind == BULK_RATE_JOB_KIND)
                )
            )

    def run(self) -> None:
        huey = self.pools.huey("io")
        while (task := huey.dequeue()) is not None:
            huey.execute(task)

    def job(self, job_id: str) -> Any:
        with self.engine.connect() as connection:
            return connection.execute(
                sa.select(platform.job_view).where(platform.job_view.c.id == job_id)
            ).one()

    def audit_rates(self) -> list[str]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                sa.text("SELECT actor_id, target FROM audit_log WHERE action = 'rate' ORDER BY id")
            )
            return [f"{r.actor_id}|{r.target}" for r in rows]

    def orders(self) -> int:
        with self.engine.connect() as connection:
            return int(connection.execute(sa.text("SELECT COUNT(*) FROM bulk_order")).scalar_one())

    def preview(self, age: str = "13+", **selection: Any) -> dict[str, Any]:
        return self.ops.rate_preview(age=age, principal=self.admin(), **selection)

    def queue(self, preview: dict[str, Any], **kw: Any) -> str:
        return self.ops.rate_queue(
            order_id=preview["order_id"], confirm=preview["token"],
            principal=kw.pop("principal", None) or self.admin(), **kw,
        )  # fmt: skip


@pytest.fixture
def rig(settings: Settings, tmp_path: Path) -> Iterator[_Rig]:
    with seeded(settings, tmp_path) as env:
        family(env)  # a shelf below 18+ exists: an unrated Edition waits out of it
        clock = ManualClock()
        engine = env.inst.engine
        with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as conn:
            for user_id, name in ((ADMIN, "root"), (OTHER_ADMIN, "second")):
                conn.execute(
                    "INSERT INTO user (id, username, role, can_rate) VALUES (?, ?, 'admin', 1)",
                    (user_id, name),
                )
        data = tmp_path / "queue"
        data.mkdir()
        pools = JobPools(data, engine=engine, clock=clock, seed="l28")
        ops = BulkOps(engine, clock)
        register_bulk_jobs(pools, ops)
        yield _Rig(env, pools, ops, clock)


@pytest.mark.req("EF-BIB-023")
def test_bulk_actions_via_queue_with_preview(rig: _Rig) -> None:
    """50 Works chosen, an age applied: a preview first, then one job (01, EF-BIB-023)."""
    ids = rig.waiting(*(f"Titre {n:02d}" for n in range(50)))
    chosen = ids[:50]
    preview = rig.preview(work_ids=chosen)
    assert len(preview["items"]) == 50 and preview["skipped"] == 0 and preview["age"] == "13+"
    assert {i["work_id"] for i in preview["items"]} == set(chosen)
    assert all(rig.rated_at(w) is None for w in chosen)  # a preview rates nothing
    assert rig.jobs() == [] and rig.orders() == 1  # and queues nothing

    job_id = rig.queue(preview)
    (job,) = rig.jobs()
    assert job.id == job_id and job.status == "queued" and job.kind == BULK_RATE_JOB_KIND
    assert all(rig.rated_at(w) is None for w in chosen)  # the order rates nothing: the job does
    assert preview["order_id"] in job.dedupe_key  # identifiers only: no age, no title, no token
    assert "13+" not in str(tuple(job)) and preview["token"] not in str(tuple(job))

    rig.run()
    assert rig.job(job_id).status == "succeeded"
    assert len(rig.jobs()) == 1  # one job for the fifty, not fifty jobs
    assert {rig.age(w) for w in chosen} == {13} and all(rig.rated_at(w) for w in chosen)
    lines = rig.audit_rates()
    assert len(lines) == 50 and all(line.startswith(f"{ADMIN}|work:") for line in lines)


@pytest.mark.req("EF-BIB-023", "EF-BIB-036")
def test_bulk_rate_applies_only_to_selection(rig: _Rig) -> None:
    """Whatever the list became between the preview and the job, only the chosen Works move."""
    a, b, c, d, e = rig.waiting("A", "B", "C", "D", "E")
    preview = rig.preview(work_ids=[a, b, c])
    (late,) = rig.waiting("Arrivé après l'aperçu")  # the catalogue changed under the preview
    job_id = rig.queue(preview)
    rig.run()
    assert rig.job(job_id).status == "succeeded"
    assert [rig.age(w) for w in (a, b, c)] == [13, 13, 13]
    for untouched in (d, e, late):
        assert rig.rated_at(untouched) is None and rig.age(untouched) is None


@pytest.mark.req("EF-BIB-023", "EF-BIB-036")
def test_a_chosen_work_that_changed_since_the_preview_stops_the_whole_job(rig: _Rig) -> None:
    """The server checks the selection again under the write lock: a Work rated by someone else
    meanwhile makes the preview stale, and not one Work is rated (all or none)."""
    a, b, c = rig.waiting("A", "B", "C")
    preview = rig.preview(work_ids=[a, b, c])
    with write_transaction(rig.engine) as connection:  # another admin rated B in the meantime
        connection.execute(
            sa.update(catalog.work)
            .where(catalog.work.c.id == b)
            .values(age_rating=16, rated_at=rig.clock.now())
        )
    job_id = rig.queue(preview)
    rig.run()
    job = rig.job(job_id)
    assert job.status in {"failed", "dead_letter"} and job.error_code == "bulk.stale"
    assert rig.age(b) == 16 and rig.age(a) is None and rig.age(c) is None
    assert rig.audit_rates() == []


@pytest.mark.req("EF-BIB-023", "EF-BIB-036")
def test_ids_outside_the_waiting_list_are_never_rated_and_never_named(rig: _Rig) -> None:
    """An id that is not waiting (rated, unknown, an adult Work on a shelf of its own) is skipped
    by the server: it is counted, not named, and stays as it was."""
    (waiting, rated) = rig.waiting("Attend", "Déjà classée")
    with write_transaction(rig.engine) as connection:
        connection.execute(
            sa.update(catalog.work)
            .where(catalog.work.c.id == rated)
            .values(age_rating=18, rated_at=rig.clock.now())
        )
    ghost = new_ulid()
    preview = rig.preview(work_ids=[waiting, rated, ghost])
    assert [i["work_id"] for i in preview["items"]] == [waiting] and preview["skipped"] == 2
    assert "Déjà classée" not in json.dumps(preview) and ghost not in json.dumps(preview)
    rig.queue(preview)
    rig.run()
    assert rig.age(waiting) == 13 and rig.age(rated) == 18  # the rated one was not lowered
    with pytest.raises(CodedRefusal):  # nothing waits in the selection: nothing to order
        rig.preview(work_ids=[rated, ghost])
    assert rig.orders() == 1


@pytest.mark.req("EF-BIB-023", "EF-BIB-036")
def test_a_frozen_filter_cannot_widen(rig: _Rig) -> None:
    """A filter is frozen by the preview into the identifiers it matched then: a Work that matches
    it afterwards is not in the order."""
    one, two, _other = rig.waiting("Alpha 1", "Alpha 2", "Beta")
    preview = rig.preview(filter={"text": "alpha"})
    assert {i["work_id"] for i in preview["items"]} == {one, two}
    (alpha3,) = rig.waiting("Alpha 3")
    rig.queue(preview)
    rig.run()
    assert rig.age(one) == 13 and rig.age(two) == 13
    assert rig.age(alpha3) is None  # it matches the filter, but it came after


@pytest.mark.req("EF-BIB-023", "EF-BIB-036")
def test_a_filter_and_a_list_together_or_neither_are_refused(rig: _Rig) -> None:
    (a,) = rig.waiting("A")
    for selection in ({"work_ids": [a], "filter": {"text": "a"}}, {}, {"work_ids": []}):
        with pytest.raises(InvalidChoiceError):
            rig.preview(**selection)
    assert rig.orders() == 0


@pytest.mark.req("EF-BIB-036", "EF-USR-024")
def test_the_order_of_another_profile_a_forged_one_and_an_expired_one_are_one_answer(
    rig: _Rig,
) -> None:
    (a,) = rig.waiting("A")
    preview = rig.preview(work_ids=[a])
    other = _principal(OTHER_ADMIN, rig.clock.now())
    answers = set()
    for order_id, who in (
        (preview["order_id"], other),  # IDOR: the order of someone else
        (new_ulid(), rig.admin()),  # forged: no such order
        ("../../etc/passwd", rig.admin()),  # not even an identifier
    ):
        with pytest.raises(UnknownSubjectError) as refused:
            rig.ops.rate_queue(order_id=order_id, confirm=preview["token"], principal=who)
        answers.add(str(refused.value))
    assert len(answers) == 1  # one answer: nothing tells an order that exists from one that does not
    assert str(preview["order_id"]) not in answers.pop()
    with pytest.raises(StalePreviewError):  # the right order, a token that is not its own
        rig.ops.rate_queue(order_id=preview["order_id"], confirm="1-00000000", principal=rig.admin())
    assert rig.jobs() == [] and rig.age(a) is None

    rig.clock.advance(timedelta(hours=1))  # an old preview is not an order any more
    with pytest.raises(UnknownSubjectError):
        rig.queue(preview)
    assert rig.jobs() == []


@pytest.mark.req("EF-BIB-023", "EF-USR-024")
def test_an_order_applies_once(rig: _Rig) -> None:
    (a,) = rig.waiting("A")
    preview = rig.preview(work_ids=[a])
    rig.queue(preview)
    with pytest.raises(CodedRefusal):
        rig.queue(preview)  # replayed: refused, never a second job
    assert len(rig.jobs()) == 1
    rig.run()
    with pytest.raises(CodedRefusal):
        rig.queue(preview)  # and not after the job ran either
    assert len(rig.jobs()) == 1


@pytest.mark.req("EF-BIB-036", "EF-USR-024")
@pytest.mark.parametrize("kind", ["stale", "key", "member", "child", "no_right"])
def test_bulk_rate_requires_rate_right_and_fresh_auth(rig: _Rig, kind: str) -> None:
    """Neither door gives anything to a stale session, a key, a member, a child, or an admin who
    holds no right to classify: no order, no job, nothing rated."""
    (a,) = rig.waiting("A")
    now = rig.clock.now()
    who = {
        "stale": _principal(ADMIN, now, stale=True),
        "key": _principal(ADMIN, now, credential=Credential.API_KEY),
        "member": _principal(ADMIN, now, role="member"),
        "child": _principal(ADMIN, now, role="child", can_rate=False),
        "no_right": _principal(ADMIN, now, can_rate=False),
    }[kind]
    with pytest.raises(CodedRefusal) as refused:
        rig.ops.rate_preview(age="13+", work_ids=[a], principal=who)
    assert refused.value.code == "forbidden"
    assert rig.orders() == 0
    good = rig.preview(work_ids=[a])
    with pytest.raises(CodedRefusal) as refused:
        rig.ops.rate_queue(order_id=good["order_id"], confirm=good["token"], principal=who)
    assert refused.value.code == "forbidden"
    assert rig.jobs() == [] and rig.age(a) is None


@pytest.mark.req("EF-BIB-036", "EF-USR-024")
def test_an_actor_who_lost_the_right_before_the_job_ran_rates_nothing(rig: _Rig) -> None:
    a, b = rig.waiting("A", "B")
    preview = rig.preview(work_ids=[a, b])
    job_id = rig.queue(preview)
    with write_transaction(rig.engine) as connection:  # the right is withdrawn while it waits
        connection.execute(sa.text("UPDATE user SET can_rate = 0 WHERE id = :u"), {"u": ADMIN})
    rig.run()
    job = rig.job(job_id)
    assert job.status in {"failed", "dead_letter"} and job.error_code == "bulk.refused"
    assert rig.age(a) is None and rig.age(b) is None and rig.audit_rates() == []


@pytest.mark.req("EF-USR-024")
def test_the_pause_of_the_queue_parks_the_job_and_nothing_is_rated(rig: _Rig) -> None:
    (a,) = rig.waiting("A")
    rig.pools.pause_writes("disk.low")
    job_id = rig.queue(rig.preview(work_ids=[a]))
    rig.run()
    assert rig.job(job_id).status == "paused" and rig.age(a) is None
    rig.pools.resume_writes("disk.low")
    rig.run()
    assert rig.job(job_id).status == "succeeded" and rig.age(a) == 13


@pytest.mark.req("EF-USR-024")
def test_bringing_the_job_back_is_the_reserved_action_rate(rig: _Rig) -> None:
    (a,) = rig.waiting("A")
    job_id = rig.queue(rig.preview(work_ids=[a]))
    with rig.engine.connect() as connection:
        assert reserved_job_action(connection, job_id) == "rate"
