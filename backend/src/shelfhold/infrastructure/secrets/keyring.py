"""The keyring: every key ever issued, one active, retired ones kept (ENF-SEC-005).

A copy of `/data` taken before a rotation stays readable because the retired keys stay in the
keyring. The file is JSON, owner-only (0600), replaced atomically. `Keyring` is immutable:
rotating returns a new value that the store then writes.
"""

import base64
import binascii
import contextlib
import dataclasses
import fcntl
import json
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import nacl.utils

from shelfhold.infrastructure.secrets.atomic import write_private
from shelfhold.infrastructure.secrets.envelope import ALG, KEY_BYTES
from shelfhold.infrastructure.secrets.errors import (
    KeyringCorruptError,
    KeyringMissingError,
    KeyringPermissionsError,
    KeyringUnreadableError,
    RotationInProgressError,
)

KEYRING_FORMAT = 1
KEY_STATES = ("active", "retired", "compromised")


def _utc(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


def _stamp(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _parse_stamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("timestamp is not a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("naive timestamp")
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class Key:
    kid: str
    material: bytes = field(repr=False)
    state: str = "active"
    alg: str = ALG
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    retired_at: datetime | None = None

    @classmethod
    def generate(cls, now: datetime | None = None) -> "Key":
        return cls(
            kid="k-" + nacl.utils.random(8).hex(),
            material=nacl.utils.random(KEY_BYTES),
            created_at=_utc(now),
        )


@dataclass(frozen=True)
class Rotation:
    """A rotation in progress: the key rows are moving to, durable until they all moved."""

    to_kid: str
    started_at: datetime


@dataclass(frozen=True)
class Keyring:
    keys: tuple[Key, ...]
    rotation: Rotation | None = None
    rotated_at: datetime | None = None
    exported_at: datetime | None = None

    def __post_init__(self) -> None:
        kids = [key.kid for key in self.keys]
        if len(set(kids)) != len(kids):
            raise ValueError("duplicate key id")
        if [key.state for key in self.keys].count("active") != 1:
            raise ValueError("a keyring has exactly one active key")

    @classmethod
    def generate(cls, now: datetime | None = None) -> "Keyring":
        return cls(keys=(Key.generate(now),))

    @property
    def active(self) -> Key:
        return next(key for key in self.keys if key.state == "active")

    def find(self, kid: str) -> Key | None:
        return next((key for key in self.keys if key.kid == kid), None)

    def get(self, kid: str) -> Key:
        key = self.find(kid)
        if key is None:
            raise KeyError(kid)
        return key

    def rotated(self, now: datetime | None = None) -> "Keyring":
        """A new active key; the previous one is retired, never dropped."""
        moment = _utc(now)
        fresh = Key.generate(moment)
        kept = tuple(
            dataclasses.replace(key, state="retired", retired_at=moment)
            if key.state == "active"
            else key
            for key in self.keys
        )
        return dataclasses.replace(
            self,
            keys=(*kept, fresh),
            rotation=Rotation(fresh.kid, moment),
            rotated_at=moment,
        )

    def rotation_completed(self, now: datetime | None = None) -> "Keyring":
        return dataclasses.replace(self, rotation=None)

    def exported(self, now: datetime | None = None) -> "Keyring":
        return dataclasses.replace(self, exported_at=_utc(now))

    def without(self, kid: str) -> "Keyring":
        return dataclasses.replace(self, keys=tuple(k for k in self.keys if k.kid != kid))

    def to_json(self) -> bytes:
        document: dict[str, Any] = {
            "format": KEYRING_FORMAT,
            "keys": [
                {
                    "kid": key.kid,
                    "alg": key.alg,
                    "state": key.state,
                    "key": base64.b64encode(key.material).decode("ascii"),
                    "created_at": _stamp(key.created_at),
                    "retired_at": _stamp(key.retired_at),
                }
                for key in self.keys
            ],
            "rotation": None
            if self.rotation is None
            else {"to_kid": self.rotation.to_kid, "started_at": _stamp(self.rotation.started_at)},
            "rotated_at": _stamp(self.rotated_at),
            "exported_at": _stamp(self.exported_at),
        }
        return json.dumps(document, indent=2, sort_keys=True).encode("utf-8") + b"\n"

    @classmethod
    def from_json(cls, raw: bytes, *, source: object = "keyring") -> "Keyring":
        try:
            document = json.loads(raw)
            if not isinstance(document, dict) or document.get("format") != KEYRING_FORMAT:
                raise ValueError("unsupported keyring format")
            keys = tuple(cls._parse_key(entry) for entry in document["keys"])
            if not keys:
                raise ValueError("no key")
            rotation = document.get("rotation")
            parsed_rotation = (
                None
                if rotation is None
                else Rotation(
                    str(rotation["to_kid"]), _require(_parse_stamp(rotation["started_at"]))
                )
            )
            keyring = cls(
                keys=keys,
                rotation=parsed_rotation,
                rotated_at=_parse_stamp(document.get("rotated_at")),
                exported_at=_parse_stamp(document.get("exported_at")),
            )
            if parsed_rotation is not None and keyring.active.kid != parsed_rotation.to_kid:
                raise ValueError("rotation target is not the active key")
            return keyring
        except (ValueError, KeyError, TypeError, binascii.Error) as exc:
            raise KeyringCorruptError(source, f"{type(exc).__name__}: {exc}") from None

    @staticmethod
    def _parse_key(entry: dict[str, Any]) -> Key:
        material = base64.b64decode(entry["key"], validate=True)
        state = entry["state"]
        retired_at = _parse_stamp(entry.get("retired_at"))
        if len(material) != KEY_BYTES or entry["alg"] != ALG or state not in KEY_STATES:
            raise ValueError("invalid key entry")
        if state != "active" and retired_at is None:
            raise ValueError("a retired key has a retirement date")
        return Key(
            kid=str(entry["kid"]),
            material=material,
            state=state,
            alg=entry["alg"],
            created_at=_require(_parse_stamp(entry["created_at"])),
            retired_at=retired_at,
        )


def _require(value: datetime | None) -> datetime:
    if value is None:
        raise ValueError("missing timestamp")
    return value


class KeyringStore:
    """Where the keyring lives, and the only code that reads or writes it."""

    def __init__(self, path: Path, *, lock_path: Path, external: bool = False) -> None:
        """`external`: a file provided by the operator (`SHELFHOLD_SECRET_KEY_FILE`, Docker or
        Kubernetes secret). It may be a symlink, read-only, owned by root; only a file that
        group or others can modify is refused. The file Shelfhold writes itself in `/data` is
        held to 0600, same uid, no symlink."""
        self.path = path
        self.lock_path = lock_path
        self.external = external

    def exists(self) -> bool:
        return os.path.lexists(self.path)

    def load(self) -> Keyring:
        try:
            flags = os.O_RDONLY | os.O_CLOEXEC | (0 if self.external else os.O_NOFOLLOW)
            descriptor = os.open(self.path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                self._check_permissions(os.fstat(handle.fileno()))
                raw = handle.read()
        except FileNotFoundError:
            raise KeyringMissingError(self.path, reason="absent") from None
        except OSError as exc:
            raise KeyringUnreadableError(self.path, exc.strerror or type(exc).__name__) from None
        return Keyring.from_json(raw, source=self.path)

    def _check_permissions(self, status: os.stat_result) -> None:
        if not stat.S_ISREG(status.st_mode):
            raise KeyringPermissionsError(self.path, "is not a regular file")
        if self.external:
            if status.st_mode & 0o022:
                raise KeyringPermissionsError(self.path, "can be modified by other users")
            if status.st_uid not in (0, os.geteuid()):
                raise KeyringPermissionsError(self.path, "belongs to another user")
            return
        if status.st_mode & 0o077:
            raise KeyringPermissionsError(self.path, "is accessible by other users")
        if status.st_uid != os.geteuid():
            raise KeyringPermissionsError(self.path, "belongs to another user")

    def create(self, keyring: Keyring) -> None:
        """First write: atomic, 0600, and never over an existing keyring."""
        write_private(self.path, keyring.to_json(), overwrite=False)

    def write(self, keyring: Keyring) -> None:
        write_private(self.path, keyring.to_json(), overwrite=True)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        """Serialise `secrets rotate`/`export`; raises at once if another process holds it."""
        descriptor = os.open(
            self.lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RotationInProgressError("another secrets operation is running") from None
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
