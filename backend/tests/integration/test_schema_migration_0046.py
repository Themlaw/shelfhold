"""Migration 0046 (M3 L01): the M3 schema, tables and columns, and every closed vocabulary.

0046 chains on the real head of main, 0040 (0041 to 0045 stay free). The DDL parity of the whole
database is `test_migrated_ddl_equals_create_all` (0002 module): it runs at head and so covers
this revision. The tests below attack each guard: a value outside a vocabulary, a child without
an age ceiling and a duplicate are all refused by the database itself.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.migrate import default_script_location, migrate
from shelfhold.infrastructure.schema import metadata
from tests.integration.test_schema_catalog import add_edition, add_unit, add_work
from tests.integration.test_schema_migration_0002 import alembic_config, tables

ARGON_STUB = "$argon2id$stub"  # a stored hash, never checked here
HASH = "sha256:" + "ab" * 32
NOW = "2026-11-12 10:00:00"
M3_TABLES = {
    "profile_source_policy", "user_state", "request", "pending_source_item", "merge_log",
    "work_redirect", "thumbnail", "time_window", "notification_digest", "source_alias_migration",
}  # fmt: skip


def _previous() -> str:
    down = ScriptDirectory(str(default_script_location())).get_revision("0046").down_revision
    assert isinstance(down, str)
    return down


@pytest.fixture
def db(settings: Settings) -> Iterator[sqlite3.Connection]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings)
    connection = sqlite3.connect(settings.database_path, isolation_level=None)  # autocommit
    connection.execute("PRAGMA foreign_keys = ON")
    yield connection
    connection.close()


def add_user(db: sqlite3.Connection, role: str = "member", **extra: Any) -> str:
    values: dict[str, Any] = {
        "id": new_ulid(),
        "username": f"u{new_ulid()[-8:]}",
        "role": role,
        "can_rate": 1 if role == "admin" else 0,
        **extra,
    }
    if role == "child" and "max_age_rating" not in extra:
        values["max_age_rating"] = 10
    columns = ", ".join(f'"{c}"' for c in values)
    marks = ", ".join("?" for _ in values)
    sql = f'INSERT INTO "user" ({columns}) VALUES ({marks})'  # noqa: S608 (test helper, fixed names)
    db.execute(sql, list(values.values()))
    return str(values["id"])


def add_row(db: sqlite3.Connection, table: str, **values: Any) -> None:
    columns = ", ".join(f'"{c}"' for c in values)
    marks = ", ".join("?" for _ in values)
    sql = f'INSERT INTO "{table}" ({columns}) VALUES ({marks})'  # noqa: S608 (test helper)
    db.execute(sql, list(values.values()))


def refused(db: sqlite3.Connection, table: str, **values: Any) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        add_row(db, table, **values)
    db.rollback()


def work(db: sqlite3.Connection) -> str:
    kind = "manga"
    db.execute("INSERT OR IGNORE INTO work_kind (key, label_fr) VALUES (?, 'Manga')", (kind,))
    work_id = new_ulid()
    db.execute(
        "INSERT INTO work (id, kind, title, sort_title) VALUES (?, ?, 't', 't')", (work_id, kind)
    )
    return work_id


@pytest.mark.req("ENF-OPS-002")
def test_0046_chains_on_the_real_head_0040_and_is_the_only_head() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0046"]
    assert script.get_revision("0046").down_revision == "0040"


@pytest.mark.req("ENF-OPS-002", "EF-USR-001")
def test_metadata_covers_the_m3_tables(db: sqlite3.Connection, settings: Settings) -> None:
    assert tables(settings.database_path) >= M3_TABLES
    assert {t.name for t in metadata.sorted_tables} >= M3_TABLES


@pytest.mark.req("ENF-OPS-002", "EF-USR-001", "EF-BIB-021", "EF-BIB-028")
def test_upgrade_from_0040_keeps_data(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), _previous())
    engine = create_db_engine(database)
    admin, member, child = new_ulid(), new_ulid(), new_ulid()
    with engine.begin() as connection:
        for user_id, name, role in ((admin, "root", "admin"), (member, "ma", "member"),
                                    (child, "lea", "child")):  # fmt: skip
            connection.execute(
                sa.text(
                    'INSERT INTO "user" (id, username, role, can_rate) VALUES (:i, :n, :r, :c)'
                ),
                {"i": user_id, "n": name, "r": role, "c": 1 if role == "admin" else 0},
            )
        work_id = add_work(connection)
        edition_id = add_edition(connection, work_id)
        add_unit(connection, edition_id, "1")
        connection.execute(
            sa.text("INSERT INTO team (id, edition_id, label) VALUES (:i, :e, 'equipe')"),
            {"i": new_ulid(), "e": edition_id},
        )
        connection.execute(
            sa.text(
                "INSERT INTO trash_item (id, kind, ref, path, deleted_at, purge_after) "
                "VALUES (:i, 'unit', :r, 'a/b', :d, :p)"
            ),
            {"i": new_ulid(), "r": new_ulid(), "d": NOW, "p": "2026-12-12 10:00:00"},
        )
        connection.execute(sa.text("INSERT INTO source_config (plugin_id) VALUES ('gutenberg')"))
    engine.dispose()
    assert "0046" in migrate(settings).applied
    with closing(sqlite3.connect(database)) as conn:
        roles = dict(conn.execute('SELECT username, role FROM "user"'))
        assert roles == {"root": "admin", "ma": "member", "lea": "child"}
        ceilings = dict(conn.execute('SELECT username, max_age_rating FROM "user"'))
        assert ceilings == {"root": None, "ma": None, "lea": 0}  # a migrated child: the strictest
        user_row = conn.execute(
            "SELECT notify_mode, hide_unrated_remote, subject_filter, kavita_username, "
            "quiet_start FROM \"user\" WHERE username = 'ma'"
        ).fetchone()
        assert user_row == ("digest", 1, "[]", None, None)
        assert conn.execute("SELECT level FROM trash_item").fetchall() == [("delete_content",)]
        assert conn.execute("SELECT number_offset FROM team").fetchall() == [(0,)]
        assert conn.execute(
            "SELECT integrity_state, integrity_checked_at FROM unit"
        ).fetchall() == [("unchecked", None)]
        assert conn.execute("SELECT family_safe FROM source_config").fetchall() == [(0,)]
        assert conn.execute(
            "SELECT maintenance_since, maintenance_by FROM instance"
        ).fetchall() in ([], [(None, None)])


@pytest.mark.req("ENF-OPS-002")
def test_downgrade_to_0040_drops_the_m3_schema(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings)
    command.downgrade(alembic_config(database), "0040")
    assert not (M3_TABLES & tables(database))
    with closing(sqlite3.connect(database)) as conn:
        cols = {r[1] for r in conn.execute('PRAGMA table_info("user")')}
    assert "max_age_rating" not in cols


@pytest.mark.req("EF-USR-001")
def test_user_role_check_admin_member_child(db: sqlite3.Connection) -> None:
    for role in ("admin", "member", "child"):
        add_user(db, role)
    for bad in ("owner", "Admin", "", "guest"):
        refused(db, "user", id=new_ulid(), username=f"x{bad}", role=bad, can_rate=1,
                max_age_rating=10)  # fmt: skip


@pytest.mark.req("EF-USR-001", "EF-USR-015")
def test_a_child_never_has_a_null_age_ceiling(db: sqlite3.Connection) -> None:
    refused(db, "user", id=new_ulid(), username="kid", role="child", can_rate=0)
    refused(db, "user", id=new_ulid(), username="kid", role="child", can_rate=0,
            max_age_rating=None)  # fmt: skip
    child = add_user(db, "child", max_age_rating=10)
    with pytest.raises(sqlite3.IntegrityError):  # clearing the ceiling is not "all ages"
        db.execute('UPDATE "user" SET max_age_rating = NULL WHERE id = ?', (child,))
    db.rollback()
    adult = add_user(db, "member")  # promoting an adult without a ceiling is refused too
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE \"user\" SET role = 'child' WHERE id = ?", (adult,))
    db.rollback()
    db.execute("UPDATE \"user\" SET role = 'child', max_age_rating = 0 WHERE id = ?", (adult,))
    assert add_user(db, "admin")  # an adult may have none
    for bad in (-1, 100, "ten", 10.5):
        refused(db, "user", id=new_ulid(), username=f"k{bad}", role="member", can_rate=0,
                max_age_rating=bad)  # fmt: skip


@pytest.mark.req("EF-USR-005", "EF-USR-017", "EF-USR-018", "EF-INT-021")
def test_user_m3_columns_are_checked(db: sqlite3.Connection) -> None:
    base = {"role": "member", "can_rate": 0}
    refused(db, "user", id=new_ulid(), username="a", notify_mode="always", **base)
    refused(db, "user", id=new_ulid(), username="b", quiet_start="22:00", **base)  # no end
    refused(db, "user", id=new_ulid(), username="c", quiet_end="07:00", **base)  # no start
    for bad in ("25:00", "7:00", "22h00", "24:00", "22:60"):
        refused(db, "user", id=new_ulid(), username="d", quiet_start=bad, quiet_end="07:00", **base)
    refused(db, "user", id=new_ulid(), username="e", subject_filter='{"a": 1}', **base)
    refused(db, "user", id=new_ulid(), username="f", subject_filter="not json", **base)
    refused(db, "user", id=new_ulid(), username="g", kavita_username="", **base)
    add_user(db, quiet_start="22:00", quiet_end="07:00", notify_mode="immediate",
             kavita_username="lea", subject_filter='["Erotica"]')  # fmt: skip
    refused(db, "user", id=new_ulid(), username="i", kavita_username="lea", **base)  # unique


@pytest.mark.req("EF-USR-014", "EF-USR-018")
def test_a_new_source_is_not_family_and_a_new_profile_hides_unrated(
    db: sqlite3.Connection,
) -> None:
    db.execute("INSERT INTO source_config (plugin_id) VALUES ('suwayomi')")
    assert db.execute("SELECT family_safe FROM source_config").fetchone() == (0,)
    user = add_user(db, "child")
    row = db.execute(
        'SELECT hide_unrated_remote, notify_mode FROM "user" WHERE id = ?', (user,)
    ).fetchone()
    assert row == (1, "digest")


@pytest.mark.req("EF-USR-005")
def test_profile_source_policy_check(db: sqlite3.Connection) -> None:
    user = add_user(db, "child")
    for policy in ("add_directly", "request", "forbidden"):
        add_row(db, "profile_source_policy", user_id=user, source_key=f"s-{policy}", policy=policy)
    for bad in ("allow", "Request", "", "ask"):
        refused(db, "profile_source_policy", user_id=user, source_key="x", policy=bad)
    refused(db, "profile_source_policy", user_id=user, source_key="s-request", policy="request")
    refused(db, "profile_source_policy", user_id=user, source_key="", policy="request")
    refused(db, "profile_source_policy", user_id=new_ulid(), source_key="y", policy="request")
    db.execute('DELETE FROM "user" WHERE id = ?', (user,))
    assert db.execute("SELECT count(*) FROM profile_source_policy").fetchone() == (0,)


@pytest.mark.req("EF-USR-007")
def test_user_state_is_per_user_and_defaults_to_nothing(db: sqlite3.Connection) -> None:
    one, two, work_id = add_user(db), add_user(db), work(db)
    add_row(db, "user_state", user_id=one, work_id=work_id, followed=1, read_at=NOW)
    add_row(db, "user_state", user_id=two, work_id=work_id)
    assert db.execute(
        "SELECT followed, favorite, read_at FROM user_state WHERE user_id = ?", (two,)
    ).fetchone() == (0, 0, None)
    assert db.execute("SELECT stamp FROM user_state WHERE user_id = ?", (two,)).fetchone()[0]
    refused(db, "user_state", user_id=one, work_id=work_id)  # one row per (user, work)
    refused(db, "user_state", user_id=one, work_id=new_ulid())  # a Work that exists
    db.execute("DELETE FROM work WHERE id = ?", (work_id,))
    assert db.execute("SELECT count(*) FROM user_state").fetchone() == (0,)


@pytest.mark.req("EF-USR-010")
def test_request_target_kind_check(db: sqlite3.Connection) -> None:
    user, work_id = add_user(db, "child"), work(db)
    for kind, value in (("work_id", work_id), ("source_ref", "gutenberg/42"),
                        ("isbn", "9782070360024"), ("free_text", "un livre")):  # fmt: skip
        add_row(db, "request", id=new_ulid(), requester_user_id=user, target_kind=kind,
                target_value=value)  # fmt: skip
    for bad in ("url", "WORK_ID", "", "title"):
        refused(db, "request", id=new_ulid(), requester_user_id=user, target_kind=bad,
                target_value="x")  # fmt: skip
    refused(db, "request", id=new_ulid(), requester_user_id=user, target_kind="free_text",
            target_value="")  # fmt: skip
    refused(db, "request", id=new_ulid(), requester_user_id=user, target_kind="work_id",
            target_value="not-a-ulid")  # fmt: skip
    assert db.execute("SELECT DISTINCT state FROM request").fetchall() == [("pending",)]


@pytest.mark.req("EF-USR-010")
def test_request_state_has_six_values_and_a_reason_only_on_refusal(db: sqlite3.Connection) -> None:
    user = add_user(db, "child")
    common = {"requester_user_id": user, "target_kind": "free_text", "target_value": "x"}
    states = ("pending", "approved", "rejected", "not_found", "cancelled", "arrived")
    for state in states:
        add_row(db, "request", id=new_ulid(), state=state, **common)
    for bad in ("done", "refused", "Pending", ""):
        refused(db, "request", id=new_ulid(), state=bad, **common)
    refused(db, "request", id=new_ulid(), state="approved", reject_reason="no", **common)
    add_row(db, "request", id=new_ulid(), state="rejected", reject_reason="trop long", **common)
    refused(db, "request", id=new_ulid(), requester_user_id=new_ulid(), target_kind="free_text",
            target_value="x")  # fmt: skip


@pytest.mark.req("EF-USR-010")
def test_pending_source_item_is_unique_per_source_and_series(db: sqlite3.Connection) -> None:
    add_row(db, "pending_source_item", id=new_ulid(), source_key="gutenberg", series_ref="1",
            title="Titre", fetched_at=NOW)  # fmt: skip
    refused(db, "pending_source_item", id=new_ulid(), source_key="gutenberg", series_ref="1",
            title="Autre", fetched_at=NOW)  # fmt: skip
    refused(db, "pending_source_item", id=new_ulid(), source_key="g", series_ref="",
            title="T", fetched_at=NOW)  # fmt: skip


@pytest.mark.req("EF-BIB-019")
def test_merge_log_and_work_redirect_checks(db: sqlite3.Connection) -> None:
    survivor, absorbed = work(db), new_ulid()
    log = new_ulid()
    add_row(db, "merge_log", id=log, kind="merge", survivor_id=survivor, absorbed_id=absorbed,
            snapshot='{"a": 1}')  # fmt: skip
    refused(db, "merge_log", id=new_ulid(), kind="join", survivor_id=survivor,
            absorbed_id=absorbed, snapshot="{}")  # fmt: skip
    refused(db, "merge_log", id=new_ulid(), kind="merge", survivor_id=survivor,
            absorbed_id=survivor, snapshot="{}")  # a Work cannot absorb itself  # fmt: skip
    refused(db, "merge_log", id=new_ulid(), kind="merge", survivor_id=survivor,
            absorbed_id=absorbed, snapshot="[1]")  # fmt: skip
    refused(db, "merge_log", id=new_ulid(), kind="merge", survivor_id="x", absorbed_id=absorbed,
            snapshot="{}")  # fmt: skip
    add_row(db, "work_redirect", from_work_id=absorbed, to_work_id=survivor, merge_log_id=log)
    refused(db, "work_redirect", from_work_id=absorbed, to_work_id=survivor)  # one per old id
    refused(db, "work_redirect", from_work_id=survivor, to_work_id=survivor)  # no loop
    refused(db, "work_redirect", from_work_id=new_ulid(), to_work_id=new_ulid())  # a real Work


@pytest.mark.req("EF-BIB-020")
def test_thumbnail_sizes_are_160_320_640_only(db: sqlite3.Connection) -> None:
    for size in (160, 320, 640):
        add_row(db, "thumbnail", content_hash=HASH, size=size, path=f"ab/{size}.webp", bytes=10)
    for bad in (0, 100, 161, 1024, "big"):
        refused(db, "thumbnail", content_hash="sha256:" + "cd" * 32, size=bad, path="a.webp",
                bytes=10)  # fmt: skip
    refused(db, "thumbnail", content_hash=HASH, size=160, path="again.webp", bytes=10)
    refused(db, "thumbnail", content_hash="deadbeef", size=160, path="b.webp", bytes=10)
    refused(db, "thumbnail", content_hash="sha256:" + "ef" * 32, size=160, path="/etc/x", bytes=1)
    refused(db, "thumbnail", content_hash="sha256:" + "ef" * 32, size=160, path="../x", bytes=1)
    refused(db, "thumbnail", content_hash="sha256:" + "ef" * 32, size=160, path="x", bytes=-1)


@pytest.mark.req("EF-BIB-021")
def test_trash_level_check(db: sqlite3.Connection) -> None:
    base = {"kind": "unit", "ref": new_ulid(), "path": "a/b", "deleted_at": NOW,
            "purge_after": "2026-12-12 10:00:00"}  # fmt: skip
    for level in ("unfollow", "remove_destinations", "delete_content"):
        add_row(db, "trash_item", id=new_ulid(), level=level, **base)
    for bad in ("purge", "all", "", "Unfollow"):
        refused(db, "trash_item", id=new_ulid(), level=bad, **base)
    add_row(db, "trash_item", id=new_ulid(), **base)
    assert db.execute("SELECT level FROM trash_item ORDER BY rowid DESC LIMIT 1").fetchone() == (
        "delete_content",
    )


@pytest.mark.req("EF-BIB-012")
def test_unit_integrity_state_check(settings: Settings, db: sqlite3.Connection) -> None:
    engine = create_db_engine(settings.database_path)
    with engine.begin() as connection:
        edition = add_edition(connection, add_work(connection))
        unit = add_unit(connection, edition, "1")
    engine.dispose()
    assert db.execute(
        "SELECT integrity_state, integrity_checked_at FROM unit WHERE id = ?", (unit,)
    ).fetchone() == ("unchecked", None)
    for state in ("ok", "corrupt", "unchecked"):
        db.execute("UPDATE unit SET integrity_state = ? WHERE id = ?", (state, unit))
    for bad in ("broken", "OK", ""):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE unit SET integrity_state = ? WHERE id = ?", (bad, unit))
        db.rollback()


@pytest.mark.req("EF-BIB-028")
def test_team_number_offset_is_laid_down_without_behaviour(
    settings: Settings, db: sqlite3.Connection
) -> None:
    engine = create_db_engine(settings.database_path)
    with engine.begin() as connection:
        edition = add_edition(connection, add_work(connection))
    engine.dispose()
    add_row(db, "team", id=new_ulid(), edition_id=edition, label="t")
    assert db.execute("SELECT number_offset FROM team").fetchone() == (0,)
    refused(db, "team", id=new_ulid(), edition_id=edition, label="u", number_offset=None)
    refused(db, "team", id=new_ulid(), edition_id=edition, label="v", number_offset="x")


@pytest.mark.req("EF-INT-018", "EF-INT-004")
def test_maintenance_and_time_windows(db: sqlite3.Connection) -> None:
    admin = add_user(db, "admin")
    db.execute("INSERT INTO instance (instance_id, jurisdiction) VALUES (?, 'FR')", (new_ulid(),))
    with pytest.raises(sqlite3.IntegrityError):  # an author without a start
        db.execute("UPDATE instance SET maintenance_by = ?", (admin,))
    db.rollback()
    db.execute("UPDATE instance SET maintenance_since = ?, maintenance_by = ?", (NOW, admin))
    db.execute("UPDATE instance SET maintenance_since = NULL, maintenance_by = NULL")
    add_row(db, "time_window", id=new_ulid(), scope="acquisition", start_utc="01:00",
            end_utc="06:00", tz="Europe/Paris")  # fmt: skip
    for start, end in (("1:00", "06:00"), ("01:00", "24:00"), ("ab:cd", "06:00")):
        refused(db, "time_window", id=new_ulid(), scope="export", start_utc=start, end_utc=end,
                tz="UTC")  # fmt: skip
    refused(
        db, "time_window", id=new_ulid(), scope="", start_utc="01:00", end_utc="02:00", tz="UTC"
    )
    refused(db, "time_window", id=new_ulid(), scope="x", start_utc="01:00", end_utc="02:00", tz="")


@pytest.mark.req("EF-INT-021")
def test_notification_digest_is_one_per_user_and_due_time(db: sqlite3.Connection) -> None:
    user = add_user(db)
    add_row(db, "notification_digest", user_id=user, due_at=NOW, payload='{"series": []}')
    refused(db, "notification_digest", user_id=user, due_at=NOW, payload="{}")
    refused(db, "notification_digest", user_id=user, due_at="2026-11-13 08:00:00", payload="[]")
    refused(db, "notification_digest", user_id=new_ulid(), due_at=NOW, payload="{}")
    db.execute('DELETE FROM "user" WHERE id = ?', (user,))
    assert db.execute("SELECT count(*) FROM notification_digest").fetchone() == (0,)


@pytest.mark.req("EF-SRC-022")
def test_source_alias_migration_check(db: sqlite3.Connection) -> None:
    for state in ("previewed", "applied", "cancelled"):
        add_row(db, "source_alias_migration", id=new_ulid(), source_key="s", from_scheme="v1",
                to_scheme="v2", state=state, preview="{}")  # fmt: skip
    for bad in ("running", "done", ""):
        refused(db, "source_alias_migration", id=new_ulid(), source_key="s", from_scheme="v1",
                to_scheme="v2", state=bad, preview="{}")  # fmt: skip
    refused(db, "source_alias_migration", id=new_ulid(), source_key="s", from_scheme="v1",
            to_scheme="v1", state="previewed", preview="{}")  # nothing to migrate  # fmt: skip
    refused(db, "source_alias_migration", id=new_ulid(), source_key="s", from_scheme="v1",
            to_scheme="v2", state="previewed", preview="[]")  # fmt: skip


@pytest.mark.req("EF-USR-001", "ENF-OPS-002")
def test_rebuilt_user_table_still_compares_usernames_without_case(db: sqlite3.Connection) -> None:
    add_user(db, username="Lea")
    refused(db, "user", id=new_ulid(), username="lea", role="member", can_rate=0)


@pytest.mark.req("EF-USR-001", "EF-USR-015")
def test_the_production_store_gives_every_child_a_ceiling(
    settings: Settings, db: sqlite3.Connection
) -> None:
    from datetime import UTC, datetime

    from shelfhold.identity.adapters.store import SqlIdentityStore

    engine = create_db_engine(settings.database_path)
    store = SqlIdentityStore(engine)
    now = datetime(2026, 11, 12, tzinfo=UTC)
    kid, older, adult = new_ulid(), new_ulid(), new_ulid()
    store.insert_user(id=kid, username="kid", role="child", password_hash=ARGON_STUB, now=now)
    store.insert_user(
        id=older,
        username="teen",
        role="child",
        password_hash=ARGON_STUB,
        now=now,
        max_age_rating=13,
    )
    store.insert_user(id=adult, username="adult", role="member", password_hash=ARGON_STUB, now=now)
    with engine.connect() as conn:
        rows = dict(conn.execute(sa.text('SELECT username, max_age_rating FROM "user"')).all())
    assert rows == {"kid": 0, "teen": 13, "adult": None}  # no path leaves a child unbounded
