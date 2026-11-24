"""The state documents of a plugin and their migrations (EF-SRC-030, M6 L09b).

A plugin keeps state documents in the host (`plugin_state`), one per key, each versioned by
`payload_schema`. A version that changes the schema ships ``migrate_state(from_version, key,
payload)`` — the conformance kit fails a plugin that changes the schema without it — and the host
calls the hook before the first call of the new version: every document written by an older
schema is handed to the plugin's own hook and rewritten under the new one, stamped with the
version that wrote it (`state_written_by_version`).

The stamp is what the rollback is judged against (EF-SRC-020): a version that did not write the
state cannot be trusted to read it, so the host refuses to return to a version older than the
one the state names — the refusal says so, and the installed plugin is left exactly as it was.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import acquisition

__all__ = [
    "PluginStateError",
    "StateDocument",
    "declared_schema",
    "migrate_documents",
    "newest_written_version",
    "read_documents",
    "write_document",
]

_STATE = acquisition.plugin_state
MigrateState = Callable[[int, str, Any], Any]


class PluginStateError(Exception):
    """A refused state act, with its closed code and the detail the admin reads."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class StateDocument:
    """One state document of a plugin, as the host keeps it."""

    key: str
    payload: Any
    payload_schema: int
    written_by: str | None = None


def declared_schema(instance: Any) -> int:
    """The ``payload_schema`` the loaded plugin declares (1 when it says nothing: the schema
    every plugin starts with)."""
    declared = getattr(instance, "payload_schema", 1)
    if isinstance(declared, bool) or not isinstance(declared, int) or declared < 1:
        return 1
    return declared


def _hook_of(instance: Any) -> MigrateState | None:
    hook = getattr(instance, "migrate_state", None)
    return hook if callable(hook) else None


def read_documents(engine: Engine, plugin_id: str) -> list[StateDocument]:
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(
                _STATE.c.key, _STATE.c.payload, _STATE.c.payload_schema,
                _STATE.c.state_written_by_version,
            ).where(_STATE.c.plugin_id == plugin_id).order_by(_STATE.c.key)
        ).all()  # fmt: skip
    return [
        StateDocument(row.key, row.payload, row.payload_schema, row.state_written_by_version)
        for row in rows
    ]


def write_document(
    engine: Engine,
    plugin_id: str,
    document: StateDocument,
    *,
    audit: Callable[[Any], None] | None = None,
) -> None:
    """Write one state document as the host keeps it: the payload, its schema version, and the
    version of the plugin that wrote it. The caller's audit callback runs in the same
    transaction, like every lifecycle line."""
    with write_transaction(engine) as connection:
        connection.execute(
            sqlite_insert(_STATE)
            .values(
                plugin_id=plugin_id, key=document.key, payload=document.payload,
                payload_schema=document.payload_schema,
                state_written_by_version=document.written_by,
            )
            .on_conflict_do_update(
                index_elements=["plugin_id", "key"],
                set_={
                    "payload": document.payload,
                    "payload_schema": document.payload_schema,
                    "state_written_by_version": document.written_by,
                },
            )
        )  # fmt: skip
        if audit is not None:
            audit(connection)


def migrate_documents(
    engine: Engine,
    plugin_id: str,
    instance: Any,
    version: str,
    *,
    audit: Callable[[Any], None] | None = None,
) -> int:
    """The host's own act before the first call of a new version (EF-SRC-030): every document
    the plugin's declared schema does not match is handed to the plugin's ``migrate_state``
    and rewritten under the new schema, stamped with the version that migrated it.

    Refused, closed: a schema that changed without the hook (`migration_missing`), a state
    newer than the version the plugin declares (`state_newer`) — a version that did not write
    it cannot be trusted to read it — and a hook that is the plugin's own bug: one that raises,
    one that answers nothing, or one that answers what the host cannot keep (`hook_failed`).
    The plugin is untrusted; its error is not the host's. A refusal migrates nothing: the
    documents are left exactly as they were."""
    declared = declared_schema(instance)
    hook = _hook_of(instance)
    documents = read_documents(engine, plugin_id)
    migrated: list[StateDocument] = []
    for document in documents:
        if document.payload_schema == declared:
            continue
        if document.payload_schema > declared:
            raise PluginStateError(
                "state_newer",
                f"a state document written under schema {document.payload_schema} is newer than "
                f"the schema {declared} this version reads",
            )
        if hook is None:
            raise PluginStateError(
                "migration_missing",
                f"payload_schema changed from {document.payload_schema} to {declared} without "
                "migrate_state(from_version)",
            )
        try:
            payload = hook(document.payload_schema, document.key, document.payload)
        except Exception as error:  # the plugin is untrusted: its bug is a refusal, never the
            raise PluginStateError(  # host's own crash — and the state stays exactly as it was
                "hook_failed",
                f"migrate_state raised {type(error).__name__} for the document {document.key!r}",
            ) from error
        if payload is None:
            raise PluginStateError(
                "hook_failed",
                f"migrate_state answered nothing for the document {document.key!r}: a state "
                "document is never replaced by an empty answer",
            )
        try:  # the host keeps the payload as the JSON column takes it, or not at all
            json.dumps(payload)
        except (TypeError, ValueError):
            raise PluginStateError(
                "hook_failed",
                f"migrate_state answered a payload the host cannot keep for the document "
                f"{document.key!r}: a state document is kept as it can be read back",
            ) from None
        migrated.append(StateDocument(document.key, payload, declared, version))
    if not migrated:  # fmt: skip
        return 0
    with write_transaction(engine) as connection:
        for document in migrated:
            connection.execute(
                sqlite_insert(_STATE)
                .values(
                    plugin_id=plugin_id, key=document.key, payload=document.payload,
                    payload_schema=document.payload_schema,
                    state_written_by_version=document.written_by,
                )
                .on_conflict_do_update(
                    index_elements=["plugin_id", "key"],
                    set_={
                        "payload": document.payload,
                        "payload_schema": document.payload_schema,
                        "state_written_by_version": document.written_by,
                    },
                )
            )  # fmt: skip
        if audit is not None:
            audit(connection)
    return len(migrated)


def newest_written_version(engine: Engine, plugin_id: str) -> str | None:
    """The newest plugin version that wrote a state document of this plugin (None when there is
    no document, or none the migration of L09b stamped): what the rollback is judged against."""
    newest: str | None = None
    for document in read_documents(engine, plugin_id):
        written = document.written_by
        if written is None:
            continue
        if newest is None or _version_key(written) > _version_key(newest):
            newest = written
    return newest


def _version_key(version: str) -> list[tuple[int, str]]:
    """The lifecycle's own comparison (dot-separated, numeric where both parts are numbers):
    enough to tell which plugin version is the newer, without a dependency."""
    return [
        (int(part), "") if part.isdigit() else (-1, part)
        for part in version.replace("-", ".").split(".")
    ]
