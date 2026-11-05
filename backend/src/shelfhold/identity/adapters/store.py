"""SQL storage of users, sessions, API keys and audit entries (tables of migration 0002)."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.identity.application.ports import (
    ApiKeyRecord,
    AuditEntry,
    NewApiKey,
    NewSession,
    SessionRecord,
    UserRecord,
)
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import identity

_user = identity.user
_session = identity.session
_key = identity.api_key


def _user_of(row: Any) -> UserRecord:
    return UserRecord(
        id=row.u_id, username=row.u_username, role=row.u_role, password_hash=row.u_password_hash
    )


# Labelled: joined with `session` or `api_key`, whose own `id` and `user_id` must not clash.
_USER_COLUMNS = (
    _user.c.id.label("u_id"),
    _user.c.username.label("u_username"),
    _user.c.role.label("u_role"),
    _user.c.password_hash.label("u_password_hash"),
)


class SqlIdentityStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

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
        self, *, id: str, username: str, role: str, password_hash: str, now: datetime
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.insert(_user).values(
                    id=id,
                    username=username,
                    role=role,
                    can_rate=role == "admin",
                    password_hash=password_hash,
                    created_at=now,
                )
            )

    def set_password_hash(self, user_id: str, password_hash: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.update(_user).where(_user.c.id == user_id).values(password_hash=password_hash)
            )

    # --- sessions ----------------------------------------------------------------------------

    def insert_session(self, session: NewSession) -> None:
        with self._engine.begin() as conn:
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
        with self._engine.begin() as conn:
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
        with self._engine.begin() as conn:
            return conn.execute(query.values(revoked_at=now)).rowcount

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
        with self._engine.begin() as conn:
            result = conn.execute(
                sa.update(_key)
                .where(_key.c.prefix == prefix, _key.c.revoked_at.is_(None))
                .values(revoked_at=now)
            )
        return result.rowcount > 0

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

    def audit(self, entry: AuditEntry) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                sa.insert(identity.audit).values(
                    id=new_ulid(),
                    actor_id=entry.actor_id,
                    action=entry.action,
                    target=entry.target,
                    at=entry.at,
                    session_id=entry.session_id,
                    auth_time=entry.auth_time,
                )
            )
