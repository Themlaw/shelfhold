"""Who owns a field of a Kavita series (EF-EXP-057, passe 13 N3).

Kavita 0.9.1 and Kavita+ may rename a series, write titles, external ids and covers, and derive an
age from the tags. Shelfhold rewrites a field only if **it** locked it, that is, if the field is
in its `field_owner` registry; a locked field the registry does not know belongs to a third party
and is never touched. If Kavita shows another value for a field of the registry, that is drift:
`reconcile.drift` is emitted and the value is pushed again, instead of a silent abstention.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from shelfhold.export.domain.kavita_access import age_of_rating


class FieldAction(StrEnum):
    WRITE = "write"  # nobody holds the field (or Shelfhold's own value is current): push it
    LEAVE = "leave"  # a third party locked it: never rewritten
    NOTHING = "nothing"  # Shelfhold's lock, same value as the last push
    REPUSH = "repush"  # Shelfhold's lock, other value in Kavita: drift, push again
    PUSH_AGAIN = "push_again"  # the registry entry was invalidated (reinstall): push again


@dataclass(frozen=True)
class OwnedField:
    """A row of the `field_owner` registry; `pushed_at` None = invalidated."""

    last_value: Any
    pushed: bool


AGE_RATING = "age_rating"  # the one field a reinstall makes Shelfhold push again (N3)


def decide(
    *,
    field: str,
    locked_in_kavita: bool,
    kavita_value: Any,
    owned: OwnedField | None,
) -> FieldAction:
    """A lock Shelfhold does not hold is looked at first: it is a third party's."""
    if owned is not None and not owned.pushed:
        if field != AGE_RATING:
            owned = None  # an invalidated entry of another field is no claim at all
        else:
            return FieldAction.LEAVE if locked_in_kavita else FieldAction.PUSH_AGAIN
    if owned is None:
        return FieldAction.LEAVE if locked_in_kavita else FieldAction.WRITE
    if locked_in_kavita and kavita_value == owned.last_value:
        return FieldAction.NOTHING
    return FieldAction.REPUSH


def is_drift(action: FieldAction) -> bool:
    return action is FieldAction.REPUSH


def effective_age(shelfhold_age: int | None, tag_rating: int | None) -> int | None:
    """The class to keep for a series: Kavita's tag-derived age is a complement, never a source.

    It can only raise what Shelfhold classed (a tag that says "adults only" on a 15+ series is
    heeded), never lower it. `tag_rating` is a Kavita `AgeRating`; "unknown", "pending" and "not
    applicable" say nothing. Without a class of its own, the tag-derived age is all there is.
    """
    from_tags = None if tag_rating is None else age_of_rating(tag_rating)
    known = [a for a in (shelfhold_age, from_tags) if a is not None]
    return max(known) if known else None
