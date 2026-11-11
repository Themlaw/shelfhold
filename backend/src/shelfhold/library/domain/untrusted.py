"""The rules of the derivative of an `untrusted` EPUB, as plain functions (EF-BIB-051).

An `untrusted` EPUB is neutralized before anything leaves Shelfhold; the original is kept in
`/store/untrusted/` (passthrough mode keeps nothing else there) so that a security update of the
neutralizer can regenerate from it. Without a `/store`, only the derivative is kept and the
regeneration restarts from it. `derived_chain` lists the earlier generations, oldest first.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

UNTRUSTED: Final = "untrusted"
PUBLISHER: Final = "publisher"
EPUB_MEDIA_TYPE: Final = "application/epub+zip"
ORIGINAL_NOT_KEPT: Final = "content.original_not_kept"  # the warning of a binding without /store


def trusted_as_published(content_trust: str | None) -> bool:
    """Only the manifest's `publisher` skips the neutralizer; anything else, or nothing, is
    `untrusted`. A watermark found by a heuristic never changes this."""
    return content_trust == PUBLISHER


def binding_warning(content_trust: str | None, *, store_available: bool) -> str | None:
    """What to tell the admin when an `untrusted` source is bound and there is no `/store`: the
    originals are not kept, so a neutralizer fix can only restart from the derivative."""
    if trusted_as_published(content_trust) or store_available:
        return None
    return ORIGINAL_NOT_KEPT


def original_rel(unit_id: str, content_id: str) -> str:
    """Where the original of an `untrusted` EPUB is kept, relative to `/store`."""
    return f"untrusted/{unit_id[:2]}/{unit_id}/{content_id}.epub"


def chain_entry(
    derived_by: str, derived_hash: str, derived_hash_by: str | None, at: datetime
) -> dict[str, Any]:
    return {
        "derived_by": derived_by,
        "derived_hash": derived_hash,
        "derived_hash_by": derived_hash_by,
        "replaced_at": at.isoformat(),
    }


def extend_chain(chain: Sequence[Any] | None, entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The earlier generations, with the one being replaced added at the end."""
    return [*(dict(e) for e in chain or ()), entry]
