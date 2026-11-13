"""The one state the family reads (EF-UX-010): En préparation, Disponible dans Kavita, En attente.

A request and a download have their own steps (`pending`, `approved`, `requested`, `downloaded`...);
the family is told one of three words, and the screen only maps the word to a text. The steps
stay the admin's. A state that the family has no word for (a refusal, a cancellation) is None:
the request says why in its own fields.
"""

from typing import Final, Literal

__all__ = ["FamilyState", "download_family_state", "event_family_state", "request_family_state"]

FamilyState = Literal["preparing", "available", "waiting"]

_REQUEST: Final[dict[str, FamilyState]] = {
    "pending": "waiting",  # nobody has decided yet
    "approved": "preparing",
    "arrived": "available",
}
_DOWNLOAD: Final[dict[str, FamilyState]] = {
    "requested": "preparing",
    "downloaded": "preparing",
    "fetched": "available",
    "abandoned": "waiting",  # the admin has to look at it
}
_EVENT: Final[dict[str, FamilyState]] = {"ops.request_arrived": "available"}


def request_family_state(state: str) -> FamilyState | None:
    return _REQUEST.get(state)


def download_family_state(state: str) -> FamilyState | None:
    return _DOWNLOAD.get(state)


def event_family_state(event_type: str) -> FamilyState | None:
    return _EVENT.get(event_type)
