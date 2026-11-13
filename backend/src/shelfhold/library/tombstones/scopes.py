"""How an upstream reference is compared with a tombstone across a migration of identity
(EF-SRC-022, EF-BIB-054 (4)).

A tombstone keeps a reference exactly as it was received. An alias, a plugin's `migrate_ref` or a
re-typed address may spell the same series differently: another case, a trailing slash, a wide
form, a percent-encoding. Such a spelling must not free a purged series, so the migration compares
**folded** forms on top of the exact comparison of the index. When in doubt it blocks.

`fold_ref` is the one place that says which spellings are one. It is applied to both sides, so a
tombstone laid under one spelling holds against every other.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from typing import Final
from urllib.parse import unquote

from shelfhold.library.tombstones.values import Scope

__all__ = ["FOLDED_SCOPES", "REF_FIELDS", "fold_ref", "folded_fields"]

_MAX_DECODINGS: Final = 4  # `%252F` is `%2F` is `/`: bounded, so a hostile ref cannot loop

# The fields of a reference that are compared folded, per scope (the others stay exact).
REF_FIELDS: Final[Mapping[Scope, tuple[str, ...]]] = {
    "series_binding": ("source_key", "sub_source", "series_ref"),
    "variant": ("source_key", "sub_source", "remote_unit_id"),
}
FOLDED_SCOPES: Final = frozenset(REF_FIELDS)


def fold_ref(value: str) -> str:
    """The form under which two spellings of a reference are the same reference.

    NFKC, percent-decoding (bounded), case folding, outer blanks and trailing separators removed;
    idempotent. A reference that is only separators keeps its separators."""
    folded = value
    for _ in range(_MAX_DECODINGS):
        decoded = unquote(folded)
        if decoded == folded:
            break
        folded = decoded
    folded = unicodedata.normalize("NFKC", folded).casefold().strip()
    stripped = folded.rstrip("/").strip()
    folded = stripped or folded
    # casefold and NFKC can each expose a new decodable or foldable form: fold until stable
    return folded if folded == value else fold_ref(folded)


def folded_fields(scope: Scope, flat: Mapping[str, str]) -> tuple[str, ...] | None:
    """The folded values of the reference fields of a flattened tombstone, or None when the scope
    has no reference to fold (or a field is missing)."""
    names = REF_FIELDS.get(scope)
    if names is None or any(name not in flat for name in names):
        return None
    return tuple(fold_ref(flat[name]) for name in names)
