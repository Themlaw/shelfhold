"""The stored query of a saved selection: a closed structure, never a language (M5 L21, EF-BIB-017).

A selection is stored as a query and evaluated each time it is read, in the scope of the profile
that reads it (`catalog.application.selections`). What can be stored is decided here, and nowhere
else, so that the text of a query can never be SQL, an expression or a way round the filter:

- one JSON object, at most `MAX_QUERY_BYTES` long, with at most five keys, each of them known:
  `reading_state`, `kind`, `shelf`, `lang` (lists) and `added_within_days` (a number);
- a list holds at most `MAX_VALUES` plain texts of a fixed shape (a state of a closed list, a
  lowercase word, a ULID, a language tag); nothing is nested, so the depth is fixed at two;
- the criteria are added up (and); the values of one criterion are alternatives (or);
- a text that is not that structure is refused with a reason, and a query is stored in one
  canonical form (sorted, without repeats), so what is read back is what was checked.

The reading state is one of the profile's own (`unread`: no Unit of the Work opened; `in_progress`:
opened and not all read; `read`: every Unit of an Edition read).
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

__all__ = [
    "MAX_ADDED_DAYS",
    "MAX_QUERY_BYTES",
    "MAX_VALUES",
    "READING_STATES",
    "QueryRefusedError",
    "SelectionQuery",
    "dump_query",
    "parse_query",
]

MAX_QUERY_BYTES: Final = 2048
MAX_VALUES: Final = 20
MAX_ADDED_DAYS: Final = 3650
READING_STATES: Final = ("unread", "in_progress", "read")
_MAX_BRACKETS: Final = 8  # one object and four lists are all a query holds
_KIND = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_SHELF = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")
_LANG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,3}")
_LIST_FIELDS: Final = ("reading_state", "kind", "shelf", "lang")
_FIELDS: Final = (*_LIST_FIELDS, "added_within_days")

Reason = Literal[
    "not_json", "not_an_object", "too_big", "too_deep", "unknown_field", "bad_value",
    "too_many_values", "empty",
]  # fmt: skip


class QueryRefusedError(ValueError):
    """The query is not the closed structure; `reason` is a short code, never an echo."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason: Reason = reason


@dataclass(frozen=True)
class SelectionQuery:
    reading_state: tuple[str, ...] = ()
    kind: tuple[str, ...] = ()
    shelf: tuple[str, ...] = ()
    lang: tuple[str, ...] = ()
    added_within_days: int | None = None


def _no_repeated_key(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise QueryRefusedError("bad_value")
    return dict(pairs)


def _read(raw: Any) -> Any:
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise QueryRefusedError("not_json") from None
    if not isinstance(raw, str):
        raise QueryRefusedError("not_an_object")
    if len(raw.encode("utf-8")) > MAX_QUERY_BYTES:
        raise QueryRefusedError("too_big")  # refused on its length, never parsed
    if raw.count("[") + raw.count("{") > _MAX_BRACKETS:
        raise QueryRefusedError("too_deep")
    try:
        return json.loads(raw, object_pairs_hook=_no_repeated_key)
    except QueryRefusedError:
        raise
    except RecursionError:
        raise QueryRefusedError("too_deep") from None
    except ValueError:
        raise QueryRefusedError("not_json") from None


def _values(name: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise QueryRefusedError("bad_value")
    if len(value) > MAX_VALUES:
        raise QueryRefusedError("too_many_values")
    found: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise QueryRefusedError("bad_value")
        found.add(_one(name, item))
    return tuple(sorted(found))


def _one(name: str, item: str) -> str:
    if name == "reading_state":
        if item in READING_STATES:
            return item
    elif name == "kind":
        if _KIND.fullmatch(item):
            return item
    elif name == "shelf":
        if _SHELF.fullmatch(item):
            return item
    elif _LANG.fullmatch(item):  # lang
        return item.lower()
    raise QueryRefusedError("bad_value")


def parse_query(raw: str | bytes | Mapping[str, Any]) -> SelectionQuery:
    """The query a text (or an already decoded object) says, or `QueryRefusedError`."""
    document = _read(raw)
    if not isinstance(document, dict):
        raise QueryRefusedError("not_an_object")
    if any(key not in _FIELDS for key in document):
        raise QueryRefusedError("unknown_field")
    if isinstance(raw, Mapping):
        try:
            if len(json.dumps(document).encode("utf-8")) > MAX_QUERY_BYTES:
                raise QueryRefusedError("too_big")
        except (TypeError, ValueError, RecursionError):
            raise QueryRefusedError("bad_value") from None
    lists = {name: _values(name, document[name]) for name in _LIST_FIELDS if name in document}
    days = document.get("added_within_days")
    if "added_within_days" in document and (
        isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_ADDED_DAYS
    ):
        raise QueryRefusedError("bad_value")
    query = SelectionQuery(**lists, added_within_days=days)
    if not (query.reading_state or query.kind or query.shelf or query.lang) and days is None:
        raise QueryRefusedError("empty")
    return query


def dump_query(query: SelectionQuery) -> str:
    """The one text a query is stored as: sorted keys, no repeat, no space."""
    document: dict[str, Any] = {
        name: list(getattr(query, name)) for name in _LIST_FIELDS if getattr(query, name)
    }
    if query.added_within_days is not None:
        document["added_within_days"] = query.added_within_days
    return json.dumps(document, sort_keys=True, separators=(",", ":"))
