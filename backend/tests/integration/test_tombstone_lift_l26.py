"""M3 L26 (EF-BIB-054 (7), 06 Q54): `tombstone lift`, the way out of a block the admin decides.

A reserved action, audited without any value, which restores nothing by itself. The lift is a line
of its own in the table and in every journal (append only), so it survives a restore and a replay;
a lifted tombstone no longer blocks, and a purge that follows lays a new one. Real cipher, real
journals, real SQLite.
"""

from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.schema import coherence, identity, platform
from shelfhold.library.tombstones import PlaceRef, VariantRef
from shelfhold.library.tombstones.lift import (
    LiftConfirmationError,
    LiftRefusedError,
    UnknownTombstoneError,
    lift_phrase,
)
from tests.intents_env import NOW
from tests.tombstone_env import ADMIN, PLACE, SLUG, TITLE, VARIANT, TombEnv, make_tomb_env

pytestmark = pytest.mark.req("EF-BIB-054")

KEY = Principal(
    ADMIN.user_id, "admin", "admin", Credential.API_KEY, key_id="01J0000000000000000000KEY0"
)
MEMBER = Principal(
    "01J00000000000000000000MEM", "member", "member", Credential.SESSION,
    session_id="01J0000000000000000000SES1", auth_time=NOW - timedelta(seconds=30),
)  # fmt: skip
CHILD = Principal(
    "01J00000000000000000000CHD", "child", "child", Credential.SESSION,
    session_id="01J0000000000000000000SES2", auth_time=NOW - timedelta(seconds=30),
)  # fmt: skip
FRESH = Principal(
    ADMIN.user_id, "admin", "admin", Credential.SESSION,
    session_id="01J0000000000000000000SES3", auth_time=NOW - timedelta(seconds=30),
)  # fmt: skip
STALE = Principal(
    ADMIN.user_id, "admin", "admin", Credential.SESSION,
    session_id="01J0000000000000000000SES4", auth_time=NOW - timedelta(hours=3),
)  # fmt: skip


def _laid(tmp_path: Path) -> tuple[TombEnv, object, int]:
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    recorded = service.record(VARIANT, env.roots)
    assert service.check(VARIANT, root_id=env.library.root_id).blocked
    return env, service, recorded.seq


def _lift(service: object, env: TombEnv, seq: int, **kw: object) -> object:
    kw.setdefault("principal", ADMIN)
    kw.setdefault("confirmation", lift_phrase(seq))
    kw.setdefault("now", NOW)
    roots = kw.pop("roots", env.roots)
    return service.lift(roots, seq=seq, **kw)  # type: ignore[attr-defined]


def _audit(env: TombEnv, action: str) -> list[dict[str, object]]:
    with env.engine.connect() as connection:
        rows = connection.execute(
            sa.select(identity.audit).where(identity.audit.c.action == action)
        ).mappings()
        return [dict(r) for r in rows]


def test_tombstone_lift_reserved_and_audited_without_title(tmp_path: Path) -> None:
    """A lifted tombstone no longer blocks; the audit names the sequence and the actor, never a
    value; the journal and the table hold no readable title either."""
    env, service, seq = _laid(tmp_path)

    done = _lift(service, env, seq, principal=FRESH)

    assert done.seq == seq  # type: ignore[attr-defined]
    assert not service.check(VARIANT, root_id=env.library.root_id).blocked  # type: ignore[attr-defined]
    assert not service.check(VARIANT, root_id=None).blocked  # type: ignore[attr-defined]
    [entry] = _audit(env, "tombstone.lifted")
    assert entry["actor_id"] == FRESH.actor_id
    assert entry["session_id"] == FRESH.session_id
    assert entry["target"] == f"seq:{seq}"
    haystack = repr(_audit(env, "tombstone.lifted")) + repr(env.table_rows())
    for root in env.roots:
        haystack += repr(env.journal(root))
    for value in (TITLE, SLUG, "4422", "suwayomi"):
        assert value not in haystack
    for root in env.roots:
        assert b"suwayomi" not in env.journal(root)  # sealed, like the line it lifts


@pytest.mark.parametrize(
    "who", [KEY, MEMBER, CHILD, STALE], ids=["key", "member", "child", "stale"]
)
def test_tombstone_lift_refused_without_the_right(tmp_path: Path, who: Principal) -> None:
    """A key (admin scope too), a member, a child, a stale session: refused, and the tombstone
    still blocks, nothing is written (table, journals, audit)."""
    env, service, seq = _laid(tmp_path)
    table, journals = env.table_rows(), [env.journal(r) for r in env.roots]

    with pytest.raises(LiftRefusedError):
        _lift(service, env, seq, principal=who)

    assert service.check(VARIANT, root_id=None).blocked  # type: ignore[attr-defined]
    assert env.table_rows() == table
    assert [env.journal(r) for r in env.roots] == journals
    assert _audit(env, "tombstone.lifted") == []


@pytest.mark.parametrize("typed", ["", "lift", "lift 999", "LIFT 1 ", "yes"])
def test_tombstone_lift_needs_the_typed_phrase(tmp_path: Path, typed: str) -> None:
    env, service, seq = _laid(tmp_path)
    assert typed != lift_phrase(seq)

    with pytest.raises(LiftConfirmationError):
        _lift(service, env, seq, confirmation=typed)

    assert service.check(VARIANT, root_id=None).blocked  # type: ignore[attr-defined]
    assert _audit(env, "tombstone.lifted") == []


def test_lift_of_an_unknown_or_already_lifted_sequence_is_refused(tmp_path: Path) -> None:
    env, service, seq = _laid(tmp_path)

    with pytest.raises(UnknownTombstoneError):
        _lift(service, env, 99, confirmation=lift_phrase(99))
    _lift(service, env, seq)
    with pytest.raises(UnknownTombstoneError):  # already lifted: nothing left to lift
        _lift(service, env, seq)
    with pytest.raises(UnknownTombstoneError):  # the lift line itself is not a tombstone
        _lift(service, env, seq + 1, confirmation=lift_phrase(seq + 1))
    assert len(_audit(env, "tombstone.lifted")) == 1


def _counts(env: TombEnv) -> dict[str, int]:
    """Every table but the ones a lift is meant to write: the tombstones, the audit, the events."""
    own = {"audit", "event", "instance", "sqlite_sequence", "content_tombstone", "tombstone_kid"}
    with env.engine.connect() as connection:
        tables = connection.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type = 'table'")
        ).scalars()
        return {
            t: connection.execute(sa.text(f'SELECT count(*) FROM "{t}"')).scalar()  # noqa: S608
            for t in tables
            if t not in own and not t.startswith(("tombstone_", "sqlite_"))
        }


def test_a_lift_restores_nothing_by_itself(tmp_path: Path) -> None:
    """The lift opens the door; it brings back no Unit, no file, no request, no job."""
    env, service, seq = _laid(tmp_path)
    before = _counts(env)
    tree = {p for p in env.tmp.rglob("*") if p.is_file()}

    _lift(service, env, seq)

    assert _counts(env) == before
    new_files = {p.name for p in env.tmp.rglob("*") if p.is_file()} - {p.name for p in tree}
    assert new_files <= {"tombstones", "tombstones.chain"}, new_files  # the journals only


def test_a_lift_survives_a_replay_and_a_restore_of_an_older_database(tmp_path: Path) -> None:
    """The lift is a line of the journals: a replay, or a database from before the lift, finds it
    and the tombstone stays lifted (the journals are the survivors of a restore, EF-BIB-053)."""
    env, service, seq = _laid(tmp_path)
    _lift(service, env, seq)

    again = env.service()  # a restart
    again.replay(env.roots)
    assert not again.check(VARIANT, root_id=env.library.root_id).blocked

    with env.engine.begin() as connection:  # the table of before the lift and the tombstone
        connection.execute(sa.delete(coherence.content_tombstone))
    restored = env.service()
    restored.replay(env.roots)
    assert not restored.check(VARIANT, root_id=env.library.root_id).blocked
    assert len(env.table_rows()) == 2  # the pair came back into the table: the tombstone, the lift


def test_a_new_purge_after_a_lift_blocks_again(tmp_path: Path) -> None:
    env, service, seq = _laid(tmp_path)
    _lift(service, env, seq)
    assert not service.check(VARIANT, root_id=None).blocked  # type: ignore[attr-defined]

    service.record(VARIANT, env.roots)  # type: ignore[attr-defined]

    assert service.check(VARIANT, root_id=None).blocked  # type: ignore[attr-defined]
    fresh = env.service()
    fresh.replay(env.roots)
    assert fresh.check(VARIANT, root_id=None).blocked


def test_a_lift_touches_one_tombstone_only(tmp_path: Path) -> None:
    env, service, seq = _laid(tmp_path)
    other = service.record(PLACE, env.roots)  # type: ignore[attr-defined]
    assert isinstance(PLACE, PlaceRef) and isinstance(VARIANT, VariantRef)

    _lift(service, env, seq)

    assert not service.check(VARIANT, root_id=None).blocked  # type: ignore[attr-defined]
    assert service.check(PLACE, root_id=None).blocked  # type: ignore[attr-defined]
    assert other.seq != seq


def test_a_lift_is_owed_to_a_root_that_is_away_and_written_when_it_returns(
    tmp_path: Path,
) -> None:
    env, service, seq = _laid(tmp_path)
    away = env.unmounted(env.books)

    _lift(service, env, seq, roots=[env.library, away])
    # the lift of the root that was away is a line owed: written first when it comes back
    assert env.books.root_id in {r["root_id"] for r in _pending(env)}
    service.flush_pending(env.books)

    fresh = env.service()
    fresh.replay([env.books])  # that root alone: its journal holds the tombstone and its lift
    assert not fresh.check(VARIANT, root_id=env.books.root_id).blocked


def _pending(env: TombEnv) -> list[dict[str, object]]:
    with env.engine.connect() as connection:
        rows = connection.execute(sa.select(coherence.tombstone_pending_root)).mappings()
        return [dict(r) for r in rows]


def test_a_lift_invalidates_the_since_tokens_like_the_other_purges(tmp_path: Path) -> None:
    env, service, seq = _laid(tmp_path)
    with env.engine.connect() as connection:
        before = connection.execute(sa.select(platform.instance.c.events_purged_through)).scalar()

    _lift(service, env, seq)

    with env.engine.connect() as connection:
        after = connection.execute(sa.select(platform.instance.c.events_purged_through)).scalar()
    assert (after or 0) > (before or 0)
