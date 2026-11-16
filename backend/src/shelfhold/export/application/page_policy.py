"""Which page formats an archive may leave in, and what the others become (EF-EXP-046, ADR-0054).

By default JPEG, PNG, GIF and WebP stay and any other page is re-encoded to the configured target
(JPEG, quality 90). A destination that declares `accepted_image_types` narrows the set it keeps: a
page in a kind it does not take is converted too. The target is always a kind the destination takes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from shelfhold.infrastructure.content_trust import KEEP_PAGE_KINDS
from shelfhold.infrastructure.packaging.cbz import PageTarget
from shelfhold.infrastructure.packaging.cbz.normalize import TARGET_FORMATS

__all__ = ["PagePolicy", "page_policy_resolver"]


@dataclass(frozen=True, slots=True)
class PagePolicy:
    target: PageTarget = field(default_factory=PageTarget)
    keep: frozenset[str] = KEEP_PAGE_KINDS

    @classmethod
    def for_destination(
        cls, accepted_image_types: Iterable[str] | None, target: PageTarget | None = None
    ) -> PagePolicy:
        target = target or PageTarget()
        accepted = {str(kind).lower().replace("jpg", "jpeg") for kind in accepted_image_types or ()}
        keep = KEEP_PAGE_KINDS & accepted if accepted else KEEP_PAGE_KINDS
        if not keep:
            keep = frozenset({target.format})
        if target.format not in keep:
            first = next((f for f in TARGET_FORMATS if f in keep), target.format)
            target = PageTarget(first, target.quality)
        return cls(target, frozenset(keep))


def page_policy_resolver(
    capabilities: Callable[[str], Any], target: PageTarget
) -> Callable[[str], PagePolicy | None]:
    """`target_id -> PagePolicy` from the destination's declared `accepted_image_types`; None for a
    destination the registry does not know (the renderer then uses its default policy)."""

    def resolve(target_id: str) -> PagePolicy | None:
        declared = capabilities(target_id)
        if declared is None:
            return None
        return PagePolicy.for_destination(declared.accepted_image_types, target)

    return resolve
