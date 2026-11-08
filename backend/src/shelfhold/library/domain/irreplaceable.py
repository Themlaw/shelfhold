"""Irreplaceable content (EF-BIB-031): computed, never stored, never deleted without words.

A Unit is irreplaceable when nothing upstream can give it back: no active source binding with a
Variant still listed, or the source says the Unit is gone, or it was imported by hand (no Variant
at all). It then stays, is flagged, and only an explicit typed confirmation lets anyone delete it.
The confirmation is the Unit's own identifier: copying a button label proves nothing, typing the
identifier of what is about to be lost does.
"""

import hmac
from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, Final

CONFIRMATION_CODE: Final = "unit.confirmation_required"

# EF-BIB-042 (3): this many content errors in a row on a binding make its Units candidates.
CANDIDATE_AFTER: Final = 2


def is_candidate(consecutive_content_errors: int) -> bool:
    """Only the counter of content errors asks this (the caller never feeds it an auth, version,
    network or 5xx failure: those are not counted). A candidate is a name on a list; the safeguard
    that acts on it is a later step."""
    return consecutive_content_errors >= CANDIDATE_AFTER


class ConfirmationRequiredError(Exception):
    code: ClassVar[str] = CONFIRMATION_CODE

    def __init__(self, unit_id: str) -> None:
        super().__init__(f"{CONFIRMATION_CODE}: type {confirmation_phrase(unit_id)} to confirm")
        self.unit_id = unit_id


@dataclass(frozen=True)
class VariantState:
    binding_active: bool
    removed: bool  # `unit_variant.removed_at` is set: the source no longer lists it


def is_irreplaceable(variants: Sequence[VariantState], availability: str) -> bool:
    if availability == "removed":
        return True
    return not any(v.binding_active and not v.removed for v in variants)


def confirmation_phrase(unit_id: str) -> str:
    return unit_id


def require_typed_confirmation(unit_id: str, *, irreplaceable: bool, typed: str | None) -> None:
    if not irreplaceable:
        return
    expected = confirmation_phrase(unit_id).encode()
    if typed is None or not hmac.compare_digest(typed.strip().encode(), expected):
        raise ConfirmationRequiredError(unit_id)
