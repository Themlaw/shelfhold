"""Contributors met while ingesting a series (EF-BIB-046).

A source spells its authors as it likes: "Doe, Jane" (authority form), "Jane Doe", several names
in one field, an organisation "Éditeur, Ltd.". They are stored as spelled (after the caller made
the text safe), one `contributor` per person, credited on the Work with a MARC relator role. The
export decides how a name reads in a person field (`catalog.domain.names`); here we only decide
who is who. Nothing is overwritten: a known name is credited again at most once.
"""

import re
from dataclasses import dataclass
from typing import Protocol

__all__ = ["ARTIST", "WRITER", "ContributorStore", "Credit", "credits_of", "record_credits"]

WRITER = "aut"
ARTIST = "art"

# A legal form or a collective noun: such a name is an organisation, whose comma is not an
# authority-form inversion ("Éditeur, Ltd." is not "Ltd. Éditeur").
_ORG = re.compile(
    r"\b(ltd|inc|llc|gmbh|co|corp|corporation|company|sarl|sas|sa|kk|"
    r"studio|studios|circle|press|publishing|publishers?|productions?|editions?|éditions?|team|"
    r"group|committee)\b\.?",
    re.IGNORECASE,
)
_MAX_NAMES = 20  # a source field is not a library
_AUTHORITY = re.compile(r"^[^,]+,[^,]+$")


@dataclass(frozen=True)
class Credit:
    name: str
    kind: str  # person | org
    family_first: bool  # the name is in authority form, "Family, Given"


class ContributorStore(Protocol):
    def credited(self, work_id: str) -> bool: ...
    def find(self, name: str, kind: str) -> str | None: ...
    def insert(self, name: str, kind: str, family_first: bool) -> str: ...
    def credit(self, work_id: str, contributor_id: str, role: str, position: int) -> bool: ...
    def next_position(self, work_id: str) -> int: ...


def _collapse(text: str) -> str:
    return " ".join(text.split())


def credits_of(raw_names: list[str]) -> list[Credit]:
    """The people and organisations a source field names, in order and without repeats.

    A field with one comma is an authority form ("Doe, Jane") unless it is an organisation; two
    commas or more list several people ("A, B, C"), which Suwayomi does in a single field.
    """
    found: dict[str, Credit] = {}
    for raw in raw_names:
        text = _collapse(raw)
        if not text:
            continue
        if _ORG.search(text):
            candidates = [Credit(text, "org", False)]
        elif _AUTHORITY.match(text):
            candidates = [Credit(text, "person", True)]
        else:
            candidates = [Credit(p, "person", False) for p in map(_collapse, text.split(",")) if p]
        for credit in candidates:
            found.setdefault(credit.name.casefold(), credit)
    return list(found.values())[:_MAX_NAMES]


def record_credits(store: ContributorStore, work_id: str, role: str, raw_names: list[str]) -> int:
    """Credit the Work with these names; returns the number of new credits."""
    added = 0
    position = store.next_position(work_id)
    for credit in credits_of(raw_names):
        contributor_id = store.find(credit.name, credit.kind) or store.insert(
            credit.name, credit.kind, credit.family_first
        )
        if store.credit(work_id, contributor_id, role, position):
            added += 1
            position += 1
    return added
