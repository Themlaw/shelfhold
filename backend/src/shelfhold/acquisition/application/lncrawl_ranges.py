"""The host's own look at what a range service answers (EF-SRC-034, EF-SRC-032, EF-BIB-051).

A two-step request asks first and receives later, and the second step is the instance's word. The
adapter checks it; the host checks it again, from what it asked, before a byte comes in: the
answer covers Units that were asked, in order, once each, at most `MAX_COVERED`; the file is on the
origin of the service and nowhere else; A file that is not an
EPUB container is never kept, whatever the answer called it. A ticket that has waited longer than
`MAX_TICKET_AGE` is let go, so a queue that never ends costs a bounded number of tries.

Nothing here names a tool: it is the same guard for any service that answers a range.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from shelfhold.library.public import Received, sniff_kind
from shelfhold.sdk.refs import RangedPayload
from shelfhold.sdk.source import SourceChanged

if TYPE_CHECKING:
    from shelfhold.acquisition.application.ranges import Keeper, RangeUnit

__all__ = [
    "MAX_COVERED",
    "MAX_TICKET_AGE",
    "RangeAnswerRefused",
    "epub_only",
    "ticket_expired",
    "verify_range_answer",
]

MAX_COVERED: Final = 5_000  # Units of one file
MAX_TICKET_AGE: Final = timedelta(hours=12)  # a job still queued after this is let go


class RangeAnswerRefused(SourceChanged):
    """The answer is not the one the request announced: a change of the source, never a state."""


def ticket_expired(now: datetime, since: datetime) -> bool:
    return now - since > MAX_TICKET_AGE


def _origin(url: str) -> str | None:
    """`scheme://host:port` with the default port spelled out, or None for anything that is not a
    plain http(s) address without credentials."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    default = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{parts.hostname}:{port or default}"


def verify_range_answer(
    units: Sequence[RangeUnit], payload: RangedPayload, *, origin: str | None
) -> None:
    """Raise `RangeAnswerRefused` unless the answer is for what was asked, on the host asked."""
    asked = {u.info.remote_unit_id: u.info.source_order for u in units}
    covers = payload.covers
    if not covers or len(covers) > MAX_COVERED:
        raise RangeAnswerRefused("the answer does not cover a range")
    previous = -1
    for remote in covers:
        position = asked.get(remote)
        if position is None:
            raise RangeAnswerRefused("the answer covers a Unit that was not asked for")
        if position <= previous:  # twice, or out of the order of the source
            raise RangeAnswerRefused("the answer covers a Unit twice or out of order")
        previous = position
    if origin is not None:
        wanted = _origin(origin)
        if wanted is None or _origin(payload.asset.request.url) != wanted:
            raise RangeAnswerRefused("the file is not on the origin of the service")


def epub_only(keep: Keeper) -> Keeper:
    """A keeper that keeps an EPUB container and nothing else (the signature, not the name)."""

    def kept(received: Received, payload: RangedPayload, item_id: str) -> None:
        if sniff_kind(received.path) != ".epub":
            raise RangeAnswerRefused("the file is not an EPUB container")
        keep(received, payload, item_id)

    return kept
