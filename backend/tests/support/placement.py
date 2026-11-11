"""The rules of placement (I3, I4) as plain functions: the reference the tests compare the SQL
decisions with. Production decides in `decide_new_variant` and in the ingest statements."""

from collections.abc import Iterable

from shelfhold.catalog.domain.placement import VariantFacts


def place_owner(variants: Iterable[VariantFacts]) -> str | None:
    """I4: the oldest Variant keeps the place (live ones first)."""
    pool = list(variants)
    live = [v for v in pool if not v.removed]
    chosen = live or pool
    return min(chosen, key=lambda v: v.age).id if chosen else None


def choose_preferred(variants: Iterable[VariantFacts]) -> str | None:
    """The one Variant exported for a Unit (I3): Team priority, then the oldest live one.

    The newcomer of an open numbering conflict is never chosen: nothing is exported for it
    without the admin's decision (EF-BIB-050). The older Variant keeps its place (EF-BIB-034).
    """
    live = [v for v in variants if not v.removed and not v.in_conflict]
    if not live:
        return None
    return min(live, key=lambda v: (v.rank is None, v.rank or 0, *v.age)).id
