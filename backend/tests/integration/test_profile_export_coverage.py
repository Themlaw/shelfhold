"""The export of a profile (EF-USR-013) holds every personal table, and never a secret (J-FLUX-1).

`SqlIdentityStore.export_rows` picks its sections one by one: a table added by a later lot never
joins by itself. This test lists, from the schema, every table that points at a profile and
fails when one is neither exported nor excluded here with its reason, on the model of the full
export (`platform/export_all.py`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.identity.application.delete_user import SECTIONS
from shelfhold.infrastructure.schema import metadata

pytestmark = pytest.mark.req("EF-USR-013")

# table -> the section of the export that holds it
EXPORTED: dict[str, str] = {
    "profile_source_policy": "source_policies",
    "user_state": "user_state",
    "request": "requests",
    "follow_request": "follow_requests",
    "download_request": "download_requests",
    "notification_digest": "notification_digests",
    "reading_position": "reading_positions",
    "reading_conflict": "reading_conflicts",
    "session": "sessions",
    "api_key": "api_keys",
    "webauthn_credential": "passkeys",  # the list of keys, never the key material
    "audit": "audit",
    "device_token": "device_tokens",
    "device": "devices",  # M5 L02: name, kind and dates; the token behind it stays out
    "collection": "collections",
    "destination_account": "destination_accounts",
    "notify_destination": "notify_destinations",
    "read_marker": "read_markers",
    "tracker_hint": "tracker_hints",
    "request_list_import": "request_list_imports",
    "source_account": "source_accounts",
    "tachibk_import": "tachibk_imports",
}

# table -> why her export leaves it out
EXCLUDED: dict[str, str] = {
    "bundle_version": "a derived file the server rebuilds (OPDS block); no data of hers inside",
    "bulk_order": "an order an admin gave (`actor_id`); the audit holds the action",
    "activity_block": "a decision of an admin on a source (`created_by`), not data of the person",
    "gap_ack": "an admin's acknowledgement (`acked_by`); the audit holds it",
    "plugin_attestation": "an admin's attestation on a plugin (`attested_by`)",
    "team_switch": "an operation an admin ran on an Edition (`created_by`); the audit holds it",
    "webhook_subscription": "a delivery setting of the host (`created_by`); secret inside",
    "instance": "the instance row (`maintenance_by`): a state of the host",
    "unit": "`deleted_by` and the like: who acted on a Unit, not data of the person",
    "read_state_push_note": "a note of the push to Kavita, rebuilt by the next pass; no content",
}

SECRET_NAMES = (
    "secret", "password", "digest", "fingerprint", "key_hash", "public_key", "_ct", "_nonce",
    "_kid",
)  # fmt: skip


def _points_at_a_profile(table: sa.Table) -> bool:
    return any(
        fk.column.table.name == "user" and fk.column.name == "id" for fk in table.foreign_keys
    )


def test_every_table_that_points_at_a_profile_is_exported_or_excluded_with_a_reason() -> None:
    personal = {t.name for t in metadata.tables.values() if _points_at_a_profile(t)}
    unknown = sorted(personal - EXPORTED.keys() - EXCLUDED.keys())
    assert not unknown, (
        f"personal tables the profile export neither holds nor excludes: {unknown}; "
        "add a section to `SqlIdentityStore.export_rows` and to `EXPORTED`, or a reason to "
        "`EXCLUDED`"
    )
    stale = sorted((EXPORTED.keys() | EXCLUDED.keys()) - metadata.tables.keys())
    assert not stale, f"entries of this test for tables that no longer exist: {stale}"
    assert all(reason.strip() for reason in EXCLUDED.values())


def test_every_section_of_the_export_has_its_table() -> None:
    sections = set(SECTIONS) | {"audit"}
    held = set(EXPORTED.values()) | {"collection_items"}  # items hang under her collections
    assert sections <= held, sorted(sections - held)


def test_no_exported_table_gives_a_secret_column() -> None:
    from shelfhold.identity.adapters import store

    source = Path(store.__file__).read_text()
    start = source.index("def export_rows")
    body = source[start : source.index("# --- household profiles", start)]
    asked = {
        name
        for line in body.splitlines()
        if line.strip().startswith('"')
        for name in line.replace(",", " ").split()
        if name.startswith('"') and name.endswith('"') and name.strip('"').islower()
    }
    leaks = sorted(n for n in asked if any(w in n.strip('"') for w in SECRET_NAMES))
    assert not leaks, f"a secret column in the export of a profile: {leaks}"
