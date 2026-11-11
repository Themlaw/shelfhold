"""Provenance and lock of one field (EF-BIB-008): what a proposal does to a stored value.

A value keeps where it comes from (provider, kind of origin, confidence, when it was fetched). A
value an admin locked is never replaced, whatever a provider proposes: the proposal is ignored and
the locked value keeps its provenance. Pure: no storage here.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

__all__ = ["FieldDecision", "FieldKey", "StoredField", "decide", "encode_value"]

UNDETERMINED_LANG = "und"


@dataclass(frozen=True)
class FieldKey:
    """The unit of provenance and of override: one field of one entity, in one language."""

    entity_type: str
    entity_id: str
    field: str
    lang: str = UNDETERMINED_LANG


@dataclass(frozen=True)
class StoredField:
    value: str  # JSON text
    provider: str
    provenance: str  # provider, source, embedded, import, manual
    confidence: float | None
    locked: bool
    fetched_at: datetime


class FieldDecision(StrEnum):
    INSERT = "insert"  # nothing stored yet
    REPLACE = "replace"  # an unlocked value, another proposal
    SAME = "same"  # the same value from the same provider: nothing to write
    KEEP_LOCKED = "keep_locked"  # locked: the proposal is ignored, the provenance kept


def encode_value(value: object) -> str:
    """The stored text of a value: JSON, keys sorted, so that equal values compare equal."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decide(stored: StoredField | None, value: str, provider: str) -> FieldDecision:
    if stored is None:
        return FieldDecision.INSERT
    if stored.locked:
        return FieldDecision.KEEP_LOCKED
    if stored.value == value and stored.provider == provider:
        return FieldDecision.SAME
    return FieldDecision.REPLACE
