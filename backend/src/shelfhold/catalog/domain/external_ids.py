"""External identifiers (EF-BIB-003): what a value names, and the shape it is stored in.

An identifier is polymorphic (`entity_type`, the entity it names) and unique per entity type: one
AniList value names one Work, and may also name one Contributor. The provider key is a stable slug
(`anilist`, `mangaupdates`, `wikidata`, `isbn`...). An ISBN is stored as ISBN-13 under `isbn`
(an ISBN-10 is converted, a wrong check digit refused) and names an Edition or a Volume, never a
Work. Pure: no storage here.
"""

import re
from dataclasses import dataclass

__all__ = [
    "EXTERNAL_ID_ENTITY_TYPES",
    "ISBN_PROVIDER",
    "ExternalId",
    "ExternalIdError",
    "normalize_external_id",
]

EXTERNAL_ID_ENTITY_TYPES = ("series", "work", "edition", "volume", "contributor")
ISBN_PROVIDER = "isbn"
_ISBN_ALIASES = frozenset({"isbn", "isbn10", "isbn13"})
_ISBN_TARGETS = frozenset({"edition", "volume"})
_PROVIDER = re.compile(r"[a-z0-9_]+\Z")
_ISBN_SEPARATORS = re.compile(r"[\s-]")


class ExternalIdError(ValueError):
    """Not an identifier this catalog stores (unknown type, bad key, bad ISBN)."""


@dataclass(frozen=True)
class ExternalId:
    entity_type: str
    provider: str
    value: str
    id_scheme: str | None = None  # the provider's own scheme, as for `source_alias` (EF-SRC-022)


def _isbn13_check(digits12: str) -> str:
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits12))
    return str((10 - total % 10) % 10)


def _isbn10_valid(text: str) -> bool:
    total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(text))
    return total % 11 == 0


def _isbn13(raw: str) -> str:
    text = _ISBN_SEPARATORS.sub("", raw).upper()
    if len(text) == 10 and text[:9].isdigit() and (text[9].isdigit() or text[9] == "X"):
        if not _isbn10_valid(text):
            raise ExternalIdError("an ISBN-10 whose check digit is wrong")
        body = "978" + text[:9]
        return body + _isbn13_check(body)
    if len(text) == 13 and text.isascii() and text.isdigit():
        if _isbn13_check(text[:12]) != text[12]:
            raise ExternalIdError("an ISBN-13 whose check digit is wrong")
        return text
    raise ExternalIdError("not an ISBN-10 nor an ISBN-13")


def normalize_external_id(
    entity_type: str, provider: str, raw: str, *, id_scheme: str | None = None
) -> ExternalId:
    """The identifier as it is stored; `ExternalIdError` for one that cannot be."""
    if entity_type not in EXTERNAL_ID_ENTITY_TYPES:
        raise ExternalIdError(f"no external identifier names a {entity_type!r}")
    if not _PROVIDER.fullmatch(provider):
        raise ExternalIdError("a provider key is a lowercase slug")
    if provider in _ISBN_ALIASES:
        if entity_type not in _ISBN_TARGETS:
            raise ExternalIdError("an ISBN names an Edition or a Volume, never a Work")
        return ExternalId(entity_type, ISBN_PROVIDER, _isbn13(raw), id_scheme)
    value = raw.strip()
    if not value:
        raise ExternalIdError("an empty identifier")
    return ExternalId(entity_type, provider, value, id_scheme)
