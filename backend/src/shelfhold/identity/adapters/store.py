"""SQL storage of users, sessions, API keys and audit entries (tables of migration 0002)."""

import secrets
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.identity.application.ports import (
    ApiKeyRecord,
    AuditEntry,
    ChildKavitaAccount,
    KeyConflictError,
    NewApiKey,
    NewPasskey,
    NewSession,
    PasskeyGoneError,
    PasskeyRecord,
    ProfileRecord,
    ProofWithdrawnError,
    SessionRecord,
    SourcePolicyRecord,
    UserRecord,
)
from shelfhold.identity.application.sessions import SessionView
from shelfhold.identity.domain.kavita_mapping import same_kavita_username
from shelfhold.identity.domain.policy import check_source_policy, effective_source_policy
from shelfhold.identity.domain.roles import ProfileError
from shelfhold.identity.domain.visibility import check_visibility, effective_visibility
from shelfhold.infrastructure.event_sequence import invalidate_tokens
from shelfhold.infrastructure.identity_counter import DAMAGED, IdentityCounter
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.requesters import live_actor
from shelfhold.infrastructure.restore_notice import (
    RestoreNoticeData,
    notice_active,
    read_notice,
    write_notice,
)
from shelfhold.infrastructure.schema import acquisition, coherence, identity, platform, reading

_user = identity.user
_session = identity.session
_key = identity.api_key
_passkey = identity.webauthn_credential


def _user_of(row: Any) -> UserRecord:
    return UserRecord(
        id=row.u_id,
        username=row.u_username,
        role=row.u_role,
        password_hash=row.u_password_hash,
        can_rate=bool(row.u_can_rate),
    )


# Labelled: joined with `session` or `api_key`, whose own `id` and `user_id` must not clash.
_USER_COLUMNS = (
    _user.c.id.label("u_id"),
    _user.c.username.label("u_username"),
    _user.c.role.label("u_role"),
    _user.c.password_hash.label("u_password_hash"),
    _user.c.can_rate.label("u_can_rate"),
)


def revoke_all_credentials(
    connection: sa.Connection,
    *,
    now: datetime,
    notice: RestoreNoticeData | None = None,
    counter: IdentityCounter | None = None,
    counter_at_least: int = 0,
    keep_notice: bool = True,
) -> tuple[int, int, int | None]:
    """Every live session and API key closed, one audit line and the restore notice written: after
    a restore, the credentials of the backup are the ones of a past (a key revoked since would be
    live again). Returns `(sessions, api_keys, counter)`: the numbers closed, and the new value of
    the identity counter (to `publish` once the transaction has committed; None without one).
    A key is made again in one command; the audit line names no user and holds no secret."""
    sessions = connection.execute(
        sa.update(_session).where(_session.c.revoked_at.is_(None)).values(revoked_at=now)
    ).rowcount
    keys = connection.execute(
        sa.update(_key).where(_key.c.revoked_at.is_(None)).values(revoked_at=now)
    ).rowcount
    connection.execute(
        audit_insert(
            AuditEntry(
                actor_id=None,
                action="restore.credentials_revoked",
                target=f"sessions={sessions} api_keys={keys}",
                at=now,
            )
        )
    )
    write_notice(
        connection,
        replace(
            notice or RestoreNoticeData(now.isoformat(), None, 0, 0),
            sessions=sessions,
            api_keys=keys,
        ),
        keep_existing=keep_notice,  # a notice nobody has seen yet is not replaced by a later one
    )
    value = counter.bump(connection, at_least=counter_at_least) if counter else None  # + the file
    return sessions, keys, value


def revoke_if_behind(
    connection: sa.Connection, counter: IdentityCounter, *, now: datetime
) -> tuple[int, int] | None:
    """The file of /data ahead of the database means the database was put back from an older
    state: every session and API key is revoked and the notice written, in the caller's
    transaction. Returns `(sessions, api_keys)` when it acted. Every write of identity runs this
    first (`SqlIdentityStore._changing`): a command that revokes something on a copied database,
    or a password rehashed at login, cannot bring the database level with the file unnoticed."""
    on_file = counter.file_value()
    if on_file is None or not (on_file == DAMAGED or on_file > counter.db_value(connection)):
        return None
    sessions, keys, _ = revoke_all_credentials(
        connection, now=now, counter=counter, counter_at_least=max(on_file, 0)
    )
    return sessions, keys


_LOCKED_RETRIES = 4


def reconcile_identity_counter(
    engine: Engine, counter: IdentityCounter, *, now: datetime
) -> tuple[int, int] | None:
    """At start, before any authenticated request. Returns `(sessions, api_keys)` when it revoked
    (see `revoke_if_behind`). Otherwise the file follows the database (a missing file is created).
    A database not migrated yet ("no such table") has nothing to compare; any other database
    error (a lock held by a shell, say) is retried and then stops the start: it is never skipped."""
    for attempt in range(_LOCKED_RETRIES + 1):
        try:
            with engine.begin() as connection:
                acted = revoke_if_behind(connection, counter, now=now)
                value = counter.db_value(connection)
            break
        except sa.exc.OperationalError as error:
            if "no such table" in str(error.orig):
                return None
            if attempt == _LOCKED_RETRIES:
                raise
            time.sleep(0.5 * (attempt + 1))
    if acted is None:
        if counter.file_value() is None:
            counter.publish(value)
        else:
            counter.settle(value)
    else:
        counter.publish(value)  # the revocation committed: the file follows
    return acted


def audit_insert(entry: AuditEntry) -> sa.Insert:
    """The audit line as an INSERT: a caller with a transaction of its own writes it in there, so
    the action and its trace commit or fail together (EF-USR-016)."""
    return sa.insert(identity.audit).values(
        id=new_ulid(),
        actor_id=live_actor(entry.actor_id),  # a user deleted since: a fresh token, never her id
        action=entry.action,
        target=entry.target,
        at=entry.at,
        session_id=entry.session_id,
        auth_time=entry.auth_time,
    )


def _passkey_of(row: Any) -> PasskeyRecord:
    return PasskeyRecord(
        id=row.id,
        user_id=row.user_id,
        credential_id=bytes(row.credential_id),
        public_key=bytes(row.public_key),
        sign_count=row.sign_count,
        transports=tuple(row.transports or ()),
        aaguid=row.aaguid,
        name=row.name,
        created_at=row.created_at,
        rp_id=row.rp_id,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


def _words_of(raw: object) -> tuple[str, ...]:
    """The words stored for a profile: text only (the column is JSON, whatever was written)."""
    if not isinstance(raw, list):
        return ()
    return tuple(w for w in raw if isinstance(w, str))


def _require_admin_caller(conn: sa.Connection, actor_id: str | None) -> None:
    """The caller is still an admin, read under the lock that writes (no demotion slips in)."""
    if actor_id is None:  # the host: nobody to re-check
        return
    role = conn.execute(sa.select(_user.c.role).where(_user.c.id == actor_id)).scalar()
    if role != "admin":
        raise ProfileError("not_allowed")


def _admin_count(conn: sa.Connection) -> int:
    return int(conn.execute(sa.select(sa.func.count()).where(_user.c.role == "admin")).scalar_one())


class _NoLiveKeyError(Exception):
    pass


def child_kavita_accounts(engine: Engine) -> Sequence[ChildKavitaAccount]:
    """The children and the account each declared, for the audit of Kavita (read only)."""
    return SqlIdentityStore(engine).child_kavita_accounts()


class SqlIdentityStore:
    def __init__(self, engine: Engine, counter: IdentityCounter | None = None) -> None:
        self._engine = engine
        self._counter = counter

    @contextmanager
    def _changing(self, *, immediate: bool = False) -> Iterator[sa.Connection]:
        """A transaction that revokes a credential or changes a password: it also adds one to the
        identity counter (in the database, same transaction; the file is written first, see
        `IdentityCounter.bump`). A database found behind the file is revoked before the change."""
        engine = (
            self._engine.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
            if immediate
            else self._engine
        )
        value: int | None = None
        with engine.begin() as conn:
            if self._counter is not None:
                revoke_if_behind(conn, self._counter, now=datetime.now(UTC))
            yield conn
            if self._counter is not None:
                value = self._counter.bump(conn)
        if self._counter is not None and value is not None:
            self._counter.publish(value)  # committed

    def restore_notice_active(self) -> bool:
        with self._engine.connect() as conn:
            return notice_active(conn)

    def password_login_suspended(self) -> bool:
        """A restore warning is up and the live passwords were not carried over."""
        with self._engine.connect() as conn:
            notice = read_notice(conn)
        return notice is not None and not notice.passwords_carried

    # --- users -------------------------------------------------------------------------------

    def _one_user(self, where: sa.ColumnElement[bool]) -> UserRecord | None:
        with self._engine.connect() as conn:
            row = conn.execute(sa.select(*_USER_COLUMNS).where(where)).first()
        return _user_of(row) if row else None

    def user_by_name(self, username: str) -> UserRecord | None:
        return self._one_user(_user.c.username == username)  # NOCASE collation

    def user_by_id(self, user_id: str) -> UserRecord | None:
        return self._one_user(_user.c.id == user_id)

    def admins(self) -> Sequence[UserRecord]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(*_USER_COLUMNS).where(_user.c.role == "admin").order_by(_user.c.id)
            ).all()
        return [_user_of(row) for row in rows]

    def has_role(self, role: str) -> bool:
        with self._engine.connect() as conn:
            return (
                conn.execute(sa.select(_user.c.id).where(_user.c.role == role)).first() is not None
            )

    def insert_user(
        self,
        *,
        id: str,
        username: str,
        role: str,
        password_hash: str,
        now: datetime,
        max_age_rating: int | None = None,
    ) -> None:
        """A child never has a NULL ceiling (NULL is not "all ages"): without one, the strictest
        (0) is laid down, whoever calls."""
        if role == "child" and max_age_rating is None:
            max_age_rating = 0
        with self._engine.begin() as conn:
            conn.execute(
                sa.insert(_user).values(
                    id=id,
                    username=username,
                    role=role,
                    can_rate=role == "admin",
                    password_hash=password_hash,
                    created_at=now,
                    max_age_rating=max_age_rating,
                )
            )

    def delete_user(
        self, user_id: str, *, keep_one_admin: bool = False, actor_id: str | None = None
    ) -> str | None:
        """Delete the account and keep its traces under one pseudonym (EF-USR-013, ENF-OPS-009).

        In one transaction the user row goes (its sessions, keys and passkeys with it) and every
        place that named it by id (`audit.actor_id` and `target`) takes the same
        random token `deleted:<hex>`: the entries are not removed and keep their date, their
        action and their order, and one person's entries stay linked to each other without
        naming anyone. (`event` is append-only and holds the id only 90 days, an opaque ULID whose
        user is gone.) The audit has no hash chain; if one is added, the pseudonym replaces the
        id outside the hashed fields. Returns the pseudonym, or None if there was no such user.
        """
        pseudonym = f"deleted:{secrets.token_hex(8)}"
        with self._changing(immediate=keep_one_admin) as conn:
            if keep_one_admin:
                _require_admin_caller(
                    conn, actor_id
                )  # decided here, in the transaction that deletes (no race)
                role = conn.execute(sa.select(_user.c.role).where(_user.c.id == user_id)).scalar()
                if role == "admin" and _admin_count(conn) <= 1:
                    raise ProfileError("last_admin")
            gone = conn.execute(sa.delete(_user).where(_user.c.id == user_id)).rowcount
            if not gone:
                return None
            audit = identity.audit
            conn.execute(
                sa.update(audit).where(audit.c.actor_id == user_id).values(actor_id=pseudonym)
            )
            # whatever the form of the target (`<id>`, `<id>|<what>`, `user=<id> session=<id>`):
            # a ULID is confused with nothing, so the id is replaced wherever it stands
            conn.execute(
                sa.update(audit)
                .where(sa.func.instr(audit.c.target, user_id) > 0)
                .values(target=sa.func.replace(audit.c.target, user_id, pseudonym))
            )
            keys = identity.idempotency_key  # 24 hours of replayable answers: not a trace to keep
            conn.execute(sa.delete(keys).where(keys.c.actor_id == user_id))
            self._unname(conn, user_id, pseudonym)
        return pseudonym

    @staticmethod
    def _unname(conn: sa.Connection, user_id: str, pseudonym: str) -> None:
        """What the foreign keys do not reach: the columns that name a user without being one
        (EF-USR-013: after a deletion no row outside the audit carries the id).

        - the decision of an admin on a request keeps the pseudonym (who decided stays one
          person, not a name);
        - a ticket of a download or a follow request stays for the admin (its outcome still
          matters) but belongs to nobody. Its visibility is NOT changed: a `private` ticket stays
          `private` with no owner, so the admin still sees it masked and unmasking stays the
          reserved, audited action it is (`request.unmask`); the household never sees it;
        - the waiver of a lost keyring (`tombstone_lost_kid`) and the pause of the queue
          (`app_setting`) name the admin who made them: the token replaces the id; so does the
          payload of a queued digest that cites her as the owner of an item;
        - the events that name her, as the actor or anywhere in the payload, are erased (the
          table is append-only for UPDATE, its DELETE is the retention's), and the removals that
          this cannot tell are told by cutting every `?since=` token (L11).
        """
        request = identity.request
        conn.execute(
            sa.update(request).where(request.c.decided_by == user_id).values(decided_by=pseudonym)
        )
        for table in (acquisition.download_request, acquisition.follow_request):
            conn.execute(
                sa.update(table)
                .where(table.c.requester_user_id == user_id)
                .values(requester_user_id=None)
            )
        lost = coherence.tombstone_lost_kid
        conn.execute(sa.update(lost).where(lost.c.actor_id == user_id).values(actor_id=pseudonym))
        for document, column in (("app_setting", "value"), ("notification_digest", "payload")):
            conn.execute(  # the text of the document, so that the JSON stays valid
                sa.text(
                    f"UPDATE {document} SET {column} = replace({column}, :id, :token) "  # noqa: S608
                    f"WHERE instr({column}, :id) > 0"
                ),
                {"id": user_id, "token": pseudonym},
            )
        event = platform.event
        mentioned = sa.or_(
            event.c.actor_id == user_id,
            sa.func.instr(sa.cast(event.c.payload, sa.String), user_id) > 0,
        )
        if conn.execute(sa.delete(event).where(mentioned)).rowcount:
            invalidate_tokens(conn)

    def export_rows(self, user_id: str, *, limit: int) -> dict[str, Any] | None:
        """Everything that is hers and that the export holds, as rows of plain values (EF-USR-013).

        Chosen column by column, so that what proves or opens something is never read: no password
        hash, no session token or secret, no key hash, no passkey material. The titles are not
        read either: a Work is named by its identifier, so a child's export cannot carry a title
        that she may not see. Each list stops at `limit` rows and the sections that were cut are
        named in `truncated` (years of reading positions are tens of thousands of rows)."""
        truncated: list[str] = []

        def rows(
            conn: sa.Connection, section: str, table: sa.Table, where: Any, *names: str
        ) -> list[Any]:
            columns = [table.c[name] for name in names]
            found = conn.execute(
                sa.select(*columns).where(where).order_by(columns[0]).limit(limit + 1)
            ).all()
            if len(found) > limit:
                truncated.append(section)
                found = found[:limit]
            return [dict(zip(names, row, strict=True)) for row in found]

        with self._engine.connect() as conn:
            user = conn.execute(
                sa.select(
                    _user.c.created_at, _user.c.locale, _user.c.timezone, _user.c.title_lang_pref,
                    _user.c.theme, _user.c.color_scheme, _user.c.notify_mode, _user.c.quiet_start,
                    _user.c.quiet_end, _user.c.kavita_username,
                ).where(_user.c.id == user_id)
            ).first()  # fmt: skip
            if user is None:
                return None
            policy, state = identity.profile_source_policy, identity.user_state
            request, digest = identity.request, identity.notification_digest
            audit, positions = identity.audit, reading.reading_position
            follow, download = acquisition.follow_request, acquisition.download_request
            # a key issued FOR her (a device token), or one of hers that is for nobody else: the
            # keys that an admin issued for a child belong to the child's export, not the admin's
            own_keys = sa.or_(
                _key.c.subject_user_id == user_id,
                sa.and_(_key.c.subject_user_id.is_(None), _key.c.user_id == user_id),
            )
            return {
                "account": dict(user._mapping),
                "source_policies": rows(
                    conn, "source_policies", policy, policy.c.user_id == user_id,
                    "source_key", "policy", "updated_at",
                ),
                "user_state": rows(
                    conn, "user_state", state, state.c.user_id == user_id,
                    "work_id", "followed", "favorite", "read_at", "stamp",
                ),
                "requests": rows(
                    conn, "requests", request, request.c.requester_user_id == user_id,
                    "id", "target_kind", "target_value", "state", "reject_reason", "visibility",
                    "work_id", "edition_id", "decided_at", "arrived_at", "created_at",
                    "updated_at",
                ),
                "follow_requests": rows(
                    conn, "follow_requests", follow, follow.c.requester_user_id == user_id,
                    "id", "source_key", "remote_id", "state", "visibility", "created_at",
                    "updated_at",
                ),
                "download_requests": rows(
                    conn, "download_requests", download, download.c.requester_user_id == user_id,
                    "id", "target_kind", "state", "visibility", "requested_at", "updated_at",
                ),
                "notification_digests": rows(  # when one is due; what it says is not a title
                    conn, "notification_digests", digest, digest.c.user_id == user_id, "due_at"
                ),
                "reading_positions": rows(
                    conn, "reading_positions", positions, positions.c.user_id == user_id,
                    "id", "carrier_type", "carrier_id", "content_hash", "locator", "percent",
                    "client_ts", "server_ts", "device_id",
                ),
                "sessions": rows(
                    conn, "sessions", _session, _session.c.user_id == user_id,
                    "id", "created_at", "last_seen_at", "auth_time", "absolute_expiry",
                    "user_agent_label", "ip", "revoked_at",
                ),
                "api_keys": rows(
                    conn, "api_keys", _key, own_keys,
                    "id", "name", "prefix", "scopes", "read_only", "created_at", "expires_at",
                    "last_used_at", "revoked_at",
                ),
                "passkeys": rows(
                    conn, "passkeys", _passkey, _passkey.c.user_id == user_id,
                    "id", "name", "created_at", "last_used_at", "revoked_at",
                ),
                "audit": rows(
                    conn, "audit", audit, audit.c.actor_id == user_id, "at", "action", "target"
                ),
                "truncated": truncated,
            }  # fmt: skip

    # --- household profiles (M3 L05, EF-USR-001, EF-USR-005) ---------------------------------

    @staticmethod
    def _profile_of(row: Any) -> ProfileRecord:
        return ProfileRecord(
            id=row.id,
            username=row.username,
            role=row.role,
            max_age_rating=row.max_age_rating,
            can_rate=bool(row.can_rate),
            visibility=row.visibility,
            hide_unrated_remote=row.hide_unrated_remote is not False,  # the safe side
            subject_filter=_words_of(row.subject_filter),
        )

    def profiles(self) -> Sequence[ProfileRecord]:
        columns = (_user.c.id, _user.c.username, _user.c.role, _user.c.max_age_rating,
                   _user.c.can_rate, _user.c.visibility, _user.c.hide_unrated_remote,
                   _user.c.subject_filter)  # fmt: skip
        with self._engine.connect() as conn:
            rows = conn.execute(sa.select(*columns).order_by(_user.c.created_at, _user.c.id)).all()
        return [self._profile_of(row) for row in rows]

    def profile(self, user_id: str) -> ProfileRecord | None:
        return next((p for p in self.profiles() if p.id == user_id), None)

    def create_profile(
        self, *, id: str, username: str, role: str, password_hash: str,
        max_age_rating: int | None, can_rate: bool, now: datetime, max_admins: int,
        actor_id: str | None = None,
    ) -> None:  # fmt: skip
        with self._changing(immediate=True) as conn:
            _require_admin_caller(conn, actor_id)
            taken = conn.execute(sa.select(_user.c.id).where(_user.c.username == username)).first()
            if taken is not None:  # NOCASE: "Mum" and "mum" are one name
                raise ProfileError("username_taken")
            if role == "admin" and _admin_count(conn) >= max_admins:
                raise ProfileError("too_many_admins")
            conn.execute(
                sa.insert(_user).values(
                    id=id,
                    username=username,
                    role=role,
                    can_rate=can_rate,
                    max_age_rating=max_age_rating,
                    password_hash=password_hash,
                    created_at=now,
                )
            )

    def update_profile(
        self, user_id: str, *, expected_role: str, expected_max_age_rating: int | None,
        expected_can_rate: bool, role: str, max_age_rating: int | None, can_rate: bool,
        max_admins: int, now: datetime, actor_id: str | None = None,
    ) -> ProfileRecord:  # fmt: skip
        with self._changing(immediate=True) as conn:
            _require_admin_caller(conn, actor_id)
            row = conn.execute(
                sa.select(_user.c.role, _user.c.max_age_rating, _user.c.can_rate).where(
                    _user.c.id == user_id
                )
            ).first()
            if row is None:
                raise ProfileError("not_found")
            current = row.role
            if (current, row.max_age_rating, bool(row.can_rate)) != (
                expected_role,
                expected_max_age_rating,
                expected_can_rate,
            ):  # a write made since it was read: the last one never wins silently
                raise ProfileError("changed_meanwhile")
            admins = _admin_count(conn)
            if current == "admin" and role != "admin" and admins <= 1:
                raise ProfileError("last_admin")
            if current != "admin" and role == "admin" and admins >= max_admins:
                raise ProfileError("too_many_admins")
            conn.execute(
                sa.update(_user)
                .where(_user.c.id == user_id)
                .values(role=role, max_age_rating=max_age_rating, can_rate=can_rate)
            )
            if current != role:  # a changed role starts again: no session or key of the old one
                policies = identity.profile_source_policy  # nor a policy of the old one: D14 again
                conn.execute(sa.delete(policies).where(policies.c.user_id == user_id))
                conn.execute(  # nor the family search of the old role: back to hide, no words
                    sa.update(_user)
                    .where(_user.c.id == user_id)
                    .values(hide_unrated_remote=True, subject_filter=[])
                )
                conn.execute(
                    sa.update(_session)
                    .where(_session.c.user_id == user_id, _session.c.revoked_at.is_(None))
                    .values(revoked_at=now)
                )
                conn.execute(
                    sa.update(_key)
                    .where(_key.c.user_id == user_id, _key.c.revoked_at.is_(None))
                    .values(revoked_at=now)
                )
            written = conn.execute(
                sa.select(_user.c.id, _user.c.username, _user.c.role, _user.c.max_age_rating,
                          _user.c.can_rate, _user.c.visibility, _user.c.hide_unrated_remote,
                          _user.c.subject_filter).where(_user.c.id == user_id)
            ).one()  # fmt: skip
            return self._profile_of(written)  # what this transaction wrote, never a later read

    def source_family(self, source_key: str) -> bool:
        config = acquisition.source_config
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(config.c.family_safe).where(config.c.plugin_id == source_key)
            ).first()
        return bool(row[0]) if row else False  # a source nobody configured is not a family one

    def set_source_policy(
        self, user_id: str, source_key: str, policy: str, *, now: datetime,
        actor_id: str | None = None,
    ) -> SourcePolicyRecord:  # fmt: skip
        """The role and the family mark are read under the lock that writes the row: the rule of
        a child (nothing but "forbidden" off a family source) holds at the moment of writing."""
        table = identity.profile_source_policy
        config = acquisition.source_config
        with self._changing(immediate=True) as conn:
            _require_admin_caller(conn, actor_id)
            role = conn.execute(sa.select(_user.c.role).where(_user.c.id == user_id)).scalar()
            if role is None:
                raise ProfileError("not_found")
            family = conn.execute(
                sa.select(config.c.family_safe).where(config.c.plugin_id == source_key)
            ).scalar()
            check_source_policy(role, policy, family=bool(family))
            conn.execute(
                sa.delete(table).where(table.c.user_id == user_id, table.c.source_key == source_key)
            )
            conn.execute(
                sa.insert(table).values(
                    user_id=user_id, source_key=source_key, policy=policy, updated_at=now
                )
            )
            return SourcePolicyRecord(
                source_key=source_key,
                policy=policy,
                effective=effective_source_policy(role, policy, family=bool(family)),
            )  # the row written, read from this transaction

    def visibility_state(self, user_id: str) -> tuple[str, str | None] | None:
        """`(role, stored visibility)` of a profile; None: no such profile."""
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(_user.c.role, _user.c.visibility).where(_user.c.id == user_id)
            ).first()
        return None if row is None else (row[0], row[1])

    def set_visibility(
        self,
        user_id: str,
        visibility: object,
        *,
        actor_id: str | None = None,
        by_admin: bool = False,
        audit: Callable[[str, str], AuditEntry | None] | None = None,
    ) -> tuple[str, str]:
        """Store the choice of a profile: `(visibility that held, visibility that holds)`. The role
        is read under the lock that writes, so a child just demoted or promoted meets the rule of
        her role now (`ProfileError`: `invalid_visibility`, `child_visibility_household`).
        `by_admin`: the caller is an admin, re-read under the same lock (`not_allowed`)."""
        immediate = self._engine.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
        with immediate.begin() as conn:
            if by_admin:
                _require_admin_caller(conn, actor_id)
            row = conn.execute(
                sa.select(_user.c.role, _user.c.visibility).where(_user.c.id == user_id)
            ).first()
            if row is None:
                raise ProfileError("not_found")
            before = effective_visibility(row.role, row.visibility)
            chosen = check_visibility(row.role, visibility)
            conn.execute(sa.update(_user).where(_user.c.id == user_id).values(visibility=chosen))
            after = effective_visibility(row.role, chosen)
            entry = None if audit is None else audit(before, after)
            if entry is not None:  # the change and its trace stand or fall together
                conn.execute(self._audit_insert(entry))
        return before, after

    def set_family_search(
        self, user_id: str, hide_unrated_remote: bool, subject_filter: tuple[str, ...], *,
        actor_id: str | None = None,
        audit: Callable[[ProfileRecord, ProfileRecord], AuditEntry | None] | None = None,
    ) -> tuple[ProfileRecord, ProfileRecord]:  # fmt: skip
        """The change and its audit entry stand or fall together; the admin caller is re-read
        under the lock that writes."""
        columns = (_user.c.id, _user.c.username, _user.c.role, _user.c.max_age_rating,
                   _user.c.can_rate, _user.c.visibility, _user.c.hide_unrated_remote,
                   _user.c.subject_filter)  # fmt: skip
        immediate = self._engine.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
        with immediate.begin() as conn:
            _require_admin_caller(conn, actor_id)
            row = conn.execute(sa.select(*columns).where(_user.c.id == user_id)).first()
            if row is None:
                raise ProfileError("not_found")
            if row.role != "child":  # only a child has a family search: nothing is left to wake up
                raise ProfileError("not_a_child")
            before = self._profile_of(row)
            conn.execute(
                sa.update(_user)
                .where(_user.c.id == user_id)
                .values(
                    hide_unrated_remote=hide_unrated_remote, subject_filter=list(subject_filter)
                )
            )
            after = self._profile_of(
                conn.execute(sa.select(*columns).where(_user.c.id == user_id)).one()
            )
            entry = None if audit is None else audit(before, after)
            if entry is not None:
                conn.execute(self._audit_insert(entry))
        return before, after

    def set_kavita_username(
        self, user_id: str, name: str | None, *, actor_id: str | None = None,
        audit: Callable[[bool, bool], AuditEntry | None] | None = None,
    ) -> tuple[bool, bool]:  # fmt: skip
        """Name (or clear) the Kavita account of a child: `(was mapped, is mapped)`.

        The caller is re-read as an admin under the lock that writes, the role of the profile is
        read there too, and no other profile may hold the same name (Kavita does not tell "Lea"
        from "lea"). The change and its audit entry stand or fall together; naming what is
        already named writes nothing."""
        immediate = self._engine.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
        with immediate.begin() as conn:
            _require_admin_caller(conn, actor_id)
            row = conn.execute(
                sa.select(_user.c.role, _user.c.kavita_username).where(_user.c.id == user_id)
            ).first()
            if row is None:
                raise ProfileError("not_found")
            if row.role != "child":  # the audit is of children: an adult has nothing to compare
                raise ProfileError("not_a_child")
            was = row.kavita_username is not None
            if row.kavita_username == name:
                return was, was
            if name is not None:
                held = conn.execute(
                    sa.select(_user.c.kavita_username).where(
                        _user.c.id != user_id, _user.c.kavita_username.is_not(None)
                    )
                ).scalars()
                if any(same_kavita_username(other, name) for other in held):
                    raise ProfileError("kavita_username_taken")
            conn.execute(sa.update(_user).where(_user.c.id == user_id).values(kavita_username=name))
            entry = None if audit is None else audit(was, name is not None)
            if entry is not None:
                conn.execute(self._audit_insert(entry))
        return was, name is not None

    def child_kavita_accounts(self) -> Sequence[ChildKavitaAccount]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(_user.c.id, _user.c.max_age_rating, _user.c.kavita_username)
                .where(_user.c.role == "child")
                .order_by(_user.c.id)
            )
            return [
                ChildKavitaAccount(
                    profile_id=row.id,
                    # a child with no readable ceiling is audited as the strictest, never as free
                    max_age=0 if row.max_age_rating is None else int(row.max_age_rating),
                    kavita_username=row.kavita_username,
                )
                for row in rows
            ]

    def source_policy_state(
        self, user_id: str, source_key: str
    ) -> tuple[str, str | None, bool] | None:
        """`(role, stored policy, family mark)` of a profile for a source in ONE statement: a
        child just demoted never meets the default of a member. None: no such profile."""
        table = identity.profile_source_policy
        config = acquisition.source_config
        query = (
            sa.select(_user.c.role, table.c.policy, config.c.family_safe)
            .select_from(_user)
            .outerjoin(
                table, sa.and_(table.c.user_id == _user.c.id, table.c.source_key == source_key)
            )
            .outerjoin(config, config.c.plugin_id == source_key)
            .where(_user.c.id == user_id)
        )
        with self._engine.connect() as conn:
            row = conn.execute(query).first()
        return None if row is None else (row[0], row[1], bool(row[2]))

    def stored_source_policies(self, user_id: str) -> dict[str, str]:
        table = identity.profile_source_policy
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(table.c.source_key, table.c.policy)
                .where(table.c.user_id == user_id)
                .order_by(table.c.source_key)
            ).all()
        return {row.source_key: row.policy for row in rows}

    def replace_password_hash(self, user_id: str, expected: str, password_hash: str) -> bool:
        with self._changing() as conn:
            return (
                conn.execute(
                    sa.update(_user)
                    .where(_user.c.id == user_id, _user.c.password_hash == expected)
                    .values(password_hash=password_hash)
                ).rowcount
                > 0
            )

    # --- passkeys (EF-USR-025) ---------------------------------------------------------------

    def insert_passkey(self, passkey: NewPasskey, audit: AuditEntry | None = None) -> bool:
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    sa.insert(_passkey).values(
                        id=passkey.id,
                        user_id=passkey.user_id,
                        credential_id=passkey.credential_id,
                        public_key=passkey.public_key,
                        sign_count=passkey.sign_count,
                        transports=list(passkey.transports),
                        aaguid=passkey.aaguid,
                        rp_id=passkey.rp_id,
                        name=passkey.name,
                        created_at=passkey.now,
                    )
                )
                if audit is not None:  # the registration and its trace stand or fall together
                    conn.execute(self._audit_insert(audit))
        except sa.exc.IntegrityError:
            return False
        return True

    def passkeys_of(self, user_id: str) -> Sequence[PasskeyRecord]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(_passkey)
                .where(_passkey.c.user_id == user_id, _passkey.c.revoked_at.is_(None))
                .order_by(_passkey.c.created_at, _passkey.c.id)
            ).all()
        return [_passkey_of(row) for row in rows]

    def passkey_by_credential(self, credential_id: bytes) -> PasskeyRecord | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                sa.select(_passkey).where(_passkey.c.credential_id == credential_id)
            ).first()
        return _passkey_of(row) if row else None

    def record_passkey_use(self, passkey_id: str, *, sign_count: int, now: datetime) -> bool:
        # The counter moves forward in the statement itself: of two concurrent presentations of
        # the same assertion only one finds `sign_count < :new` (0 stays 0: no counter).
        advanced = _passkey.c.sign_count < sign_count
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.update(_passkey)
                .where(
                    _passkey.c.id == passkey_id,
                    _passkey.c.revoked_at.is_(None),
                    sa.or_(
                        advanced, sa.and_(_passkey.c.sign_count == 0, sa.literal(sign_count) == 0)
                    ),
                )
                .values(sign_count=sign_count, last_used_at=now)
            )
        return result.rowcount == 1

    def revoke_passkey(self, user_id: str, passkey_id: str, *, now: datetime) -> str:
        with self._changing(immediate=True) as conn:
            live = (
                conn.execute(
                    sa.select(_passkey.c.id).where(
                        _passkey.c.user_id == user_id, _passkey.c.revoked_at.is_(None)
                    )
                )
                .scalars()
                .all()
            )
            if passkey_id not in live:
                return "not_found"
            password = conn.execute(
                sa.select(_user.c.password_hash).where(_user.c.id == user_id)
            ).scalar_one_or_none()
            if password is None and len(live) <= 1:
                return "last_factor"
            conn.execute(
                sa.update(_passkey).where(_passkey.c.id == passkey_id).values(revoked_at=now)
            )
            conn.execute(  # what that passkey opened goes with it
                sa.update(_session)
                .where(_session.c.passkey_id == passkey_id, _session.c.revoked_at.is_(None))
                .values(revoked_at=now)
            )
        return "revoked"

    def passkey_registrations_since_acknowledgement(self) -> list[tuple[str, str, str, datetime]]:
        """`(reference, username, passkey name, registered at)`; the reference is the id of the
        audit line of the registration, the one `acknowledge-passkeys --through` takes."""
        audit, user = identity.audit, _user
        with self._engine.connect() as conn:
            last = conn.execute(
                sa.select(sa.func.max(audit.c.target)).where(
                    audit.c.action == "passkey.registrations_acknowledged"
                )
            ).scalar_one()
            query = (
                sa.select(audit.c.id, user.c.username, _passkey.c.name, audit.c.at)
                .select_from(
                    audit.join(_passkey, _passkey.c.id == audit.c.target).join(
                        user, user.c.id == _passkey.c.user_id
                    )
                )
                .where(audit.c.action == "passkey.registered")
                .order_by(audit.c.id)
            )
            if last is not None:
                query = query.where(audit.c.id > last)
            return [(r.id, r.username, r.name, r.at) for r in conn.execute(query)]

    def passkey_rp_ids(self) -> set[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(_passkey.c.rp_id)
                .where(_passkey.c.revoked_at.is_(None), _passkey.c.rp_id.is_not(None))
                .distinct()
            ).scalars()
            return {str(rp_id) for rp_id in rows}

    def reset_credentials(
        self, user_id: str, password_hash: str, *, now: datetime
    ) -> tuple[int, int, int]:
        """`(sessions, passkeys, api keys)` closed with the new password, in one transaction."""
        with self._changing(immediate=True) as conn:
            conn.execute(
                sa.update(_user).where(_user.c.id == user_id).values(password_hash=password_hash)
            )
            passkeys = conn.execute(
                sa.update(_passkey)
                .where(_passkey.c.user_id == user_id, _passkey.c.revoked_at.is_(None))
                .values(revoked_at=now)
            ).rowcount
            sessions = conn.execute(
                sa.update(_session)
                .where(_session.c.user_id == user_id, _session.c.revoked_at.is_(None))
                .values(revoked_at=now)
            ).rowcount
            keys = conn.execute(
                sa.update(_key)
                .where(_key.c.user_id == user_id, _key.c.revoked_at.is_(None))
                .values(revoked_at=now)
            ).rowcount
        return int(sessions), int(passkeys), int(keys)

    # --- sessions ----------------------------------------------------------------------------

    def insert_session(self, session: NewSession) -> None:
        # the write lock is taken first whenever the proof can be withdrawn meanwhile: a passkey
        # revoked or a password reset between the check and the insert must not leave a session
        guarded = session.passkey_id is not None or session.password_hash is not None
        engine = (
            self._engine.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
            if guarded
            else self._engine
        )
        with engine.begin() as conn:
            if session.password_hash is not None:
                stored = conn.execute(
                    sa.select(_user.c.password_hash).where(_user.c.id == session.user_id)
                ).scalar()
                if stored != session.password_hash:  # reset (or removed) since it was checked
                    raise ProofWithdrawnError
            if session.passkey_id:  # the write lock is held: no revocation can come in between
                live = conn.execute(
                    sa.select(_passkey.c.id).where(
                        _passkey.c.id == session.passkey_id, _passkey.c.revoked_at.is_(None)
                    )
                ).first()
                if live is None:
                    raise PasskeyGoneError
            conn.execute(
                sa.insert(_session).values(
                    id=session.id,
                    user_id=session.user_id,
                    token_hash=session.token_hash,
                    csrf_secret=session.csrf_secret,
                    created_at=session.now,
                    last_seen_at=session.now,
                    auth_time=session.now,
                    absolute_expiry=session.absolute_expiry,
                    remember=session.remember,
                    ip=session.ip,
                    user_agent_label=session.device,
                    passkey_id=session.passkey_id,
                )
            )

    def session_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        query = (
            sa.select(_session, *_USER_COLUMNS)
            .join(_user, _user.c.id == _session.c.user_id)
            .where(_session.c.token_hash == token_hash)
        )
        with self._engine.connect() as conn:
            row = conn.execute(query).first()
        if row is None:
            return None
        return SessionRecord(
            id=row.id,
            user=_user_of(row),
            csrf_secret=row.csrf_secret,
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
            auth_time=row.auth_time,
            absolute_expiry=row.absolute_expiry,
            remember=bool(row.remember),
            revoked_at=row.revoked_at,
        )

    def touch_session(self, session_id: str, *, now: datetime, absolute_expiry: datetime) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(_session)
                .where(_session.c.id == session_id, _session.c.revoked_at.is_(None))
                .values(last_seen_at=now, absolute_expiry=absolute_expiry)
            )

    def rotate_session(
        self, session_id: str, *, token_hash: str, csrf_secret: str, now: datetime
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(_session)
                .where(_session.c.id == session_id, _session.c.revoked_at.is_(None))
                .values(
                    token_hash=token_hash, csrf_secret=csrf_secret, auth_time=now, last_seen_at=now
                )
            )

    def revoke_session_by_token_hash(self, token_hash: str, *, now: datetime) -> None:
        with self._changing() as conn:
            conn.execute(
                sa.update(_session)
                .where(_session.c.token_hash == token_hash, _session.c.revoked_at.is_(None))
                .values(revoked_at=now)
            )

    def revoke_sessions(
        self, *, now: datetime, user_id: str | None = None, session_id: str | None = None
    ) -> int:
        query = sa.update(_session).where(_session.c.revoked_at.is_(None))
        if user_id is not None:
            query = query.where(_session.c.user_id == user_id)
        if session_id is not None:
            query = query.where(_session.c.id == session_id)
        with self._changing() as conn:
            return conn.execute(query.values(revoked_at=now)).rowcount

    def live_sessions(self, *, now: datetime, user_id: str | None = None) -> Sequence[SessionView]:
        query = (
            sa.select(_session, _user.c.username.label("u_username"), _user.c.role.label("u_role"))
            .join(_user, _user.c.id == _session.c.user_id)
            .where(_session.c.revoked_at.is_(None), _session.c.absolute_expiry > now)
            .order_by(_session.c.last_seen_at.desc(), _session.c.id)
        )
        if user_id is not None:
            query = query.where(_session.c.user_id == user_id)
        with self._engine.connect() as conn:
            rows = conn.execute(query).all()
        return [
            SessionView(
                id=row.id,
                user_id=row.user_id,
                username=row.u_username,
                role=row.u_role,
                device=row.user_agent_label,
                ip=row.ip,
                created_at=row.created_at,
                auth_time=row.auth_time,
                last_seen_at=row.last_seen_at,
                absolute_expiry=row.absolute_expiry,
                remember=row.remember,
            )
            for row in rows
        ]

    def revoke_session_of(self, user_id: str, session_id: str, *, now: datetime) -> bool:
        query = sa.update(_session).where(
            _session.c.id == session_id,
            _session.c.user_id == user_id,
            _session.c.revoked_at.is_(None),
        )
        with self._changing() as conn:
            return conn.execute(query.values(revoked_at=now)).rowcount > 0

    # --- API keys ----------------------------------------------------------------------------

    def insert_api_key(self, key: NewApiKey) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.insert(_key).values(
                    id=key.id,
                    user_id=key.user_id,
                    name=key.name,
                    prefix=key.prefix,
                    hash=key.hash,
                    scopes=list(key.scopes),
                    read_only=key.read_only,
                    created_at=key.now,
                    expires_at=key.expires_at,
                )
            )

    def _keys(self, where: sa.ColumnElement[bool] | None = None) -> list[ApiKeyRecord]:
        query = sa.select(_key, *_USER_COLUMNS).join(_user, _user.c.id == _key.c.user_id)
        if where is not None:
            query = query.where(where)
        with self._engine.connect() as conn:
            rows = conn.execute(query.order_by(_key.c.created_at, _key.c.id)).all()
        return [
            ApiKeyRecord(
                id=row.id,
                user=_user_of(row),
                name=row.name,
                prefix=row.prefix,
                hash=row.hash,
                scopes=tuple(row.scopes),
                read_only=bool(row.read_only),
                created_at=row.created_at,
                expires_at=row.expires_at,
                last_used_at=row.last_used_at,
                revoked_at=row.revoked_at,
            )
            for row in rows
        ]

    def api_key_by_prefix(self, prefix: str) -> ApiKeyRecord | None:
        found = self._keys(_key.c.prefix == prefix)
        return found[0] if found else None

    def api_keys(self) -> Sequence[ApiKeyRecord]:
        return self._keys()

    def touch_api_key(self, key_id: str, *, now: datetime) -> None:
        with self._engine.begin() as conn:
            conn.execute(sa.update(_key).where(_key.c.id == key_id).values(last_used_at=now))

    def revoke_api_key(self, prefix: str, *, now: datetime) -> bool:
        with self._changing() as conn:
            result = conn.execute(
                sa.update(_key)
                .where(_key.c.prefix == prefix, _key.c.revoked_at.is_(None))
                .values(revoked_at=now)
            )
        return result.rowcount > 0

    def rotate_api_key(self, old_prefix: str, new: NewApiKey, *, audit: AuditEntry) -> bool:
        try:
            with self._changing() as conn:  # one transaction: both or neither
                revoked = conn.execute(
                    sa.update(_key)
                    .where(_key.c.prefix == old_prefix, _key.c.revoked_at.is_(None))
                    .values(revoked_at=new.now)
                )
                if revoked.rowcount != 1:
                    raise _NoLiveKeyError
                conn.execute(
                    sa.insert(_key).values(
                        id=new.id,
                        user_id=new.user_id,
                        name=new.name,
                        prefix=new.prefix,
                        hash=new.hash,
                        scopes=list(new.scopes),
                        read_only=new.read_only,
                        created_at=new.now,
                        expires_at=new.expires_at,
                    )
                )
                conn.execute(self._audit_insert(audit))
        except _NoLiveKeyError:  # the block was rolled back: nothing was written
            return False
        except sa.exc.IntegrityError as error:
            raise KeyConflictError from error
        return True

    # --- audit -------------------------------------------------------------------------------

    def audit_targets(self, action: str, *, since: datetime) -> list[str]:
        audit = identity.audit
        query = (
            sa.select(audit.c.target)
            .where(audit.c.action == action, audit.c.at >= since, audit.c.target.is_not(None))
            .distinct()
            .order_by(audit.c.target)
        )
        with self._engine.connect() as conn:
            return [str(target) for (target,) in conn.execute(query)]

    @staticmethod
    def _audit_insert(entry: AuditEntry) -> sa.Insert:
        return audit_insert(entry)

    def audit(self, entry: AuditEntry) -> None:
        with self._engine.begin() as conn:
            conn.execute(self._audit_insert(entry))
