"""A device (M5 L02, EF-USR-008, EF-USR-022 (4), D19): what one pairing makes.

An e-reader, a phone or a browser is a **device**: it reads as one reader profile (its subject, as
for the token), it has a label a person chose and a kind from a closed list, it was created and it
is seen again and again. The secret it types is not here: that is the token's, and it changes
without the device changing (a rotation), while retiring the device ends every token it holds.
The same rule as for the token holds for who may handle one (`may_handle`): a member the devices of
their own profile, an admin those of every profile, a child none.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final

KINDS: Final = ("koreader", "kobo", "opds", "browser", "other")
DEFAULT_KIND: Final = "other"
LABEL_LIMIT: Final = 80


class DeviceKindError(ValueError):
    """A kind that is not on the closed list."""


def device_kind(value: object) -> str:
    """The kind asked for: `other` when none is given, an error for a word outside the list."""
    if value is None:
        return DEFAULT_KIND
    if not isinstance(value, str) or value not in KINDS:
        raise DeviceKindError("device_kind_unknown")
    return value


def device_name(value: object) -> str | None:
    """A label a person typed: trimmed, cut at `LABEL_LIMIT`, `None` when it says nothing."""
    if not isinstance(value, str):
        return None
    return value.strip()[:LABEL_LIMIT] or None


@dataclass(frozen=True)
class Device:
    id: str
    subject_user_id: str
    label: str | None
    kind: str
    created_at: datetime
    last_seen_at: datetime | None
    revoked_at: datetime | None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


def may_handle(*, role: str, user_id: str, subject_user_id: str) -> bool:
    """Whether a session of `role` may list, rotate or retire the devices of `subject_user_id`:
    the profile's own for a member, every profile's for an admin, nobody's for a child."""
    if role == "admin":
        return True
    return role == "member" and user_id == subject_user_id
