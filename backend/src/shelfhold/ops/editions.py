"""The commands on one Edition's metadata and identifiers, and the sorted list (L12, L15).

`meta lock|unlock` (EF-BIB-008): an admin's value, or the value already stored, is locked; no
provider replaces it afterwards, and the provenance stays. `edition renumber-id` (EF-BIB-003): a
provider renumbered, the new identifier is added and the old one is marked `superseded`; nothing
is overwritten or deleted, and an identifier is never taken from the Edition that holds it.
`listing` (ENF-I18N-003): the Editions in the order of their title without its leading article,
by the language of the Edition.

None of them is a reserved action (the spec does not make it one): the caller is an admin, by the
host CLI or by an admin session or key. Everything the caller types is checked before anything is
written: the field is a slug, the value is bounded text, the language is a tag, the identifier is
one this catalog stores.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa

from shelfhold.catalog.public import (
    ExternalIdConflictError,
    ExternalIdError,
    FieldKey,
    edit_field,
    holder_of,
    is_age_field,
    lock_current,
    normalize_external_id,
    renumber_external_id,
    sort_key,
    unlock_field,
)
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import is_ulid
from shelfhold.infrastructure.schema import catalog
from shelfhold.ops.errors import InvalidChoiceError, UnknownSubjectError

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["MAX_LIMIT", "MAX_VALUE", "SORTS", "EditionOps"]

MAX_VALUE: Final = 4096  # characters of a typed value
MAX_LIMIT: Final = 1000
SORTS: Final = ("title",)
_FIELD: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_LANG: Final = re.compile(r"(und|[a-z]{2,3}(-[A-Za-z0-9]{1,8}){0,3})")
_ON: Final = ("edition", "work")


def _clean_field(name: str) -> str:
    if not _FIELD.fullmatch(name):
        raise InvalidChoiceError("a field is a lowercase name of at most 64 characters")
    if is_age_field(name):  # the age rating has its own safeguards: a lock must not pin it
        raise InvalidChoiceError("the age rating is not locked as a field")
    return name


def _clean_lang(lang: str) -> str:
    if len(lang) > 35 or not _LANG.fullmatch(lang):
        raise InvalidChoiceError("not a language tag")
    return lang


def _clean_value(value: str) -> str:
    if not value.strip() or len(value) > MAX_VALUE or "\x00" in value:
        raise InvalidChoiceError(f"a value is text of 1 to {MAX_VALUE} characters")
    return value


class EditionOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def _edition(self, connection: sa.Connection, edition_id: str) -> tuple[str, str]:
        """`(edition id, work id)`: an Edition that does not exist is told, never created."""
        if not is_ulid(edition_id):
            raise InvalidChoiceError("not an Edition identifier")
        row = connection.execute(
            sa.select(catalog.edition.c.id, catalog.edition.c.work_id).where(
                catalog.edition.c.id == edition_id
            )
        ).first()
        if row is None:
            raise UnknownSubjectError("no such Edition")
        return str(row.id), str(row.work_id)

    def _key(
        self, connection: sa.Connection, edition_id: str, name: str, lang: str, on: str
    ) -> FieldKey:
        if on not in _ON:
            raise InvalidChoiceError("a field is kept on the Edition or on its Work")
        edition, work = self._edition(connection, edition_id)
        return FieldKey(on, edition if on == "edition" else work, _clean_field(name),
                        _clean_lang(lang))  # fmt: skip

    def lock(
        self, edition_id: str, name: str, value: str | None, *, lang: str = "und",
        on: str = "edition",
    ) -> dict[str, Any]:  # fmt: skip
        """Lock a field: with `value`, an admin's own (`manual`); without, the one stored."""
        if value is not None:
            _clean_value(value)
        with write_transaction(self._comp.engine) as connection:
            key = self._key(connection, edition_id, name, lang, on)
            if value is None:
                if not lock_current(connection, key):
                    raise InvalidChoiceError("nothing is stored for that field: give a value")
            else:
                edit_field(connection, key, value, now=self._comp.clock.now())
        return {"edition_id": edition_id, "field": name, "lang": lang, "on": on, "locked": True,
                "kept": value is None}  # fmt: skip

    def unlock(
        self, edition_id: str, name: str, *, lang: str = "und", on: str = "edition"
    ) -> dict[str, Any]:  # fmt: skip
        """Lift the lock; the value stays, with its provenance, until a provider proposes another
        (and a hand-typed one is never replaced by one)."""
        with write_transaction(self._comp.engine) as connection:
            key = self._key(connection, edition_id, name, lang, on)
            if not unlock_field(connection, key):
                raise InvalidChoiceError("nothing is stored for that field")
        return {"edition_id": edition_id, "field": name, "lang": lang, "on": on,
                "locked": False, "kept": True}  # fmt: skip

    def renumber(
        self, edition_id: str, provider: str, old: str, new: str, *, id_scheme: str | None = None
    ) -> dict[str, Any]:  # fmt: skip
        """A provider renumbered `old` into `new` for this Edition."""
        try:
            before = normalize_external_id("edition", provider, old, id_scheme=id_scheme)
            after = normalize_external_id("edition", provider, new, id_scheme=id_scheme)
        except ExternalIdError as error:
            raise InvalidChoiceError(str(error)) from None
        with write_transaction(self._comp.engine) as connection:
            edition, _work = self._edition(connection, edition_id)
            if holder_of(connection, before) != edition:  # never invented, never another's
                raise InvalidChoiceError("the old identifier is not one of this Edition's")
            try:
                renumber_external_id(connection, edition, before, after.value, origin="provider")
            except ExternalIdConflictError:
                raise InvalidChoiceError("the new identifier names another Edition") from None
        return {"edition_id": edition, "provider": before.provider, "old": before.value,
                "new": after.value, "added": True}  # fmt: skip

    def listing(
        self, *, sort: str = "title", limit: int = 200, offset: int = 0
    ) -> list[dict[str, Any]]:
        """The Editions by the sort key of their Work's title in the Edition's language."""
        if sort not in SORTS:
            raise InvalidChoiceError("the list is sorted by title")
        if not 1 <= limit <= MAX_LIMIT or offset < 0:
            raise InvalidChoiceError(f"a page is 1 to {MAX_LIMIT} Editions")
        ed, work = catalog.edition, catalog.work
        with self._comp.engine.connect() as connection:
            rows = connection.execute(
                sa.select(ed.c.id, ed.c.work_id, ed.c.lang, work.c.title).select_from(
                    ed.join(work, work.c.id == ed.c.work_id)
                )
            ).all()
        keyed = sorted(
            ((sort_key(str(r.title), str(r.lang)), str(r.id), r) for r in rows),
            key=lambda found: (found[0], found[1]),
        )
        return [
            {"edition_id": str(r.id), "work_id": str(r.work_id), "lang": str(r.lang),
             "title": " ".join(str(r.title).split()), "sort_key": key}
            for key, _id, r in keyed[offset : offset + limit]
        ]  # fmt: skip
