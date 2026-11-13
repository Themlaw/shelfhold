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
from collections.abc import Iterable, Mapping
from typing import Final
from urllib.parse import unquote

from shelfhold.library.tombstones.values import Scope

__all__ = [
    "FOLDED_SCOPES",
    "REF_FIELDS",
    "EmptyRefError",
    "Node",
    "alias_family",
    "fold_ref",
    "folded_fields",
    "safe_fold",
]

_MAX_DECODINGS: Final = 4  # `%252F` is `%2F` is `/`: bounded, so a hostile ref cannot loop

# The fields of a reference that are compared folded, per scope (the others stay exact).
REF_FIELDS: Final[Mapping[Scope, tuple[str, ...]]] = {
    "series_binding": ("source_key", "sub_source", "series_ref"),
    "variant": ("source_key", "sub_source", "remote_unit_id"),
}
FOLDED_SCOPES: Final = frozenset(REF_FIELDS)


class EmptyRefError(ValueError):
    """A reference with nothing left once folded (`/`, `?`, invisible characters only): it names
    no series, so it is refused rather than compared (it would match everything or nothing)."""


def _fold_once(value: str) -> str:
    decoded = unquote(value)
    text = unicodedata.normalize("NFKC", decoded)
    text = "".join(c for c in text if unicodedata.category(c) != "Cf")  # soft hyphen, ZWSP...
    for marker in ("#", "?"):  # a fragment or a query never names another series
        text = text.partition(marker)[0]
    return text.strip().strip("/").strip().casefold()


def fold_ref(value: str) -> str:
    """The form under which two spellings of a reference are the same reference: percent-decoded
    (bounded), NFKC, format characters (Unicode Cf) and blanks removed, leading and trailing
    separators removed, a query or fragment and what follows it cut, compared without case.
    Idempotent. Raises `EmptyRefError` when nothing is left."""
    folded = value
    for _ in range(_MAX_DECODINGS):
        following = _fold_once(folded)
        if following == folded:
            break
        folded = following
    else:
        folded = _fold_once(folded)
    if not folded:
        raise EmptyRefError("the reference is empty once folded")
    return folded


def safe_fold(value: str) -> str:
    """`fold_ref`, or the case-folded value when nothing is left (a graph node must exist)."""
    try:
        return fold_ref(value)
    except EmptyRefError:
        return value.casefold()


Node = tuple[str, str]  # (source_key, remote_id), folded


def alias_family(
    edges: Iterable[tuple[str, str, str, str]], start: tuple[str, str], *, limit: int = 512
) -> tuple[Node, ...] | None:
    """The connected component of a reference in the graph of the aliases of a plugin, **every
    scheme, both directions** (`edges`: old key, old id, new key, new id). A reference is its own
    family when no alias touches it. Cycles are harmless (visited set). None when the component is
    larger than `limit`: the caller blocks, it never guesses."""
    graph: dict[Node, set[Node]] = {}
    for old_key, old_id, new_key, new_id in edges:
        old = (safe_fold(old_key), safe_fold(old_id))
        new = (safe_fold(new_key), safe_fold(new_id))
        graph.setdefault(old, set()).add(new)
        graph.setdefault(new, set()).add(old)
    root = (safe_fold(start[0]), safe_fold(start[1]))
    seen, queue = {root}, [root]
    while queue:
        for following in graph.get(queue.pop(), ()):
            if following not in seen:
                if len(seen) >= limit:
                    return None
                seen.add(following)
                queue.append(following)
    return tuple(sorted(seen))


def folded_fields(scope: Scope, flat: Mapping[str, str]) -> tuple[str, ...] | None:
    """The folded values of the reference fields of a flattened tombstone, or None when the scope
    has no reference to fold (or a field is missing)."""
    names = REF_FIELDS.get(scope)
    if names is None or any(name not in flat for name in names):
        return None
    return tuple(safe_fold(flat[name]) for name in names)
