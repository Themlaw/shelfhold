"""The identity commands of the host CLI, behind one interface with two channels.

`SocketIdentity` talks to the running server over the local socket (principal `local_cli`, audit
actor `local_cli:<uid>`); `LocalAuth` itself is the direct channel for a stopped server
(ENF-OPS-015). Errors cross the socket as codes and come back as the same exceptions.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from shelfhold.identity.public import AdminExistsError, CreatedApiKey, UnknownUserError
from shelfhold.infrastructure.local_socket import RpcFailure, call

_VALUE_ERRORS = frozenset(
    {"invalid_username", "password_too_short", "password_too_long", "invalid_expiry"}
)


class KeyView(Protocol):
    @property
    def prefix(self) -> str: ...
    @property
    def name(self) -> str | None: ...
    @property
    def scopes(self) -> tuple[str, ...]: ...
    @property
    def read_only(self) -> bool: ...
    @property
    def created_at(self) -> datetime: ...
    @property
    def expires_at(self) -> datetime | None: ...
    @property
    def last_used_at(self) -> datetime | None: ...
    @property
    def revoked_at(self) -> datetime | None: ...


class IdentityCommands(Protocol):
    def create_admin(self, username: str, password: str) -> str: ...
    def reset_password(self, username: str, password: str) -> int: ...
    def revoke_sessions(
        self, *, username: str | None = None, session_id: str | None = None
    ) -> int: ...
    def create_api_key(
        self, *, name: str | None, read_only: bool = False, expires_days: int | None = None
    ) -> CreatedApiKey: ...
    def rotate_api_key(
        self, prefix: str, *, expires_days: int | None = None
    ) -> CreatedApiKey | None: ...
    def api_keys(self) -> Sequence[KeyView]: ...
    def revoke_api_key(self, prefix: str) -> bool: ...


@dataclass(frozen=True)
class _Key:
    prefix: str
    name: str | None
    scopes: tuple[str, ...]
    read_only: bool
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None


def _when(value: object) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def _created(raw: object) -> CreatedApiKey:
    if not isinstance(raw, dict):
        raise ValueError("malformed_reply")
    return CreatedApiKey(
        value=raw["value"], prefix=raw["prefix"], expires_at=_when(raw["expires_at"])
    )


class SocketIdentity:
    def __init__(self, path: Path) -> None:
        self._path = path

    def _call(self, method: str, **params: object) -> object:
        try:
            return call(self._path, method, {k: v for k, v in params.items() if v is not None})
        except RpcFailure as failure:
            code = (failure.data or {}).get("code")
            if code == "unknown_user":
                raise UnknownUserError from None
            if code == "admin_exists":
                raise AdminExistsError from None
            if code in _VALUE_ERRORS:
                raise ValueError(code) from None
            if code == "key_rotation_failed":
                raise RuntimeError("api_key_prefix_exhausted") from None
            raise

    def create_admin(self, username: str, password: str) -> str:
        return str(self._call("identity.create_admin", username=username, password=password))

    def reset_password(self, username: str, password: str) -> int:
        return int(str(self._call("identity.reset_password", username=username, password=password)))

    def revoke_sessions(self, *, username: str | None = None, session_id: str | None = None) -> int:
        return int(
            str(self._call("identity.sessions_revoke", username=username, session_id=session_id))
        )

    def create_api_key(
        self, *, name: str | None, read_only: bool = False, expires_days: int | None = None
    ) -> CreatedApiKey:
        return _created(
            self._call(
                "identity.keys_create", name=name, read_only=read_only, expires_days=expires_days
            )
        )

    def rotate_api_key(
        self, prefix: str, *, expires_days: int | None = None
    ) -> CreatedApiKey | None:
        raw = self._call("identity.keys_rotate", prefix=prefix, expires_days=expires_days)
        return None if raw is None else _created(raw)

    def api_keys(self) -> Sequence[KeyView]:
        rows = self._call("identity.keys_list")
        if not isinstance(rows, list):
            raise ValueError("malformed_reply")
        return [
            _Key(
                prefix=row["prefix"],
                name=row["name"],
                scopes=tuple(row["scopes"]),
                read_only=row["read_only"],
                created_at=datetime.fromisoformat(row["created_at"]),
                expires_at=_when(row["expires_at"]),
                last_used_at=_when(row["last_used_at"]),
                revoked_at=_when(row["revoked_at"]),
            )
            for row in rows
        ]

    def revoke_api_key(self, prefix: str) -> bool:
        return bool(self._call("identity.keys_revoke", prefix=prefix))
