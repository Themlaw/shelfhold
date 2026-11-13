"""M3 L10: the read-only audit of the Kavita accounts mapped to child profiles.

Each profile of a child may name its Kavita account (EF-USR-017). The audit reads `GET /api/Users`
and the libraries, compares what each account reaches with the age of the child, and says so
(`kavita.account_audit`, EF-EXP-036). It never writes to Kavita: the simulated server records every
call, and the tests prove there is none that changes anything.
"""

from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.targets.kavita.accounts_audit import (
    ChildAccount,
    ChildAccountsAuditor,
    health_entry,
)
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, platform
from shelfhold.ops.kavita_accounts import KavitaAccountsOps
from shelfhold.ops.wiring import open_ops
from shelfhold.runtime_jobs import access_audit
from tests.auth_env import PASSWORD
from tests.export_env import export_env
from tests.fakes.kavita import member
from tests.kavita_env import run
from tests.kavita_export_env import LIBRARY_ID, Wired, wire
from tests.l41_env import instance

OTHER_LIBRARY = 8  # the second library of the simulated server: no shelf points at it
ADULT_ONLY = 18


def _wire(env: Any) -> Wired:
    """The simulated Kavita with its first library dedicated to the shelf: a library over `/manga`
    holds more than the shelf's folder, and is no family library (L10 review, B1)."""
    return wire(env)


def _child(max_age: int = 10, kavita: str | None = "kid") -> ChildAccount:
    return ChildAccount(profile_id=new_ulid(), max_age=max_age, kavita_username=kavita)


def _audit(w: Wired, *children: ChildAccount) -> Any:
    auditor = ChildAccountsAuditor(w.env.engine, w.env.clock, w.events)
    return run(auditor.audit(w.client, w.env.target_id, list(children)))


def _codes(report: Any, index: int = 0) -> set[str]:
    return {f.code for f in report.children[index].findings}


def _sound_account() -> dict[str, Any]:
    """A well set up child account: one family library, a tier of 10, no unknowns."""
    return member("kid", libraries=(LIBRARY_ID,), age_rating=5, include_unknowns=False)


@pytest.mark.req("EF-EXP-036")
def test_kavita_accounts_audit_flags_child_with_adult_library() -> None:
    with export_env(max_age_rating=10) as env:
        w = _wire(env)
        w.fake.users.append(
            member(
                "kid", libraries=(LIBRARY_ID, OTHER_LIBRARY), age_rating=5, include_unknowns=False
            )
        )
        report = _audit(w, _child(10))
        assert report.children[0].status == "alert"
        assert "non_family_library" in _codes(report)
        flagged = [f for f in report.children[0].findings if f.code == "non_family_library"]
        assert [f.library for f in flagged] == [str(OTHER_LIBRARY)]  # the one library, by ref
        # an account that reaches only the family library is sound
        w.fake.users[-1] = _sound_account()
        assert _audit(w, _child(10)).children[0].status == "ok"
        # a shelf with no ceiling (18+) is not a family library either
        with env.engine.begin() as connection:
            connection.execute(sa.update(export.shelf).values(max_age_rating=None))
        assert "non_family_library" in _codes(_audit(w, _child(10)))


@pytest.mark.req("EF-USR-017", "EF-EXP-036")
def test_declarative_kavita_mapping_audit_compares_max_age() -> None:
    with export_env(max_age_rating=13) as env:  # the library holds up to 13
        w = _wire(env)
        w.fake.users.append(_sound_account())
        # the library may hold what a 10 year old must not read
        assert "library_above_max_age" in _codes(_audit(w, _child(10)))
        assert _audit(w, _child(13)).children[0].status == "ok"
        # the restriction of the account is above the age of the child (tier 12 for a child of 10)
        w.fake.users[-1] = member(
            "kid", libraries=(LIBRARY_ID,), age_rating=7, include_unknowns=False
        )
        assert "restriction_above_max_age" in _codes(_audit(w, _child(10)))
        # no restriction at all, and the unknowns included: both are said
        w.fake.users[-1] = member("kid", libraries=(LIBRARY_ID,))
        codes = _codes(_audit(w, _child(13)))
        assert {"unrestricted", "includes_unknowns"} <= codes
        # a Kavita administrator reaches everything, whatever the profile says
        w.fake.users[-1] = member("kid", admin=True)
        assert "account_is_admin" in _codes(_audit(w, _child(13)))
        # the name is matched without regard to case, as Kavita does
        w.fake.users[-1] = _sound_account()
        assert _audit(w, _child(13, "KID")).children[0].status == "ok"
        # a name that no account answers to is an alert, not a pass
        assert "account_not_found" in _codes(_audit(w, _child(13, "ghost")))


@pytest.mark.req("EF-EXP-036")
def test_child_without_family_library_flagged() -> None:
    with export_env(max_age_rating=10) as env:
        w = _wire(env)
        w.fake.users.append(
            member("kid", libraries=(OTHER_LIBRARY,), age_rating=5, include_unknowns=False)
        )
        assert {"no_family_library", "non_family_library"} <= _codes(_audit(w, _child(10)))
        w.fake.users[-1] = member("kid", libraries=(), age_rating=5, include_unknowns=False)
        assert _codes(_audit(w, _child(10))) == {"no_family_library"}  # nothing, so nothing to add
        # no shelf below 18+ at all: no library is a family one, whatever the account reaches
        with env.engine.begin() as connection:
            connection.execute(sa.update(export.shelf).values(max_age_rating=None))
        w.fake.users[-1] = _sound_account()
        assert "no_family_library" in _codes(_audit(w, _child(10)))


@pytest.mark.req("EF-USR-017")
def test_child_without_mapping_is_reported_unmapped_and_never_guessed() -> None:
    with export_env(max_age_rating=10) as env:
        w = _wire(env)
        w.fake.users.append(member("kid", libraries=(OTHER_LIBRARY,)))  # alarming, but not mapped
        report = _audit(w, _child(10, kavita=None))
        assert report.children[0].status == "unmapped" and report.children[0].findings == ()
        assert w.events.codes() == []  # an account nobody declared is not audited by its name


@pytest.mark.req("EF-EXP-036")
def test_audit_never_writes_to_kavita() -> None:
    with export_env(max_age_rating=10) as env:
        w = _wire(env)
        w.fake.users.append(member("kid", libraries=(LIBRARY_ID, OTHER_LIBRARY)))
        _audit(w, _child(10), _child(7, "ghost"), _child(7, None))
        _audit(w, _child(10))  # twice: still nothing
        assert w.fake.writes == [] and w.fake.violations == []
        assert {r.method for r in w.fake.requests} == {"GET"}
        assert w.fake.requests_to("/api/Users")
        # nothing of Shelfhold's own shelves was touched either: the audit does not pause them
        assert [r.paused_reason for r in env.rows(export.shelf)] == [None]


@pytest.mark.req("EF-EXP-036")
def test_account_audit_is_notified_once_and_names_no_one() -> None:
    with export_env(max_age_rating=10) as env:
        w = _wire(env)
        w.fake.users.append(member("kid", libraries=(OTHER_LIBRARY,), include_unknowns=False))
        child = _child(10)
        _audit(w, child)
        (detail,) = w.events.details("kavita.account_audit")
        assert detail["target_id"] == env.target_id and detail["children"] == 1
        assert "kid" not in repr(detail) and str(child.profile_id) not in repr(detail)
        _audit(w, child)  # the next day, nothing changed: no second message
        assert w.events.codes() == ["kavita.account_audit"]
        w.fake.users[-1] = (
            _sound_account()
        )  # the gap is closed: the state is clean, nothing is said
        report = _audit(w, child)
        assert report.children[0].status == "ok" and w.events.codes() == ["kavita.account_audit"]
        w.fake.users[-1] = member("kid", libraries=(OTHER_LIBRARY,), include_unknowns=False)
        _audit(w, child)  # it opens again: said again
        assert w.events.codes() == ["kavita.account_audit"] * 2


@pytest.mark.req("EF-EXP-036")
def test_account_audit_that_cannot_read_kavita_fails_visibly_not_clean() -> None:
    with export_env(max_age_rating=10) as env:
        w = _wire(env)
        w.fake.users.append(_sound_account())
        w.fake.missing.add(("GET", "/api/users"))
        report = _audit(w, _child(10))
        assert report.failed and report.children == ()  # no child is reported "ok" unread
        assert "kavita.account_audit" not in w.events.codes() and w.fake.writes == []
        entry = health_entry(env.engine, env.target_id)
        assert entry["state"] == "unverified"


@pytest.mark.req("EF-EXP-036")
def test_a_target_without_a_usable_client_is_unverified_never_clean() -> None:
    with export_env(max_age_rating=10) as env:
        w = _wire(env)
        w.fake.users.append(_sound_account())
        auditor = ChildAccountsAuditor(env.engine, env.clock, w.events)

        def no_key(_target_id: str) -> Any:
            raise KeyError("sealed key")

        ops = KavitaAccountsOps(
            env.engine, auditor, targets=lambda: [env.target_id], client_for=no_key,
            clock=env.clock,
            children=lambda: [],
        )  # fmt: skip
        (entry,) = ops.run()
        assert entry["state"] == "unverified" and entry["alerts"] == 0
        assert w.fake.requests == []  # nothing was asked of anyone


@pytest.mark.req("EF-EXP-036")
def test_health_entry_for_the_health_page() -> None:
    with export_env(max_age_rating=10) as env:
        w = _wire(env)
        assert health_entry(env.engine, env.target_id)["state"] == "never_audited"
        w.fake.users.append(member("kid", libraries=(OTHER_LIBRARY,)))
        _audit(w, _child(10))
        entry = health_entry(env.engine, env.target_id)
        assert entry["state"] == "alert" and entry["alerts"] == 1
        assert entry["event"] == "kavita.account_audit"
        assert "kid" not in repr(entry)
        w.fake.users[-1] = _sound_account()
        _audit(w, _child(10))
        assert health_entry(env.engine, env.target_id)["state"] == "ok"


@pytest.mark.req("EF-EXP-036", "EF-USR-017")
def test_the_daily_job_and_the_ops_audit_children_through_the_connected_targets(
    settings: Settings, tmp_path: Any
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        auth = local_auth(inst.engine, settings)
        kid = auth.profile_add("kid", PASSWORD, "child", max_age_rating=10)
        auth.profile_set_kavita_username(kid, "kid.k")
        auth.profile_add("tiny", PASSWORD, "child", max_age_rating=5)  # nobody declared an account
        everything = tuple(int(lib["id"]) for lib in inst.kavita.libraries)
        inst.kavita.users.append(member("kid.k", libraries=everything))
        written = len(inst.kavita.writes)
        access_audit(inst.comp)()
        access_audit(inst.comp)()  # the next day: the same gap is not said twice
        with inst.engine.connect() as connection:
            rows = connection.execute(
                sa.select(platform.event.c.payload).where(
                    platform.event.c.type == "kavita.account_audit"
                )
            ).all()
        assert [set(r.payload) for r in rows] == [{"target_id", "children", "alerts"}]
        assert rows[0].payload["children"] == 2 and rows[0].payload["alerts"] == 1
        ops = open_ops(settings, engine=inst.engine, composition=inst.comp)
        (entry,) = ops.kavita_accounts.run()
        assert entry["state"] == "alert" and entry["alerts"] == 1 and entry["unmapped"] == 1
        codes = {f["code"] for c in entry["children"] for f in c["findings"]}
        assert {"non_family_library", "no_family_library", "unrestricted"} <= codes
        assert "kid.k" not in repr(entry) and len(inst.kavita.writes) == written  # read only


@pytest.mark.req("EF-EXP-036")
def test_two_accounts_that_kavita_confuses_are_ambiguous_never_the_first() -> None:
    with export_env(max_age_rating=10) as env:
        w = _wire(env)
        w.fake.users.append(member("Lea", libraries=(LIBRARY_ID,), age_rating=5))
        w.fake.users.append(member("LEA", admin=True))
        assert _codes(_audit(w, _child(10, "lea"))) == {"account_ambiguous"}
        assert _codes(_audit(w, _child(10, "Lea"))) != {"account_ambiguous"}  # an exact name wins


@pytest.mark.req("EF-EXP-036")
def test_one_audit_at_a_time_for_the_route() -> None:
    from shelfhold.ops.errors import AuditRunningError

    with export_env(max_age_rating=10) as env:
        w = _wire(env)
        auditor = ChildAccountsAuditor(env.engine, env.clock, w.events)
        ops = KavitaAccountsOps(
            env.engine, auditor, targets=lambda: [env.target_id], client_for=lambda _t: w.client,
            children=lambda: [], clock=env.clock,
        )  # fmt: skip
        assert ops._running.acquire(blocking=False)  # an audit is running
        with pytest.raises(AuditRunningError):
            ops.run()
        assert w.fake.requests == []  # nothing was asked of Kavita
        ops._running.release()
        assert ops.run()[0]["state"] == "ok"


@pytest.mark.req("EF-EXP-036")
def test_an_old_answer_is_not_ok_and_an_unreadable_one_is_unverified() -> None:
    from datetime import timedelta

    with export_env(max_age_rating=10) as env:
        w = _wire(env)
        w.fake.users.append(_sound_account())
        _audit(w, _child(10))
        now = env.clock.now()
        assert health_entry(env.engine, env.target_id, now=now)["state"] == "ok"
        assert health_entry(env.engine, env.target_id, now=now + timedelta(days=3))["state"] == (
            "unverified"
        )
