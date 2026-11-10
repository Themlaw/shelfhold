"""The last line against a secret in a reference: the database refuses the write (EF-SRC-035).

Every statement that writes a table of references, of journals, of events or of payloads is
looked at before it runs. If any of its parameters holds a registered secret value (the values
`register_secret_value` was given, in any form a URL carries them: encoded once or twice, `+` for
a space, either case of the hexadecimal digits), the statement is refused with
`SecretInWriteError`, which names the table and never the value. The registry is the same one the
log scrubber uses: whatever the Fetcher injected, the vault opened or a passphrase loaded.

Each table of the schema is either guarded or exempt *with a reason*; a test fails when a new
table is neither, so the decision is taken when the table is created.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

from sqlalchemy import Engine, event

from shelfhold.infrastructure.logging import known_secret_values
from shelfhold.infrastructure.secret_text import contains_secret

__all__ = [
    "EXEMPT_TABLES",
    "GUARDED_TABLES",
    "SecretInWriteError",
    "check_write",
    "install_write_guard",
]


class SecretInWriteError(ValueError):
    """A write would put a secret in the database. The table is named, the value never is."""

    def __init__(self, table: str) -> None:
        super().__init__(f"refused: a write to {table} holds a registered secret value")
        self.table = table


# References (what a source names), the journals of what was done, the events and notices, the
# payloads of intents, tickets and snapshots, the provenance records, the caches keyed by URL.
GUARDED_TABLES: Final = frozenset(
    {
        "audit", "binding_release_sample", "content", "document_alias",
        "download_request", "download_request_unit", "event", "export_item", "export_op",
        "export_plan", "export_record", "fs_intent", "follow_notice", "follow_request",
        "gap_ack", "http_cache", "import_entry", "job_view", "kavita_access_audit",
        "metadata_cache",
        "notification_pending", "replica", "revision_pending", "safeguard_backlog",
        "series_follow", "series_gap", "source_alias", "source_binding", "source_failure",
        "source_item", "sub_source", "trash_item", "unit_variant", "upstream_snapshot",
        "variant_revision",
    }
)  # fmt: skip

_SEALED = "its secrets are sealed (ENF-SEC-005) or hashed; the value is not a reference"
_CATALOG = "catalog metadata (titles, names, numbers): not a reference, and never a credential"
_SETTING = "a setting or an identity of the instance: not a reference"
_LOCK = "a lock, a sentinel, a slot or a schedule: no text from a source"
EXEMPT_TABLES: Final[dict[str, str]] = {
    "api_key": _SEALED, "app_setting": _SETTING, "backup_run": _SETTING,
    "contribution": _CATALOG, "contributor": _CATALOG, "contributor_name": _CATALOG,
    "domain_slot": _LOCK, "edition": _CATALOG, "edition_export_series": _CATALOG,
    "export_series": _CATALOG, "export_target": _SEALED, "external_id": _CATALOG,
    "field_owner": _CATALOG, "idempotency_key": _SETTING, "instance": _SETTING,
    "kavita_instance": _SEALED, "age_suggestion": _CATALOG,
    "merge_suggestion": _CATALOG, "metadata_field": _CATALOG, "mount_sentinel": _LOCK,
    "normalizer": _SETTING, "physical_root": _SETTING, "plugin_attestation": _SETTING,
    "plugin_install": _SETTING, "plugin_state": _SETTING, "provider_quota": _LOCK,
    "reading_position": _CATALOG, "remote_library": _SETTING, "resource_lock": _LOCK,
    "schedule": _LOCK, "secret_key": _SEALED, "series": _CATALOG, "series_member": _CATALOG,
    "session": _SEALED, "shelf": _SETTING, "shelf_destination": _SETTING,
    "shelf_destination_remote_library": _SETTING, "source_account": _SEALED,
    "source_config": _SEALED, "team": _CATALOG, "team_alias": _CATALOG, "team_priority": _CATALOG,
    "tombstone_kid": _SEALED, "tombstone_lost_kid": _SEALED, "tombstone_pending_root": _LOCK,
    "content_tombstone": _SEALED, "unit": _CATALOG, "unit_coverage": _CATALOG, "user": _SEALED,
    "volume": _CATALOG, "webauthn_credential": _SEALED, "work": _CATALOG,
    "work_kind": _CATALOG, "work_relation": _CATALOG, "work_relation_kind": _CATALOG,
    "work_title": _CATALOG,
}  # fmt: skip

_WRITE: Final = re.compile(
    r"""^\s*(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?)\s+["`\[]?(\w+)""",
    re.IGNORECASE,
)


def _strings(value: Any, depth: int = 0) -> list[str]:
    if depth > 6:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, bytes | bytearray):
        return [bytes(value).decode("utf-8", errors="replace")]
    if isinstance(value, Mapping):
        return [s for item in value.values() for s in _strings(item, depth + 1)]
    if isinstance(value, Sequence):
        return [s for item in value for s in _strings(item, depth + 1)]
    return []


def check_write(statement: str, parameters: Any, *, many: bool) -> None:
    """Raise `SecretInWriteError` when a guarded table is written with a registered secret."""
    match = _WRITE.match(statement)
    if match is None or match.group(1) not in GUARDED_TABLES or not parameters:
        return
    known = known_secret_values()
    if not known:
        return
    rows: Sequence[Any] = parameters if many else [parameters]
    for row in rows:
        for text in _strings(row):
            if contains_secret(text, known):
                raise SecretInWriteError(match.group(1))


def install_write_guard(engine: Engine) -> None:
    @event.listens_for(engine, "before_cursor_execute")
    def _guard(
        _conn: Any, _cursor: Any, statement: str, parameters: Any, _context: Any, many: bool
    ) -> None:
        check_write(statement, parameters, many=many)
