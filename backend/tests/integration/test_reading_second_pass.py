"""Reading progress, after the second adverse review of #284: the speed rule, the server door,
the hashes taken in one pass, the profile export and the purge."""

import ast
import hashlib
import io
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.schema import reading
from shelfhold.reading import public
from shelfhold.reading.application.member_keys import MemberKeyRevokedError
from shelfhold.reading.application.server_state import record_server_state
from shelfhold.reading.domain.document_hash import (
    kavita_hash,
    koreader_partial_md5,
    verified_hashes,
)
from shelfhold.reading.public import record_position, undo_read_mark
from tests.reading_env import NEW_HASH, ReadingEnv, hours, reading_env

pytestmark = pytest.mark.req("EF-LEC-014")

BOUNDS = [(1, 10), (11, 20), (21, 30)]


def states(env: ReadingEnv, user: str, units: list[str]) -> list[str]:
    got = env.progress(user)
    return [got[u].state if u in got else "-" for u in units]


def report(
    env: ReadingEnv, reader: str, volume: str, page: int, when: datetime, **kw: float
) -> str:
    with env.engine.begin() as connection:
        return record_position(
            connection, reader_id=reader, subject_id=reader, carrier_type="export_item",
            carrier_id=volume, content_hash=NEW_HASH, locator={"page": page}, percent=None,
            client_ts=when, now=when, device_id="reader-1", **kw,
        )  # fmt: skip


def volume_of(env: ReadingEnv) -> tuple[list[str], str]:
    units = env.units("ok", 3)
    return units, env.file("ok", units, bounds=BOUNDS, content_hash=NEW_HASH)


# --- the speed rule -------------------------------------------------------------------------


def test_a_book_closed_a_chapter_apart_at_a_human_speed_is_read(tmp_path: Path) -> None:
    """KOReader pushes at close, not at each page: three sittings of 10 pages in 5 minutes each."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units, volume = volume_of(env)
        start = hours(1)
        report(env, reader, volume, 1, start)
        report(env, reader, volume, 11, start + timedelta(minutes=5))
        report(env, reader, volume, 21, start + timedelta(minutes=10))
        report(env, reader, volume, 30, start + timedelta(minutes=15))
        assert states(env, reader, units) == ["read", "read", "read"]


def test_the_same_jumps_in_a_few_seconds_are_a_look_not_a_reading(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units, volume = volume_of(env)
        for page, seconds in [(1, 0), (11, 2), (21, 4), (30, 6)]:
            report(env, reader, volume, page, hours(1) + timedelta(seconds=seconds))
        assert "read" not in states(env, reader, units)


def test_the_limit_is_an_argument_of_the_service(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units, volume = volume_of(env)
        start = hours(1)
        report(env, reader, volume, 1, start, max_pages_per_minute=5)
        report(env, reader, volume, 30, start + timedelta(minutes=3), max_pages_per_minute=5)
        assert "read" not in states(env, reader, units)  # 9.7 pages a minute > 5


def test_the_client_date_never_buys_a_traversal(tmp_path: Path) -> None:
    """The device says it took an hour; the server saw both positions in the same second."""
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units, volume = volume_of(env)
        now = hours(1)
        with env.engine.begin() as connection:
            for page, ts in [(1, now - timedelta(hours=9)), (30, now)]:
                record_position(
                    connection, reader_id=reader, subject_id=reader, carrier_type="export_item",
                    carrier_id=volume, content_hash=NEW_HASH, locator={"page": page},
                    percent=None, client_ts=ts, now=now, device_id="reader-1",
                )  # fmt: skip
        assert "read" not in states(env, reader, units)


def test_with_no_anchor_a_first_report_reaches_nothing(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units, volume = volume_of(env)
        report(env, reader, volume, 30, hours(1))
        assert "read" not in states(env, reader, units)


# --- the server door ------------------------------------------------------------------------


def pulled(env: ReadingEnv, reader: str, volume: str, destination: str) -> str:
    with env.engine.begin() as connection:
        return record_server_state(
            connection, user_id=reader, shelf_destination_id=destination,
            carrier_type="export_item", carrier_id=volume, content_hash=NEW_HASH,
            locator={"page": 30}, percent=1.0, client_ts=hours(1),
        )  # fmt: skip


def test_a_state_pulled_for_a_linked_live_account_is_trusted(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units, volume = volume_of(env)
        destination = env.destination()
        env.account(reader, destination)
        pulled(env, reader, volume, destination)
        assert states(env, reader, units) == ["read", "read", "read"]


def test_a_state_for_a_member_with_no_account_is_refused(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader, other = env.user("reader"), env.user("other")
        _units, volume = volume_of(env)
        destination = env.destination()
        env.account(other, destination)
        with pytest.raises(MemberKeyRevokedError):
            pulled(env, reader, volume, destination)
        assert env.progress(reader) == {}


def test_a_state_for_a_revoked_account_is_refused(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        _units, volume = volume_of(env)
        destination = env.destination()
        account = env.account(reader, destination)
        with env.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE destination_account SET revoked_at = :t WHERE id = :i"),
                {"t": hours(2), "i": account},
            )
        with pytest.raises(MemberKeyRevokedError):
            pulled(env, reader, volume, destination)


def test_a_server_state_is_not_a_public_door() -> None:
    assert not hasattr(public, "record_server_state")


def test_no_interface_imports_the_server_state_door() -> None:
    """Closed: no route, CLI nor device dependency may reach it (see also the import contract)."""
    root = Path(__file__).resolve().parents[2] / "src" / "shelfhold" / "interfaces"
    offenders = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or "", *(f"{node.module}.{a.name}" for a in node.names)]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any("server_state" in n for n in names):
                offenders.append(str(path.relative_to(root)))
    assert offenders == []


def test_the_state_a_server_holds_can_be_undone_and_stays_undone(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units, volume = volume_of(env)
        destination = env.destination()
        env.account(reader, destination)
        pulled(env, reader, volume, destination)
        with env.engine.begin() as connection:
            undo_read_mark(
                connection, viewer_id=reader, subject_id=reader, unit_id=units[0], now=hours(3)
            )
        assert states(env, reader, units) == ["in_progress", "read", "read"]


def test_the_default_position_of_the_test_env_is_a_device_report(tmp_path: Path) -> None:
    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units, volume = volume_of(env)
        env.position(reader, volume, page=30, percent=1.0)
        assert "read" not in states(env, reader, units)
        env.position(reader, volume, page=30, percent=1.0, source="server")
        assert states(env, reader, units) == ["read", "read", "read"]


# --- the hashes, in one pass ----------------------------------------------------------------


@pytest.mark.parametrize("size", [0, 1, 4096, 1024 * 1024 - 1, 3 * 1024 * 1024 + 17])
def test_one_pass_gives_the_three_hashes_of_the_separate_functions(
    tmp_path: Path, size: int
) -> None:
    data = bytes((i * 31) % 251 for i in range(size))
    file = tmp_path / "volume.cbz"
    file.write_bytes(data)
    sha, koreader, kavita = verified_hashes(io.BytesIO(data))
    assert sha == hashlib.sha256(data).hexdigest()
    assert koreader == koreader_partial_md5(file)
    assert kavita == kavita_hash(file).lower()


# --- the profile export and the purge ---------------------------------------------------------


def test_the_export_of_a_profile_holds_the_source_and_the_marks(tmp_path: Path) -> None:
    from shelfhold.identity.adapters.store import SqlIdentityStore

    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        _units, volume = volume_of(env)
        env.position(reader, volume, page=30, percent=1.0, source="server")
        exported = SqlIdentityStore(env.engine).export_rows(reader, limit=1000)
        assert exported is not None
        row = dict(exported["reading_positions"][0])
        assert row["source"] == "server"
        assert "read_units" in row and "undone_units" in row


def test_positions_of_other_files_forget_the_units_of_a_purged_work(tmp_path: Path) -> None:
    from shelfhold.export.application.purge import PurgeService

    with reading_env(tmp_path) as env:
        reader = env.user("reader")
        units, _volume = volume_of(env)
        other = env.file("ok", [units[0]], bounds=[(1, 10)])
        row = env.position(reader, other, page=3, read_units=[units[0]])
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(reading.reading_position)
                .where(reading.reading_position.c.id == row)
                .values(undone_units={units[0]: "2026-11-01T00:00:00+00:00"})
            )
            plan = type("Plan", (), {"unit_ids": (units[0],)})()
            PurgeService._scrub_positions(connection, plan)
            got = connection.execute(
                sa.select(
                    reading.reading_position.c.covered_units,
                    reading.reading_position.c.read_units,
                    reading.reading_position.c.undone_units,
                ).where(reading.reading_position.c.id == row)
            ).one()
        assert units[0] not in (got.covered_units or [])
        assert got.read_units == [] and got.undone_units == {}
