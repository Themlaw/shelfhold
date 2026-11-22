"""The `*_id` keys of an event payload (J-FLUX-2)."""

from typing import Final

KINDS: Final = ("work", "edition", "series", "unit")
CONTENT_ID_SUFFIXES: Final = tuple(f"{kind}_id" for kind in KINDS)
# The `*_id` keys an event may carry that name no content a reader could be refused (J-FLUX-2):
# anything else ending in `_id` is refused by `publish`, so a new event cannot name a Work, an
# Edition, a series or a Unit under a key the projection does not read.
NON_CONTENT_ID_KEYS: Final = frozenset({
    "user_id", "passkey_id", "intent_id", "root_id", "variant_id", "attach_id", "request_id",
    "job_id", "destination_id", "export_item_id", "shelf_destination_id", "upgrade_id",
    "assembly_id", "target_id", "shelf_id", "team_id", "plugin_id", "session_id", "import_id",
    "pending_item_id", "trash_item_id", "plan_id", "migration_id", "key_id", "public_id",
    "profile_id", "subject_user_id", "actor_id", "owner_id", "requester_user_id",
    "install_id", "instance_id", "template_id", "version_id", "binding_id", "from_binding_id",
    "to_binding_id", "source_id", "subscription_id", "event_id",
})  # fmt: skip

# Keys that end like a content key and name something else: a row of the destination's own
# tables or an id of the connected service, never a Work, an Edition, a series or a Unit of the
# catalogue (`event_refs` read `export_series_id` as a catalogue series, found none, and hid the
# event from every reader with a ceiling).
NOT_CATALOG_KEYS: Final = frozenset({"export_series_id", "kavita_series_id", "remote_unit_id"})


def is_content_key(key: str, kind: str | None = None) -> bool:
    """`key` names a content of the catalogue (of `kind`, or of any kind)."""
    if key in NOT_CATALOG_KEYS:
        return False
    suffixes = CONTENT_ID_SUFFIXES if kind is None else (f"{kind}_id",)
    return key.endswith(suffixes)
