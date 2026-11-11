"""The payload of an `upstream_snapshot` row: checked when it is written (EF-EXP-056, EF-SRC-035).

The snapshot is durable, saved with the database and kept until a detachment gave the settings
back, so what it holds must be harmless to keep: plain JSON, small, with no key that names a
secret and no value that is one (a registered secret in any form a URL carries it, a
`user:password@` part, a token shaped value). A payload that fails is refused and nothing is
stored.

The payload is checked against a **closed schema** the service declares (`snapshot_schema`): every
key and every nesting level it may hold, with the type of each value. A key that is not in the
schema is refused, whatever its name; there is no list of words to forget one from. A service
without a schema keeps no snapshot at all.

A service whose settings do hold something sensitive that the restoration needs declares it:
`snapshot_sensitive_fields`, a tuple of top-level field names. Those fields are not refused, they
are sealed in the ENF-SEC-005 envelope, bound to the table, the column and the row id (associated
data `upstream_snapshot`, `payload`, id), and stored under `$sealed`. Without a vault nothing
sensitive is kept at all.
"""

import base64
import json
import re
from collections.abc import Collection, Iterable, Mapping
from typing import Any, Final

from shelfhold.infrastructure.fetcher.refs import has_userinfo
from shelfhold.infrastructure.logging import known_secret_index
from shelfhold.infrastructure.secret_text import SecretIndex, contains_secret
from shelfhold.infrastructure.secrets.envelope import Envelope
from shelfhold.infrastructure.secrets.vault import SecretVault

__all__ = [
    "CAPABILITY",
    "MAX_PAYLOAD_BYTES",
    "SCHEMA_VERSION",
    "SEALED_KEY",
    "SnapshotRefusedError",
    "open_sealed",
    "prepare_payload",
    "seal_fields",
    "validate_payload",
]

CAPABILITY: Final = "upstream_restore"
SCHEMA_VERSION: Final = 1
MAX_PAYLOAD_BYTES: Final = 16 * 1024
SEALED_KEY: Final = "$sealed"
_TOKEN_VALUE: Final = re.compile(
    r"eyJ[\w-]+\.[\w-]+\.[\w-]*|\b(?:Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE
)
_MAX_DEPTH: Final = 6


class SnapshotRefusedError(ValueError):
    """The payload is not a plain, secret-free document (EF-EXP-056, passe 9)."""


# A schema is a type (`int`, `str`, `bool`, `float`), a one-item list (every item is that shape) or
# a dictionary of shapes (closed: no other key). A key that may be missing is `"name?"`.
Shape = Any


def _shaped(value: object, shape: Shape, where: str) -> None:
    if isinstance(shape, dict):
        if not isinstance(value, Mapping):
            raise SnapshotRefusedError(f"snapshot {where} must be an object")
        allowed = {k.rstrip("?"): v for k, v in shape.items()}
        for key in value:
            if key not in allowed:
                raise SnapshotRefusedError(f"snapshot key {where}{key!r} is not in the schema")
        for key, inner in shape.items():
            name = key.rstrip("?")
            if name in value:
                _shaped(value[name], inner, f"{where}{name}.")
            elif not key.endswith("?"):
                raise SnapshotRefusedError(f"snapshot is missing {where}{name!r}")
    elif isinstance(shape, list):
        if not isinstance(value, list):
            raise SnapshotRefusedError(f"snapshot {where} must be a list")
        for item in value:
            _shaped(item, shape[0], where)
    elif shape is float:
        if type(value) not in (int, float):
            raise SnapshotRefusedError(f"snapshot {where} must be a number")
    elif type(value) is not shape:
        raise SnapshotRefusedError(f"snapshot {where} has the wrong type")


def _plain(value: object, depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        raise SnapshotRefusedError("snapshot too deep")
    if isinstance(value, Mapping):
        for key, inner in value.items():
            if not isinstance(key, str):
                raise SnapshotRefusedError("snapshot keys must be text")
            _plain(inner, depth + 1)
    elif isinstance(value, list):
        for inner in value:
            _plain(inner, depth + 1)
    elif value is not None and not isinstance(value, str | int | float | bool):
        raise SnapshotRefusedError("snapshot holds a value that is not plain data")


def _walk(value: object, known: SecretIndex, depth: int = 0) -> None:
    if depth > _MAX_DEPTH:
        raise SnapshotRefusedError("snapshot too deep")
    if isinstance(value, Mapping):
        for key, inner in value.items():
            if not isinstance(key, str):
                raise SnapshotRefusedError("snapshot keys must be text")
            if contains_secret(key, known):
                raise SnapshotRefusedError("snapshot key holds a secret")
            _walk(inner, known, depth + 1)
    elif isinstance(value, list):
        for inner in value:
            _walk(inner, known, depth + 1)
    elif isinstance(value, str):
        if contains_secret(value, known):
            raise SnapshotRefusedError("snapshot holds a secret value")
        if has_userinfo(value) or _TOKEN_VALUE.search(value):
            raise SnapshotRefusedError("snapshot holds a credential")
    elif value is not None and not isinstance(value, int | float | bool):
        raise SnapshotRefusedError("snapshot holds a value that is not plain data")


def _size(payload: Mapping[str, Any]) -> None:
    if len(json.dumps(payload, separators=(",", ":"), sort_keys=True)) > MAX_PAYLOAD_BYTES:
        raise SnapshotRefusedError("snapshot too large")


def validate_payload(
    payload: Mapping[str, Any], *, schema: Shape, known: Iterable[str] | None = None
) -> dict[str, Any]:
    """Plain JSON in the shape of the service's closed `schema`, small, and with no secret in it
    (key or value): it is stored in the clear. `known` defaults to every registered secret."""
    if not isinstance(schema, dict):
        raise SnapshotRefusedError("the service declares no snapshot schema")
    values = known_secret_index() if known is None else SecretIndex(known)
    _shaped(payload, schema, "")
    _walk(payload, values)
    _size(payload)
    return dict(payload)


def prepare_payload(
    payload: Mapping[str, Any], sensitive: Collection[str], *, can_seal: bool, schema: Shape
) -> tuple[dict[str, Any], dict[str, Any]]:
    """`(clear part, sensitive part)`. The clear part is validated; the sensitive fields must be
    plain data and there must be a vault to seal them, else the payload is refused."""
    hidden = {k: v for k, v in payload.items() if k in sensitive}
    if hidden and not can_seal:
        raise SnapshotRefusedError("a sensitive field cannot be kept without the vault")
    _plain(hidden)
    clear = validate_payload(
        {k: v for k, v in payload.items() if k not in sensitive}, schema=schema
    )
    _size(payload)
    return clear, hidden


def seal_fields(vault: SecretVault, row_id: str, hidden: Mapping[str, Any]) -> dict[str, Any]:
    """The envelope (`v`, `kid`, `alg`, `nonce`, `ct`) of the sensitive fields, for this row."""
    sealed = vault.seal(
        json.dumps(dict(hidden), sort_keys=True).encode(),
        table="upstream_snapshot", column="payload", row_id=row_id,
    )  # fmt: skip
    return {
        "v": sealed.v, "kid": sealed.kid, "alg": sealed.alg,
        "nonce": base64.b64encode(sealed.nonce).decode(),
        "ct": base64.b64encode(sealed.ct).decode(),
    }  # fmt: skip


def open_sealed(vault: SecretVault, row_id: str, blob: Mapping[str, Any]) -> dict[str, Any]:
    """The sensitive fields back. Fails (an `EnvelopeError`) when the row is not the one they were
    sealed for."""
    envelope = Envelope(
        int(blob["v"]), str(blob["kid"]), str(blob["alg"]),
        base64.b64decode(str(blob["nonce"])), base64.b64decode(str(blob["ct"])),
    )  # fmt: skip
    plain = vault.open(envelope, table="upstream_snapshot", column="payload", row_id=row_id)
    loaded = json.loads(plain)
    if not isinstance(loaded, dict):
        raise SnapshotRefusedError("the sealed part of a snapshot is not a document")
    return dict(loaded)
