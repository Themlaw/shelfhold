"""A file dropped in the import folder, matched to a request that waits for it (EF-USR-019).

Only a request an admin has put aside as "to buy" or "to borrow elsewhere" is open to a match, and
only on a text or an ISBN the person typed. The title is compared folded (`fold_title`: NFKC, case,
accents; no ASCII reduction), the whole title and not a part of it: a wrong match tells somebody
a book arrived that did not, which is worse than one that waits for a second look. The ISBN is the
one the Work's own Editions carry, or one written in the title of the file. Pure reading: what a
match does to the request is the request service's.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.catalog.public import ExternalIdError, fold_title, normalize_external_id
from shelfhold.infrastructure.schema import identity
from shelfhold.infrastructure.schema.identity import REQUEST_ELSEWHERE_STATES

__all__ = ["isbns_in", "open_request_matches"]

# ten or thirteen digits (the last of ten may be an X), groups apart by a space or a hyphen
_CANDIDATE = re.compile(
    r"(?<![\w-])(?:\d[ -]?){9}[\dXx](?![\w-])|(?<![\w-])97[89](?:[ -]?\d){10}(?![\w-])"
)


def isbns_in(text: str) -> list[str]:
    """The valid ISBNs written in a text, as ISBN-13."""
    found: list[str] = []
    for match in _CANDIDATE.finditer(text):
        try:
            isbn = normalize_external_id("edition", "isbn", match.group(0)).value
        except ExternalIdError:
            continue
        if isbn not in found:
            found.append(isbn)
    return found


def open_request_matches(
    connection: Connection, *, title: str, isbns: Iterable[str] = ()
) -> list[str]:
    """The identifiers of the open "elsewhere" requests this title or one of these ISBNs answers,
    oldest first."""
    wanted = fold_title(title)
    held = {i for raw in isbns for i in _normalized(raw)} | set(isbns_in(title))
    request = identity.request
    rows = connection.execute(
        sa.select(request.c.id, request.c.target_kind, request.c.target_value)
        .where(
            request.c.state.in_(REQUEST_ELSEWHERE_STATES),
            request.c.target_kind.in_(("free_text", "isbn")),
        )
        .order_by(request.c.created_at, request.c.id)
    )
    return [
        row.id
        for row in rows
        if (row.target_kind == "isbn" and row.target_value in held)
        or (row.target_kind == "free_text" and wanted and fold_title(row.target_value) == wanted)
    ]


def _normalized(raw: str) -> list[str]:
    try:
        return [normalize_external_id("edition", "isbn", raw).value]
    except ExternalIdError:
        return []
