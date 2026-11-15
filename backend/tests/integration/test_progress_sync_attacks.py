"""Attacks on the progress synchronisation (M4 L14b, EF-EXP-026, EF-USR-012, EF-USR-006).

There is no HTTP route in this lot (the synchronisation is a job), so the attacks are the ones a
service can suffer: a profile's data written with another profile's key, a key that opens another
account, one Kavita account for two profiles, a revoked or expired key, and a child whose ceiling
the synchronisation must not cross. A route, an API key or a device token cannot reach it: the
architecture test `test_no_interface_imports_the_sync` holds that door shut.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.db import write_transaction
from tests.fakes.kavita import API_KEY
from tests.sync_env import sync_env

REQ = ("EF-EXP-026", "EF-USR-012", "EF-USR-006")


def reader_calls(env, key: str):  # type: ignore[no-untyped-def]
    return env.calls(key, fragment="/api/reader/")


# ------------------------------------------------------------------ IDOR between profiles


@pytest.mark.req(*REQ)
def test_idor_a_profiles_positions_are_never_pushed_with_another_profiles_key(
    tmp_path: Path,
) -> None:
    with sync_env(tmp_path) as env:
        mine, theirs = env.member("mine"), env.member("theirs")
        chapter = env.chapter("Orchard Road", 1)
        env.device_position(theirs, chapter, 15)
        env.clock.advance(minutes=1)
        env.sync(mine)  # my pass: nothing of mine to say
        assert env.fake.progress_writes == []
        assert env.kavita_page(theirs, chapter) == 0
        env.sync(theirs)
        assert env.fake.progress_writes == [(env.names[theirs], chapter.chapter_id, 15)]


@pytest.mark.req(*REQ)
def test_idor_a_state_pulled_for_one_profile_never_lands_on_another(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        mine, theirs = env.member("mine"), env.member("theirs")
        chapter = env.chapter("Orchard Road", 1)
        env.read_in_kavita(theirs, chapter, 13)
        env.sync(mine)
        assert env.positions(mine) == [] and env.positions(theirs) == []
        env.sync(theirs)
        assert env.positions(mine) == []


# ------------------------------------------------------------------ another member's key


@pytest.mark.req(*REQ)
def test_a_sealed_key_moved_to_another_profiles_row_opens_nothing(tmp_path: Path) -> None:
    """The vault binds a key to its row: copied under another profile it cannot be opened, and the
    synchronisation says so with a closed code, without a single call."""
    with sync_env(tmp_path) as env:
        mine, theirs = env.member("mine"), env.member("theirs")
        chapter = env.chapter("Orchard Road", 1)
        env.device_position(mine, chapter, 5)
        with write_transaction(env.engine) as connection:
            stolen = connection.execute(
                sa.text(
                    "SELECT secret_nonce, secret_ct FROM destination_account WHERE user_id = :u"
                ),
                {"u": theirs},
            ).one()
            connection.execute(
                sa.text(
                    "UPDATE destination_account SET secret_nonce = :n, secret_ct = :c "
                    "WHERE user_id = :u"
                ),
                {"n": stolen[0], "c": stolen[1], "u": mine},
            )
        env.clock.advance(minutes=1)
        assert env.sync(mine).code == "token_unreadable"
        assert env.calls() == []


@pytest.mark.req(*REQ)
def test_a_key_that_now_opens_another_account_stops_that_profile(tmp_path: Path) -> None:
    """A rotated or swapped key that opens somebody else's Kavita account: the profile's own name
    on file is compared with who the key says it is, before anything is read or written."""
    with sync_env(tmp_path) as env:
        mine, theirs = env.member("mine"), env.member("theirs")
        chapter = env.chapter("Orchard Road", 1)
        env.device_position(mine, chapter, 5)
        env.fake.key_accounts[env.keys[mine]] = env.names[theirs]  # the key now opens their account
        env.clock.advance(minutes=1)
        assert env.sync(mine).code == "account_mismatch"
        assert [c.path.lower() for c in env.calls(fragment="/api/")] == ["/api/account"]
        assert env.fake.progress_writes == []
        assert env.positions(mine, source="server") == []


# ------------------------------------------------------------------ one Kavita account, two profiles


@pytest.mark.req(*REQ)
def test_a_kavita_account_shared_by_two_profiles_is_not_synchronised(tmp_path: Path) -> None:
    """The schema keeps a name unique per binding; two bindings of the same Kavita may still be
    given the same account by two profiles. Its progress would then be read as both, so neither
    is synchronised, and nothing is said of whose account it is."""
    with sync_env(tmp_path) as env:
        other_binding = env.second_destination()
        one = env.member("one", kavita_name="shared-account")
        two = env.member("two", kavita_name="shared-account", destination=other_binding)
        chapter = env.chapter("Orchard Road", 1)
        env.read_in_kavita(one, chapter, 9)
        assert env.sync(one).code == "account_shared"
        assert env.sync(two, other_binding).code == "account_shared"
        assert env.calls() == []
        assert env.positions(one) == [] and env.positions(two) == []


# ------------------------------------------------------------------ revoked, expired, refused


@pytest.mark.req(*REQ)
def test_a_revoked_key_stops_the_profile_with_a_closed_code(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader, steady = env.member("reader"), env.member("steady")
        chapter = env.chapter("Orchard Road", 1)
        env.device_position(reader, chapter, 5)
        env.device_position(steady, chapter, 6)
        env.revoke(reader)
        env.clock.advance(minutes=1)
        reports = env.sync_all()
        assert reports[steady].code == "ok"
        assert env.calls(env.keys[reader]) == []
        assert env.kavita_page(steady, chapter) == 6
        assert [w[0] for w in env.fake.progress_writes] == [env.names[steady]]


@pytest.mark.req(*REQ)
def test_a_key_revoked_during_a_run_is_not_used_for_the_next_call(tmp_path: Path) -> None:
    """`revoked_at` is read again before each call: a key revoked after the first chapter does not
    serve for the second."""
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        first, second = env.chapter("Orchard Road", 1), env.chapter("Orchard Road", 2)
        env.read_in_kavita(reader, first, 4)
        env.read_in_kavita(reader, second, 5)
        seen: list[str] = []

        def revoke_after_the_first(request) -> None:  # type: ignore[no-untyped-def]
            if "/reader/" in request.path.lower():
                seen.append(request.path)
                if len(seen) == 1:
                    env.revoke(reader)

        env.fake.hook = revoke_after_the_first
        outcome = env.sync(reader)
        assert outcome.code == "member_key_revoked"
        assert len(seen) == 1  # the first call was made, none after the revocation


@pytest.mark.req(*REQ)
def test_a_key_kavita_refuses_stops_that_profile_with_no_fallback(tmp_path: Path) -> None:
    """Expired or deleted on the Kavita side: 401. The profile stops (`token_refused`); the
    dedicated key is never tried in its place, and the other profiles go on."""
    with sync_env(tmp_path) as env:
        reader, steady = env.member("reader"), env.member("steady")
        chapter = env.chapter("Orchard Road", 1)
        env.device_position(reader, chapter, 5)
        env.device_position(steady, chapter, 6)
        env.fake.auth_keys[:] = [k for k in env.fake.auth_keys if k["key"] != env.keys[reader]]
        env.clock.advance(minutes=1)
        reports = env.sync_all()
        assert reports[reader].code == "token_refused"
        assert reports[steady].code == "ok"
        assert reader_calls(env, API_KEY) == []
        assert env.fake.progress_writes == [(env.names[steady], chapter.chapter_id, 6)]


# ------------------------------------------------------------------ a child


@pytest.mark.req(*REQ)
def test_a_child_is_neither_pulled_nor_pushed_above_her_ceiling(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        child = env.member("child", role="child")
        shown = env.chapter("Orchard Road", 1, edition_key="ok")
        hidden = env.chapter("Harbor Sixteen", 1, edition_key="above")
        env.read_in_kavita(child, shown, 4)
        env.read_in_kavita(child, hidden, 15)  # Kavita holds a state on a series she cannot see
        env.clock.advance(minutes=1)
        assert env.sync(child).code == "ok"
        asked = {c.query.get("chapterId") for c in env.calls(fragment="/api/reader/get-progress")}
        assert asked == {str(shown.chapter_id)}  # the hidden chapter was not even asked about
        assert [p.carrier_id for p in env.positions(child)] == [shown.carrier]  # and not stored
        assert env.kavita_page(child, hidden) == 15  # untouched


@pytest.mark.req(*REQ)
def test_a_position_a_child_has_above_a_lowered_ceiling_is_not_pushed(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        child = env.member("child", role="child", max_age_rating=16)
        chapter = env.chapter("Harbor Sixteen", 1, edition_key="above")
        env.device_position(child, chapter, 9)  # written while her ceiling allowed it
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.text("UPDATE user SET max_age_rating = 10 WHERE id = :u"), {"u": child}
            )
        env.clock.advance(minutes=1)
        env.sync(child)
        assert env.calls(fragment="/api/reader/") == []
        assert env.fake.progress_writes == []


@pytest.mark.req(*REQ)
def test_a_pulled_state_on_a_hidden_series_is_not_stored_by_any_path(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        child = env.member("child", role="child")
        hidden = env.chapter("Harbor Sixteen", 1, edition_key="above")
        env.read_in_kavita(child, hidden, 20)  # finished, on a series above her ceiling
        env.sync(child)
        assert env.positions(child) == []
        assert env.state(child, hidden) == "unread"


# ------------------------------------------------------------------ the door stays shut


@pytest.mark.req(*REQ)
def test_no_interface_imports_the_sync() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "shelfhold" / "interfaces"
    offenders = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or "", *(f"{node.module}.{a.name}" for a in node.names)]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any(n.endswith(("reading.application.sync", "kavita.progress")) for n in names):
                offenders.append(str(path.relative_to(root)))
    assert offenders == []
