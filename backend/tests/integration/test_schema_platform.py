"""M1a schema, platform / identity / coherence (migration 0002): constraints that bite."""
# ruff: noqa: S106

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine, exc

from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import coherence, identity, metadata, platform

SHA = "sha256:" + "a" * 64
ARGON = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_db_engine(tmp_path / "schema.db")
    metadata.create_all(engine)
    yield engine
    engine.dispose()


def add_user(connection: sa.Connection, *, role: str = "admin", can_rate: bool = True) -> str:
    user_id = new_ulid()
    connection.execute(
        sa.insert(identity.user).values(
            id=user_id, username=f"u-{user_id}", password_hash=ARGON, role=role, can_rate=can_rate
        )
    )
    return user_id


def rejected(connection: sa.Connection, statement: Any, expected: str) -> None:
    """The statement fails, and for the reason named: a constraint, or a message fragment."""
    with pytest.raises((exc.IntegrityError, exc.StatementError)) as raised:
        connection.execute(statement)
    assert expected in str(raised.value.orig if raised.value.orig else raised.value), raised.value


API_ID_TABLES = [
    (identity.user, "id"),
    (identity.session, "id"),
    (identity.api_key, "id"),
    (identity.audit, "id"),
    (platform.job_view, "id"),
    (platform.schedule, "id"),
    (platform.instance, "instance_id"),
    (platform.physical_root, "root_id"),
    (coherence.fs_intent, "id"),
    (coherence.export_plan, "plan_id"),
    (coherence.export_op, "id"),
    (coherence.content_tombstone, "id"),
]


@pytest.mark.req("ENF-OPS-020")
@pytest.mark.parametrize(("table", "column"), API_ID_TABLES, ids=lambda v: getattr(v, "name", v))
def test_api_ids_are_opaque_ulids(engine: Engine, table: sa.Table, column: str) -> None:
    assert isinstance(table.c[column].type, sa.String)
    assert table.c[column].type.length == 26
    assert table.c[column].primary_key
    sql = {str(c.sqltext) for c in table.constraints if isinstance(c, sa.CheckConstraint)}
    assert any(column in text and "GLOB" in text for text in sql), sql


@pytest.mark.req("ENF-OPS-020")
def test_a_non_ulid_identifier_is_refused_by_the_database(engine: Engine) -> None:
    with engine.begin() as connection:
        for bad in ("1", "not-a-ulid", new_ulid().lower(), new_ulid()[:-1] + "U", new_ulid() + "0"):
            rejected(
                connection,
                sa.insert(identity.user).values(
                    id=bad, username=f"x{bad}", password_hash=ARGON, role="admin", can_rate=True
                ),
                "ck_user_id_ulid",
            )


@pytest.mark.req("ENF-OPS-020")
def test_event_stream_ids_are_monotonic_and_never_reused(engine: Engine) -> None:
    def emit(connection: sa.Connection) -> int:
        result = connection.execute(
            sa.insert(platform.event).values(
                type="unit.added",
                schema_version=1,
                payload={},
                occurred_at=NOW,
                visibility="household",
            )
        )
        return int(result.inserted_primary_key[0])

    with engine.begin() as connection:
        first, second = emit(connection), emit(connection)
        connection.execute(sa.delete(platform.event))
        third = emit(connection)
    assert first < second < third  # AUTOINCREMENT: SSE ids are not recycled after a purge


@pytest.mark.req("ENF-SEC-008", "EF-USR-003")
def test_hash_fields_prefixed_with_algorithm(engine: Engine) -> None:
    with engine.begin() as connection:
        user_id = add_user(connection)
        base: dict[str, Any] = {
            "user_id": user_id,
            "prefix": "shk_abcd",
            "scopes": ["admin"],
            "read_only": False,
            "created_at": NOW,
        }
        for bad in ("a" * 64, "md5:" + "a" * 32, "sha256:short", "sha256:" + "A" * 64):
            rejected(
                connection,
                sa.insert(identity.api_key).values(id=new_ulid(), hash=bad, **base),
                "ck_api_key_hash_sha256_prefixed",
            )
        connection.execute(
            sa.insert(identity.api_key).values(id=new_ulid(), hash=SHA, **base),
        )
        rejected(
            connection,
            sa.insert(identity.user).values(
                id=new_ulid(),
                username="plain",
                password_hash="hunter2",
                role="admin",
                can_rate=True,
            ),
            "ck_user_password_hash_argon2id",
        )
        rejected(
            connection,
            sa.insert(identity.session).values(
                id=new_ulid(),
                user_id=user_id,
                token_hash="b" * 64,
                csrf_secret="x",
                last_seen_at=NOW,
                auth_time=NOW,
                absolute_expiry=NOW,
            ),
            "ck_session_token_hash_sha256_prefixed",
        )
        rejected(
            connection,
            sa.insert(coherence.fs_intent).values(
                id=new_ulid(),
                op="rename",
                intent_schema_version="1.0",
                expected_sha256="b" * 64,
                state="pending",
                epoch=0,
            ),
            "ck_fs_intent_expected_sha256_sha256_prefixed",
        )


@pytest.mark.req("EF-USR-021", "EF-USR-003")
def test_user_roles_and_rating_right(engine: Engine) -> None:
    with engine.begin() as connection:
        add_user(connection, role="member", can_rate=False)
        add_user(connection, role="child", can_rate=False)
        rejected(
            connection,
            sa.insert(identity.user).values(
                id=new_ulid(), username="r", password_hash=ARGON, role="root", can_rate=False
            ),
            "ck_user_role_values",
        )
        rejected(
            connection,
            sa.insert(identity.user).values(
                id=new_ulid(), username="adm", password_hash=ARGON, role="admin", can_rate=False
            ),
            "ck_user_admin_can_rate",
        )
        rejected(
            connection,
            sa.insert(identity.user).values(
                id=new_ulid(),
                username="t",
                password_hash=ARGON,
                role="member",
                can_rate=False,
                theme="neon",
            ),
            "ck_user_theme_values",
        )


@pytest.mark.req("EF-USR-022")
def test_session_carries_auth_time_expiry_and_remember_flag(engine: Engine) -> None:
    columns = set(identity.session.c.keys())
    assert {
        "auth_time",
        "last_seen_at",
        "absolute_expiry",
        "remember",
        "csrf_secret",
        "revoked_at",
    } <= columns
    with engine.begin() as connection:
        user_id = add_user(connection)
        connection.execute(
            sa.insert(identity.session).values(
                id=new_ulid(),
                user_id=user_id,
                token_hash=SHA,
                csrf_secret="s",
                last_seen_at=NOW,
                auth_time=NOW,
                absolute_expiry=NOW + timedelta(days=30),
            )
        )
        row = connection.execute(sa.select(identity.session.c.remember)).scalar_one()
        assert row is False  # default: no "remember me"


@pytest.mark.req("EF-USR-003")
def test_api_key_prefix_unique_and_scopes_a_json_list(engine: Engine) -> None:
    with engine.begin() as connection:
        user_id = add_user(connection)
        values: dict[str, Any] = {
            "user_id": user_id,
            "prefix": "shk_dup",
            "hash": SHA,
            "read_only": True,
            "created_at": NOW,
            "expires_at": None,
        }
        connection.execute(
            sa.insert(identity.api_key).values(id=new_ulid(), scopes=["read"], **values)
        )
        rejected(
            connection,
            sa.insert(identity.api_key).values(id=new_ulid(), scopes=["read"], **values),
            "UNIQUE constraint failed: api_key.prefix",
        )
        assert connection.execute(sa.select(identity.api_key.c.scopes)).scalar_one() == ["read"]
        rejected(
            connection,
            sa.insert(identity.api_key).values(
                id=new_ulid(), scopes="read", **{**values, "prefix": "shk_other"}
            ),
            "ck_api_key_scopes_array",
        )


@pytest.mark.req("EF-USR-013")
def test_audit_actor_is_not_a_foreign_key_so_it_survives_user_deletion(engine: Engine) -> None:
    assert not identity.audit.c.actor_id.foreign_keys
    with engine.begin() as connection:
        user_id = add_user(connection)
        connection.execute(
            sa.insert(identity.audit).values(
                id=new_ulid(), actor_id=user_id, action="rating.set", target="work:1", at=NOW
            )
        )
        connection.execute(sa.delete(identity.user).where(identity.user.c.id == user_id))
        assert (
            connection.execute(sa.select(sa.func.count()).select_from(identity.audit)).scalar_one()
            == 1
        )


@pytest.mark.req("EF-INT-001")
def test_idempotency_key_is_scoped_per_actor_and_expires(engine: Engine) -> None:
    values: dict[str, Any] = {
        "key": "k1",
        "method": "POST",
        "path": "/api/v1/x",
        "request_hash": SHA,
        "created_at": NOW,
        "expires_at": NOW + timedelta(hours=24),
    }
    with engine.begin() as connection:
        connection.execute(sa.insert(identity.idempotency_key).values(actor_id="a", **values))
        connection.execute(sa.insert(identity.idempotency_key).values(actor_id="b", **values))
        rejected(
            connection,
            sa.insert(identity.idempotency_key).values(actor_id="a", **values),
            "UNIQUE constraint failed: idempotency_key.actor_id, idempotency_key.key",
        )
        rejected(
            connection,
            sa.insert(identity.idempotency_key).values(
                actor_id="c", **{**values, "request_hash": "nohash"}
            ),
            "ck_idempotency_key_request_hash_sha256_prefixed",
        )


@pytest.mark.req("ENF-TIME-001")
def test_datetimes_are_stored_in_utc_and_naive_values_are_refused(engine: Engine) -> None:
    paris = timezone(timedelta(hours=2))
    local = datetime(2026, 11, 4, 14, 0, tzinfo=paris)
    with engine.begin() as connection:
        user_id = add_user(connection)
        connection.execute(
            sa.insert(identity.audit).values(
                id=new_ulid(), actor_id=user_id, action="a", target="t", at=local
            )
        )
        raw = connection.execute(sa.text("SELECT at FROM audit")).scalar_one()
        assert str(raw).startswith("2026-11-04 12:00:00")
        back = connection.execute(sa.select(identity.audit.c.at)).scalar_one()
        assert back == local
        assert back.tzinfo is not None
        assert back.utcoffset() == timedelta(0)
        rejected(
            connection,
            sa.insert(identity.audit).values(
                id=new_ulid(), actor_id=user_id, action="a", target="t", at=datetime(2026, 1, 1)
            ),
            "naive datetime",
        )


@pytest.mark.req("EF-INT-026")
def test_instance_is_a_single_row_with_restore_markers(engine: Engine) -> None:
    columns = set(platform.instance.c.keys())
    assert {"refresh_mode", "restore_epoch", "restore_generation", "lease_heartbeat_at"} <= columns
    values = {"instance_id": new_ulid(), "jurisdiction": "FR"}
    with engine.begin() as connection:
        connection.execute(sa.insert(platform.instance).values(**values))
        rejected(
            connection,
            sa.insert(platform.instance).values(instance_id=new_ulid(), jurisdiction="FR"),
            "UNIQUE constraint failed: instance.singleton",
        )
    with engine.begin() as connection:
        row = connection.execute(sa.select(platform.instance)).one()
        assert row.refresh_mode == "piloted"
        assert row.restore_epoch == 0
        assert row.restore_generation == 0
        rejected(
            connection,
            sa.update(platform.instance).values(refresh_mode="sometimes"),
            "ck_instance_refresh_mode_values",
        )


@pytest.mark.req("EF-EXP-041", "EF-INT-026", "ENF-OPS-019")
def test_fs_intent_states_include_stale_from_backup_and_a_schema_version(engine: Engine) -> None:
    def intent(state: str) -> Any:
        return sa.insert(coherence.fs_intent).values(
            id=new_ulid(),
            op="rename",
            intent_schema_version="1.0",
            src_path="a",
            dst_path="b",
            state=state,
            epoch=0,
        )

    with engine.begin() as connection:
        for state in ("pending", "applying", "done", "cancelled", "stale_from_backup"):
            connection.execute(intent(state))
        rejected(connection, intent("finished"), "ck_fs_intent_state_values")
        rejected(
            connection,
            sa.insert(coherence.fs_intent).values(
                id=new_ulid(), op="chmod", intent_schema_version="1.0", state="pending", epoch=0
            ),
            "ck_fs_intent_op_values",
        )
        rejected(
            connection,
            sa.insert(coherence.fs_intent).values(
                id=new_ulid(), op="rename", state="pending", epoch=0
            ),
            "NOT NULL constraint failed: fs_intent.intent_schema_version",
        )


@pytest.mark.req("EF-EXP-041", "EF-EXP-059")
def test_fs_intent_roots_reference_physical_roots(engine: Engine) -> None:
    with engine.begin() as connection:
        root = new_ulid()
        connection.execute(
            sa.insert(platform.physical_root).values(
                root_id=root, path_entry="/library/manga", generation=1
            )
        )
        connection.execute(
            sa.insert(coherence.fs_intent).values(
                id=new_ulid(),
                op="copy_cross_root",
                intent_schema_version="1.0",
                src_root=root,
                dst_root=root,
                state="pending",
                epoch=0,
            )
        )
        rejected(
            connection,
            sa.insert(coherence.fs_intent).values(
                id=new_ulid(),
                op="rename",
                intent_schema_version="1.0",
                src_root=new_ulid(),
                state="pending",
                epoch=0,
            ),
            "FOREIGN KEY constraint failed",
        )


@pytest.mark.req("EF-EXP-042")
def test_export_plan_carries_a_version_counter_and_ordered_ops(engine: Engine) -> None:
    plan_id = new_ulid()
    plan: dict[str, Any] = {
        "plan_id": plan_id,
        "kind": "export",
        "dry_run": True,
        "base_version": 3,
        "status": "planned",
    }
    with engine.begin() as connection:
        connection.execute(sa.insert(coherence.export_plan).values(**plan))
        rejected(
            connection,
            sa.insert(coherence.export_plan).values(
                **{**plan, "plan_id": new_ulid(), "base_version": "v1-"}
            ),
            "ck_export_plan_base_version_hex",
        )
        rejected(
            connection,
            sa.insert(coherence.export_plan).values(
                **{**plan, "plan_id": new_ulid(), "kind": "teleport"}
            ),
            "ck_export_plan_kind_values",
        )
        for position, op in enumerate(
            ("mkdir", "write", "rename", "recycle", "delete", "touch_mtime", "api_call")
        ):
            connection.execute(
                sa.insert(coherence.export_op).values(
                    id=new_ulid(),
                    plan_id=plan_id,
                    batch=0,
                    position=position,
                    op=op,
                    status="pending",
                )
            )
        rejected(
            connection,
            sa.insert(coherence.export_op).values(
                id=new_ulid(), plan_id=plan_id, batch=0, position=0, op="unlink", status="pending"
            ),
            "ck_export_op_op_values",
        )
        rejected(
            connection,
            sa.insert(coherence.export_op).values(
                id=new_ulid(), plan_id=plan_id, batch=0, position=0, op="mkdir", status="pending"
            ),
            "UNIQUE constraint failed: export_op.plan_id, export_op.batch, export_op.position",
        )
        connection.execute(sa.delete(coherence.export_plan))
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(coherence.export_op)
            ).scalar_one()
            == 0
        )


@pytest.mark.req("EF-EXP-029", "EF-EXP-059")
def test_one_mount_sentinel_per_physical_root(engine: Engine) -> None:
    root = new_ulid()
    with engine.begin() as connection:
        connection.execute(
            sa.insert(platform.physical_root).values(
                root_id=root, path_entry="/library", generation=1, st_dev=64768
            )
        )
        sentinel: dict[str, Any] = {
            "root_id": root,
            "st_dev": 64768,
            "capabilities": {"reflink": False},
            "mount_id": f"{new_ulid()}:1:{root}",
            "generation": 1,
            "checked_at": NOW,
            "state": "ok",
        }
        connection.execute(sa.insert(platform.mount_sentinel).values(**sentinel))
        rejected(
            connection,
            sa.insert(platform.mount_sentinel).values(**sentinel),
            "UNIQUE constraint failed: mount_sentinel.root_id",
        )
        connection.execute(sa.update(platform.mount_sentinel).values(state="mismatch"))
        rejected(
            connection,
            sa.update(platform.mount_sentinel).values(state="gone"),
            "ck_mount_sentinel_state_values",
        )
        rejected(
            connection,
            sa.insert(platform.physical_root).values(
                root_id=new_ulid(), path_entry="/library", generation=1
            ),
            "UNIQUE constraint failed: physical_root.path_entry",
        )  # path entry is unique


@pytest.mark.req("EF-EXP-042", "ENF-OPS-007")
def test_resource_lock_and_domain_slot_invariants(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.insert(coherence.resource_lock).values(
                resource="work:1", holder_job_id=new_ulid(), lease_until=NOW
            )
        )
        rejected(
            connection,
            sa.insert(coherence.resource_lock).values(
                resource="work:1", holder_job_id=new_ulid(), lease_until=NOW
            ),
            "UNIQUE constraint failed: resource_lock.resource",
        )
        connection.execute(
            sa.insert(platform.domain_slot).values(limit_key="src:a", slots=2, in_use=1)
        )
        rejected(
            connection,
            sa.insert(platform.domain_slot).values(limit_key="src:b", slots=1, in_use=2),
            "ck_domain_slot_in_use_range",
        )
        rejected(
            connection,
            sa.insert(platform.domain_slot).values(limit_key="src:c", slots=0, in_use=0),
            "ck_domain_slot_slots_min",
        )


@pytest.mark.req("EF-INT-003", "ENF-LEG-007", "ENF-OPS-014")
def test_schedule_and_job_view_shape(engine: Engine) -> None:
    assert {"cron", "jitter_s", "window", "last_run_at", "next_due_at", "enabled"} <= set(
        platform.schedule.c.keys()
    )
    assert {"huey_task_id", "dedupe_key", "source_key", "limit_key", "error_code"} <= set(
        platform.job_view.c.keys()
    )
    with engine.begin() as connection:
        connection.execute(
            sa.insert(platform.schedule).values(
                id=new_ulid(),
                cron="*/15 * * * *",
                jitter_s=42,
                job_kind="refresh",
                window={"start": "01:00", "end": "05:00"},
            )
        )
        job: dict[str, Any] = {
            "kind": "export",
            "status": "queued",
            "attempts": 0,
            "created_at": NOW,
        }
        connection.execute(sa.insert(platform.job_view).values(id=new_ulid(), **job))
        rejected(
            connection,
            sa.insert(platform.job_view).values(id=new_ulid(), **{**job, "status": "?"}),
            "ck_job_view_status_values",
        )
        rejected(
            connection,
            sa.insert(platform.job_view).values(id=new_ulid(), **{**job, "progress": 1.5}),
            "ck_job_view_progress_range",
        )


@pytest.mark.req("EF-INT-002", "ENF-OPS-009")
def test_event_has_schema_version_and_visibility(engine: Engine) -> None:
    base: dict[str, Any] = {
        "type": "unit.added",
        "schema_version": 1,
        "payload": {"n": 1},
        "occurred_at": NOW,
    }
    with engine.begin() as connection:
        for visibility in ("private", "household", "ops"):
            connection.execute(sa.insert(platform.event).values(visibility=visibility, **base))
        rejected(
            connection,
            sa.insert(platform.event).values(visibility="public", **base),
            "ck_event_visibility_values",
        )
        rejected(
            connection,
            sa.insert(platform.event).values(visibility="ops", **{**base, "schema_version": None}),
            "NOT NULL constraint failed: event.schema_version",
        )


@pytest.mark.req("EF-INT-002")
def test_notification_is_written_with_its_event_and_follows_it_on_purge(engine: Engine) -> None:
    with engine.begin() as connection:
        event_id = connection.execute(
            sa.insert(platform.event).values(
                type="request.approved",
                schema_version=1,
                payload={},
                occurred_at=NOW,
                visibility="private",
            )
        ).inserted_primary_key[0]
        connection.execute(
            sa.insert(platform.notification_pending).values(
                id=new_ulid(), event_id=event_id, channel="apprise", attempts=0, next_try_at=NOW
            )
        )
        rejected(
            connection,
            sa.insert(platform.notification_pending).values(
                id=new_ulid(),
                event_id=event_id + 99,
                channel="apprise",
                attempts=0,
                next_try_at=NOW,
            ),
            "FOREIGN KEY constraint failed",
        )
        connection.execute(sa.delete(platform.event))
        assert (
            connection.execute(
                sa.select(sa.func.count()).select_from(platform.notification_pending)
            ).scalar_one()
            == 0
        )


@pytest.mark.req("ENF-SEC-005", "EF-BIB-054")
def test_secret_keys_have_a_lifecycle(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(sa.insert(coherence.secret_key).values(kid="k1", state="active"))
        connection.execute(
            sa.insert(coherence.secret_key).values(kid="k0", state="retired", retired_at=NOW)
        )
        connection.execute(sa.insert(coherence.secret_key).values(kid="kx", state="compromised"))
        rejected(
            connection,
            sa.insert(coherence.secret_key).values(kid="k2", state="retired"),
            "ck_secret_key_retired_has_date",
        )
        rejected(
            connection,
            sa.insert(coherence.secret_key).values(kid="k3", state="lost"),
            "ck_secret_key_state_values",
        )


@pytest.mark.req("EF-BIB-053", "EF-BIB-054")
def test_content_tombstone_stores_only_encrypted_raw_values(engine: Engine) -> None:
    forbidden = ("hash", "digest", "salt", "title", "name", "ref")
    for column in coherence.content_tombstone.c.keys():  # noqa: SIM118
        assert not any(word in column for word in forbidden), column
    assert {"scope", "seq", "kid", "alg", "nonce", "ct", "epoch"} <= set(
        coherence.content_tombstone.c.keys()
    )
    entry: dict[str, Any] = {
        "kid": "k1",
        "v": 2,
        "alg": "xchacha20poly1305",
        "nonce": b"n" * 24,
        "ct": b"c" * 40,
        "created_at": NOW,
        "epoch": 0,
        "reason": "opaque",
    }
    with engine.begin() as connection:
        rejected(
            connection,
            sa.insert(coherence.content_tombstone).values(
                id=new_ulid(), scope="variant", seq=1, **entry
            ),
            "FOREIGN KEY constraint failed",
        )  # kid not in the issued registry
        connection.execute(
            sa.insert(coherence.tombstone_kid).values(kid="k1", first_seq=1, created_at=NOW)
        )
        for seq, scope in enumerate(("variant", "place", "series_binding", "work"), start=1):
            connection.execute(
                sa.insert(coherence.content_tombstone).values(
                    id=new_ulid(), scope=scope, seq=seq, **entry
                )
            )
        rejected(
            connection,
            sa.insert(coherence.content_tombstone).values(
                id=new_ulid(), scope="edition", seq=9, **entry
            ),
            "ck_content_tombstone_scope_values",
        )
        rejected(
            connection,
            sa.insert(coherence.content_tombstone).values(
                id=new_ulid(), scope="work", seq=1, **entry
            ),
            "UNIQUE constraint failed: content_tombstone.seq",
        )  # seq is unique
        rejected(
            connection,
            sa.insert(coherence.content_tombstone).values(
                id=new_ulid(), scope="work", seq=10, **{**entry, "alg": "aes"}
            ),
            "ck_content_tombstone_alg_known",
        )
        for bad, name in (
            ({"v": 0}, "ck_content_tombstone_v_min"),
            ({"reason": "x" * 65}, "ck_content_tombstone_reason_length"),
        ):
            rejected(
                connection,
                sa.insert(coherence.content_tombstone).values(
                    id=new_ulid(), scope="work", seq=11, **{**entry, **bad}
                ),
                name,
            )


@pytest.mark.req("EF-BIB-052")
def test_tombstone_pending_root_is_keyed_by_root_and_sequence(engine: Engine) -> None:
    root = new_ulid()
    with engine.begin() as connection:
        connection.execute(
            sa.insert(platform.physical_root).values(
                root_id=root, path_entry="/library/books", generation=1
            )
        )
        line = {"root_id": root, "seq": 1, "line": '{"v":"2.0"}', "created_at": NOW}
        connection.execute(sa.insert(coherence.tombstone_pending_root).values(**line))
        rejected(
            connection,
            sa.insert(coherence.tombstone_pending_root).values(**line),
            "UNIQUE constraint failed: tombstone_pending_root.root_id, tombstone_pending_root.seq",
        )
        connection.execute(sa.insert(coherence.tombstone_pending_root).values(**{**line, "seq": 2}))


@pytest.mark.req("EF-EXP-042")
def test_export_plan_rejects_a_non_integer_or_negative_version_and_unprefixed_fingerprint(
    engine: Engine,
) -> None:
    plan: dict[str, Any] = {"kind": "export", "dry_run": False, "status": "planned"}
    with engine.begin() as connection:
        connection.execute(
            sa.insert(coherence.export_plan).values(
                plan_id=new_ulid(), base_version=0, files_fingerprint=SHA, **plan
            )
        )
        rejected(
            connection,
            sa.insert(coherence.export_plan).values(plan_id=new_ulid(), base_version="-1", **plan),
            "ck_export_plan_base_version_hex",
        )
        rejected(
            connection,
            sa.insert(coherence.export_plan).values(
                plan_id=new_ulid(), base_version=1, files_fingerprint="abc", **plan
            ),
            "ck_export_plan_files_fingerprint_sha256_prefixed",
        )


@pytest.mark.req("EF-USR-022")
def test_audit_records_the_session_and_its_auth_time(engine: Engine) -> None:
    assert identity.audit.c.session_id.nullable
    assert identity.audit.c.auth_time.nullable
    assert not identity.audit.c.session_id.foreign_keys
    values: dict[str, Any] = {"actor_id": "a", "action": "rating.set", "target": "work:1"}
    with engine.begin() as connection:
        connection.execute(sa.insert(identity.audit).values(id=new_ulid(), **values))
        session_id = new_ulid()
        connection.execute(
            sa.insert(identity.audit).values(
                id=new_ulid(), session_id=session_id, auth_time=NOW, **values
            )
        )
        row = connection.execute(
            sa.select(identity.audit.c.session_id, identity.audit.c.auth_time).where(
                identity.audit.c.session_id.is_not(None)
            )
        ).one()
        assert row == (session_id, NOW)
        rejected(
            connection,
            sa.insert(identity.audit).values(id=new_ulid(), session_id="nope", **values),
            "ck_audit_session_id_ulid",
        )


@pytest.mark.req("EF-USR-001")
def test_a_user_without_a_password_is_allowed_but_a_weak_hash_is_not(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.insert(identity.user).values(
                id=new_ulid(), username="sso", password_hash=None, role="member", can_rate=False
            )
        )
        rejected(
            connection,
            sa.insert(identity.user).values(
                id=new_ulid(),
                username="md5",
                password_hash="md5:abc",
                role="member",
                can_rate=False,
            ),
            "ck_user_password_hash_argon2id",
        )


@pytest.mark.req("EF-BIB-053", "ENF-SEC-005")
def test_tombstone_kid_is_append_only_and_a_cited_kid_stays_in_the_keyring(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.insert(coherence.tombstone_kid).values(kid="k1", first_seq=1, created_at=NOW)
        )
        connection.execute(sa.insert(coherence.secret_key).values(kid="k1", state="active"))
        connection.execute(sa.insert(coherence.secret_key).values(kid="k9", state="active"))
        rejected(
            connection,
            sa.update(coherence.tombstone_kid).values(first_seq=2),
            "tombstone_kid is append-only",
        )
        rejected(connection, sa.delete(coherence.tombstone_kid), "tombstone_kid is append-only")
        rejected(
            connection,
            sa.delete(coherence.secret_key).where(coherence.secret_key.c.kid == "k1"),
            "kid is cited by a tombstone",
        )
        connection.execute(  # an uncited key can still go
            sa.delete(coherence.secret_key).where(coherence.secret_key.c.kid == "k9")
        )


@pytest.mark.req("EF-INT-002")
def test_event_update_is_refused_with_create_all_too(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.insert(platform.event).values(
                type="a", schema_version=1, payload={}, occurred_at=NOW, visibility="ops"
            )
        )
        rejected(connection, sa.update(platform.event).values(type="b"), "event is append-only")


@pytest.mark.req("EF-BIB-054")
def test_tombstone_journal_markers_are_nullable_on_instance_and_sentinel(engine: Engine) -> None:
    for table in (platform.instance, platform.mount_sentinel):
        assert table.c.tombstone_count.nullable
        assert table.c.tombstone_last_tag.nullable
    with engine.begin() as connection:
        connection.execute(
            sa.insert(platform.instance).values(
                instance_id=new_ulid(), jurisdiction="FR", tombstone_count=3, tombstone_last_tag="t"
            )
        )
        assert connection.execute(sa.select(platform.instance.c.tombstone_count)).scalar_one() == 3


@pytest.mark.req("EF-INT-003")
def test_schedule_is_unique_per_job_kind_and_scope(engine: Engine) -> None:
    def schedule(kind: str, scope: str | None) -> Any:
        values: dict[str, Any] = {"id": new_ulid(), "job_kind": kind, "cron": "* * * * *"}
        if scope is not None:
            values["scope_key"] = scope
        return sa.insert(platform.schedule).values(**values)

    with engine.begin() as connection:
        connection.execute(schedule("refresh", None))
        connection.execute(schedule("refresh", "source:a"))
        connection.execute(schedule("refresh", "source:b"))
        assert connection.execute(
            sa.select(platform.schedule.c.scope_key)
            .where(platform.schedule.c.job_kind == "refresh")
            .order_by(platform.schedule.c.scope_key)
        ).scalars().all() == ["", "source:a", "source:b"]
        rejected(
            connection,
            schedule("refresh", "source:a"),
            "UNIQUE constraint failed: schedule.job_kind, schedule.scope_key",
        )
        rejected(
            connection,
            schedule("refresh", ""),
            "UNIQUE constraint failed: schedule.job_kind, schedule.scope_key",
        )


@pytest.mark.req("ENF-OPS-020")
def test_foreign_keys_used_by_cascades_are_indexed(engine: Engine) -> None:
    inspector = sa.inspect(engine)
    for table, column in (("notification_pending", "event_id"), ("fs_intent", "plan_id")):
        indexed = {tuple(i["column_names"]) for i in inspector.get_indexes(table)}
        assert (column,) in indexed, (table, column)
