"""What a person asks for, and the rules of an approval (EF-USR-010).

A request names its target in one of four ways (`work_id`, `source_ref`, `isbn`, `free_text`) and
only becomes a Work when an admin approves it. Everything here is pure: the target is checked and
normalised, a child may only ask through the reference her own search gave, an approval of a
child's request needs an age (rating is approving, B2), and what is large asks for a confirmation.
"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from shelfhold.sdk.age import AGE_TIERS

__all__ = [
    "SIZE_CONFIRM_BYTES",
    "SIZE_CONFIRM_UNITS",
    "TARGET_KINDS",
    "InvalidTargetError",
    "RequestTarget",
    "approval_refusal",
    "can_transition",
    "child_may_ask",
    "needs_age",
    "parse_target",
    "pick_edition",
]

TARGET_KINDS = ("work_id", "source_ref", "isbn", "free_text")
SIZE_CONFIRM_UNITS = 100  # an approval above this asks for a confirmation ...
SIZE_CONFIRM_BYTES = 5 * 2**30  # ... and so does one above this (5 GiB)
MAX_TEXT = 200
_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
# the adults who can be asked for nothing more: any other role is read as a child (closed)
_ADULT_ROLES = frozenset({"admin", "member"})
_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"approved", "rejected", "not_found", "cancelled"}),
    "approved": frozenset({"arrived"}),
}


class InvalidTargetError(ValueError):
    """The target is not one (the message is a code, never an echo of the input)."""


@dataclass(frozen=True)
class RequestTarget:
    kind: str
    value: str


def _isbn(text: str) -> str:
    digits = re.sub(r"[\s-]", "", text).upper()
    if re.fullmatch(r"\d{9}[\dX]", digits):
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(digits))
        if total % 11:
            raise InvalidTargetError("invalid_isbn")
        body = "978" + digits[:9]
        return body + str(
            (10 - sum(int(c) * (1 + 2 * (i % 2)) for i, c in enumerate(body)) % 10) % 10
        )
    if re.fullmatch(r"\d{13}", digits):
        if sum(int(c) * (1 + 2 * (i % 2)) for i, c in enumerate(digits)) % 10:
            raise InvalidTargetError("invalid_isbn")
        return digits
    raise InvalidTargetError("invalid_isbn")


def _text(text: str) -> str:
    folded = " ".join(unicodedata.normalize("NFKC", text).split())
    if not folded or len(folded) > MAX_TEXT:
        raise InvalidTargetError("invalid_text")
    # a control, a format character (zero width, bidi override) or an unassigned one is never
    # part of a title somebody typed: it would be read otherwise than it displays
    if any(unicodedata.category(c) in {"Cc", "Cf", "Cs", "Co", "Cn"} for c in folded):
        raise InvalidTargetError("invalid_text")
    return folded


def parse_target(kind: str, value: str) -> RequestTarget:
    """The target, normalised; `InvalidTargetError` for what is not one."""
    if kind not in TARGET_KINDS or not isinstance(value, str):
        raise InvalidTargetError("invalid_kind")
    if kind in ("work_id", "source_ref"):
        if not _ULID.fullmatch(value):
            raise InvalidTargetError("invalid_identifier")
        return RequestTarget(kind, value)
    return RequestTarget(kind, _isbn(value) if kind == "isbn" else _text(value))


def child_may_ask(kind: str) -> bool:
    """A child asks only through the reference of the family search. A Work identifier would tell
    her whether a hidden title exists; an ISBN or a text would make her a search box."""
    return kind == "source_ref"


def can_transition(current: str, new: str) -> bool:
    return new in _TRANSITIONS.get(current, frozenset())


def needs_age(requester_role: str) -> bool:
    return requester_role not in _ADULT_ROLES


def _valid_age(age: object) -> bool:
    return isinstance(age, int) and not isinstance(age, bool) and age in AGE_TIERS


def approval_refusal(
    *, requester_role: str, age: object, already_rated: bool, units: int,
    size_bytes: int | None, confirmed: bool,
) -> str | None:  # fmt: skip
    """Why this approval may not go ahead: `choose_age` (a child's request with no age, and no
    rating the admin already made), `confirmation_required` (over 100 Units or 5 GiB); else None."""
    if units < 0 or (size_bytes is not None and size_bytes < 0):
        raise ValueError("a size cannot be negative")
    if needs_age(requester_role) and not already_rated and not _valid_age(age):
        return "choose_age"
    over_bytes = size_bytes is not None and size_bytes > SIZE_CONFIRM_BYTES  # None: unknown
    if (units > SIZE_CONFIRM_UNITS or over_bytes) and not confirmed:
        return "confirmation_required"
    return None


def pick_edition(editions: Sequence[tuple[str, str]], locale: str) -> str | None:
    """The Edition to start from: the one in the language of the person who asked, else the
    first (the order given is the order of the catalogue, so the choice is stable)."""
    wanted = locale.lower().replace("_", "-")
    for edition_id, lang in editions:
        if lang.lower() == wanted:
            return edition_id
    for edition_id, lang in editions:
        if lang.lower().split("-")[0] == wanted.split("-")[0] and wanted != "und":
            return edition_id
    return editions[0][0] if editions else None
