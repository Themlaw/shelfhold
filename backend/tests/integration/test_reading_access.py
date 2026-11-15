"""M4 L14a: who reads and who writes a reading position (EF-LEC-014, EF-USR-006, ENF-SEC-010).

A profile reads and writes its own progress and nobody else's, admin included; a child sees no
progress on a Work above her ceiling, and a Work she may not see answers like a Work that does not
exist. The service takes the reader from the authenticated principal (a session, a key or a device
token all read as their own profile, `identity.reader_scope`), never from the request, and a
profile that is not there reads and writes nothing.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, identity, reading
from shelfhold.reading.public import (
    MemberKeyRevokedError,
    UnknownCarrierError,
    live_member_account,
    position_of,
    progress_of,
    record_position,
)
from tests.reading_env import NEW_HASH, hours, reading_env

pytestmark = pytest.mark.req("EF-LEC-014", "EF-USR-006")


def put(env, who: str, subject: str, carrier: str, **kw):  # type: ignore[no-untyped-def]
    with env.engine.begin() as connection:
        return record_position(
            connection, reader_id=who, subject_id=subject, carrier_type="export_item",
            carrier_id=carrier, content_hash=NEW_HASH, locator={}, percent=kw.pop("percent", 0.5),
            client_ts=kw.pop("client_ts", hours(1)), **kw,
        )  # fmt: skip


def mine(env, who: str, subject: str | None = None, units=None):  # type: ignore[no-untyped-def]
    with env.engine.connect() as connection:
        return progress_of(connection, viewer_id=who, subject_id=subject or who, unit_ids=units)


def test_a_profile_records_and_reads_its_own_position(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units = env.units("ok", 2)
        item = env.file("ok", units)
        position = put(env, reader, reader, item, percent=1.0)
        assert {p.unit_id: p.state for p in mine(env, reader)} == {u: "in_progress" for u in units}
        with env.engine.connect() as connection:
            assert position_of(connection, viewer_id=reader, position_id=position) is not None


def test_a_future_client_ts_is_stored_bounded_by_the_server_date(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        item = env.file("ok", env.units("ok", 1))
        far = datetime(2099, 1, 1, tzinfo=UTC)
        position = put(env, reader, reader, item, client_ts=far)
        with env.engine.connect() as connection:
            row = connection.execute(
                sa.select(reading.reading_position).where(reading.reading_position.c.id == position)
            ).one()
        assert row.client_ts <= row.server_ts.replace(tzinfo=row.client_ts.tzinfo)


@pytest.mark.parametrize("attacker_role", ["member", "admin", "child"])
def test_nobody_reads_the_progress_of_another_profile(tmp_path: Path, attacker_role: str) -> None:
    """IDOR: the id of another profile in the request gets nothing, whoever asks."""
    with reading_env(tmp_path) as env:
        victim = env.user("victim")
        attacker = env.user("attacker", attacker_role)
        item = env.file("ok", env.units("ok", 2))
        put(env, victim, victim, item, percent=1.0)
        assert mine(env, victim) != []
        assert mine(env, attacker, subject=victim) == []


@pytest.mark.parametrize("attacker_role", ["member", "admin", "child"])
def test_nobody_writes_the_position_of_another_profile(tmp_path: Path, attacker_role: str) -> None:
    with reading_env(tmp_path) as env:
        victim = env.user("victim")
        attacker = env.user("attacker", attacker_role)
        item = env.file("ok", env.units("ok", 1))
        with pytest.raises(UnknownCarrierError):
            put(env, attacker, victim, item)
        assert mine(env, victim) == []
        with env.engine.connect() as connection:
            count = connection.execute(
                sa.select(sa.func.count()).select_from(reading.reading_position)
            ).scalar_one()
        assert count == 0


def test_a_position_of_another_profile_is_not_found_by_its_id(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        victim = env.user("victim")
        other = env.user("other")
        item = env.file("ok", env.units("ok", 1))
        position = put(env, victim, victim, item)
        with env.engine.connect() as connection:
            assert position_of(connection, viewer_id=other, position_id=position) is None
            missing = position_of(connection, viewer_id=other, position_id=new_ulid())
        assert missing is None  # exactly as if it did not exist


def test_a_child_sees_no_progress_above_her_ceiling(tmp_path: Path) -> None:
    """Her own old position on a Work that is now above her ceiling (rated after, ceiling
    lowered): it is gone from every answer, as is any trace of it."""
    with reading_env(tmp_path) as env:
        kid = env.user("kid", "child")
        visible_units = env.units("ok", 1)
        hidden_units = env.units("above", 1)
        visible = env.file("ok", visible_units)
        hidden = env.file("above", hidden_units)
        env.position(kid, visible, percent=1.0)
        hidden_position = env.position(kid, hidden, percent=1.0)  # left by an earlier state
        got = mine(env, kid)
        assert [p.unit_id for p in got] == visible_units
        assert mine(env, kid, units=hidden_units) == []
        with env.engine.connect() as connection:
            assert position_of(connection, viewer_id=kid, position_id=hidden_position) is None


def test_a_child_cannot_record_on_a_work_above_her_ceiling(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        kid = env.user("kid", "child")
        hidden = env.file("above", env.units("above", 1))
        unknown = new_ulid()
        with pytest.raises(UnknownCarrierError) as above:
            put(env, kid, kid, hidden)
        with pytest.raises(UnknownCarrierError) as missing:
            put(env, kid, kid, unknown)
        assert str(above.value) == str(missing.value)  # no way to tell the two apart
        assert mine(env, kid) == []


def test_an_admin_reads_her_own_progress_on_any_work(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        adult = env.user("adult", "admin")
        units = env.units("above", 1)
        item = env.file("above", units)
        put(env, adult, adult, item, percent=1.0)
        assert [p.unit_id for p in mine(env, adult)] == units


@pytest.mark.parametrize("who", ["", "not-a-ulid", "01HZZZZZZZZZZZZZZZZZZZZZZM"])
def test_a_profile_that_is_not_there_reads_and_writes_nothing(tmp_path: Path, who: str) -> None:
    """A key or a device token whose profile was erased: the principal has no profile."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        item = env.file("ok", env.units("ok", 1))
        put(env, reader, reader, item, percent=1.0)
        assert mine(env, who) == []
        with pytest.raises(UnknownCarrierError):
            put(env, who, who, item)


def test_a_key_of_a_child_profile_reads_with_her_ceiling(tmp_path: Path) -> None:
    """A key or a device token reads as the profile it was issued for, never as whoever issued it:
    the scope is read from the database at each call, so lowering a ceiling closes the door."""
    with reading_env(tmp_path) as env:
        kid = env.user("kid", "child", max_age_rating=10)
        units = env.units("twelve", 1)
        item = env.file("twelve", units)
        env.position(kid, item, percent=1.0)
        assert mine(env, kid) == []  # 12 > 10
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(identity.user).where(identity.user.c.id == kid).values(max_age_rating=13)
            )
        assert [p.unit_id for p in mine(env, kid)] == units  # the ceiling raised: seen at once


def test_the_progress_of_a_profile_goes_with_the_profile(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        item = env.file("ok", env.units("ok", 1))
        put(env, reader, reader, item)
        with env.engine.begin() as connection:
            connection.execute(sa.delete(identity.user).where(identity.user.c.id == reader))
        with env.engine.connect() as connection:
            assert (
                connection.execute(sa.text("SELECT count(*) FROM reading_progress")).scalar() == 0
            )


def test_a_member_key_revoked_between_two_calls_is_refused_at_the_second(tmp_path: Path) -> None:
    """L13, `revoked_at` re-read before each call that uses a member's key (EF-USR-012)."""
    with reading_env(tmp_path) as env:
        member = env.user("member")
        destination = env.destination()
        account = env.account(member, destination)
        with env.engine.connect() as connection:
            assert (
                live_member_account(connection, user_id=member, shelf_destination_id=destination)
                == account
            )
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(identity.destination_account)
                .where(identity.destination_account.c.id == account)
                .values(
                    revoked_at=hours(3),
                    secret_kid=None,
                    secret_v=None,
                    secret_nonce=None,
                    secret_ct=None,
                    token_fingerprint=None,
                )
            )
        with env.engine.connect() as connection, pytest.raises(MemberKeyRevokedError):
            live_member_account(connection, user_id=member, shelf_destination_id=destination)


def test_a_member_key_is_not_another_members(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        one, two = env.user("one"), env.user("two")
        destination = env.destination()
        env.account(one, destination)
        with env.engine.connect() as connection, pytest.raises(MemberKeyRevokedError):
            live_member_account(connection, user_id=two, shelf_destination_id=destination)


def test_coverage_table_is_not_a_door(tmp_path: Path) -> None:
    """Sanity: the progress of a carrier that covers nothing is empty, not an error."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        item = new_ulid()
        with env.engine.begin() as connection:
            connection.execute(
                sa.insert(export.export_item).values(
                    id=item, edition_id=env.seed.editions["ok"], kind="volume"
                )
            )
        put(env, reader, reader, item)
        assert mine(env, reader) == []
