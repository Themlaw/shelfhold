"""The export of one user (EF-USR-013): everything that is hers, as one JSON document.

Deleting a user is the cascade of the store (`SqlIdentityStore.delete_user`: the foreign keys
take her state, requests, sessions, keys and policies, the rest is un-named in the same
transaction, and the audit keeps one opaque token). The export is the other half of the right to
her data, and it is a **reserved action** like the deletion (an admin session, fresh, never a key):
it reveals one person's data to someone else.

The document is built from rows the store chose column by column. Nothing that proves or opens
something is in it (no password hash, no session token, no key hash, no passkey material), and no
Work title: a Work is named by its identifier, so the export of a child cannot carry a title she
may not see (ENF-SEC-010).
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from shelfhold.identity.application.ports import ProfileRecord
from shelfhold.identity.domain.visibility import effective_visibility

EXPORT_FORMAT = "shelfhold-user-export"
EXPORT_VERSION = 1


def _plain(value: Any) -> Any:
    """Dates as ISO text, bytes never (a column that holds some is not exported at all)."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def build_export(profile: ProfileRecord, rows: Mapping[str, Any]) -> dict[str, Any]:
    """The document of a profile, from what the store read for it."""
    account = dict(rows["account"])
    preferences = {
        key: account.pop(key)
        for key in (
            "locale", "timezone", "title_lang_pref", "theme", "color_scheme", "notify_mode",
            "quiet_start", "quiet_end",
        )
    }  # fmt: skip
    document = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "profile": {
            "id": profile.id,
            "username": profile.username,
            "role": profile.role,
            "max_age_rating": profile.max_age_rating,
            "can_rate": profile.can_rate,
            "visibility": effective_visibility(profile.role, profile.visibility),
            "hide_unrated_remote": profile.hide_unrated_remote,
            "subject_filter": list(profile.subject_filter),
            "kavita_username": account["kavita_username"],
            "created_at": account["created_at"],
        },
        "preferences": preferences,
        **{
            key: rows[key]
            for key in (
                "source_policies", "user_state", "requests", "reading_positions", "sessions",
                "api_keys", "passkeys", "audit",
            )
        },
    }  # fmt: skip
    return dict(_plain(document))
