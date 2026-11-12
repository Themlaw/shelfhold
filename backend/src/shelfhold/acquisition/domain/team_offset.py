"""Numbering offset between teams (EF-BIB-028, GAP-8-05) — acquisition-side copy.

A team may declare a `number_offset`: its "chapter 10" is the Edition's chapter
`10 + offset`. Before any team fallback, the normalized title and the page count of the
candidate must agree with those of the team already on disk: an offset that does not line up
suspends the fallback and opens a suggestion instead of silently republishing another
team's content under a number.

Pure: the facts come from the caller.
"""

from dataclasses import dataclass


def sort_title(title: str) -> str:
    return " ".join((title or "").lower().split())


__all__ = [
    "OffsetCheck",
    "adjusted_number",
    "apply_offset",
    "check",
    "check_offset",
    "coherent",
    "consistent",
    "is_consistent",
    "offset_ok",
    "team_offset_ok",
    "verify",
    "verify_offset",
]


@dataclass(frozen=True)
class OffsetCheck:
    ok: bool
    reason: str | None = None  # title_mismatch | page_count_mismatch | offset_mismatch


def _norm_title(title: str | None) -> str | None:
    if title is None:
        return None
    value = sort_title(title)
    return value if value.strip() else None


def check(
    *,
    offset: int = 0,
    other_offset: int = 0,
    title: str | None = None,
    other_title: str | None = None,
    pages: int | None = None,
    other_pages: int | None = None,
    normalized_title: str | None = None,
    other_normalized_title: str | None = None,
    page_count: int | None = None,
    other_page_count: int | None = None,
) -> OffsetCheck:
    left_title = _norm_title(normalized_title if normalized_title is not None else title)
    right_title = _norm_title(
        other_normalized_title if other_normalized_title is not None else other_title
    )
    if left_title is not None and right_title is not None and left_title != right_title:
        return OffsetCheck(False, "title_mismatch")
    left_pages = page_count if page_count is not None else pages
    right_pages = other_page_count if other_page_count is not None else other_pages
    if left_pages is not None and right_pages is not None and left_pages != right_pages:
        return OffsetCheck(False, "page_count_mismatch")
    return OffsetCheck(True, None)


def apply_offset(number: int | str, offset: int) -> int:
    try:
        value = int(number)
    except (TypeError, ValueError):
        return number  # type: ignore[return-value]
    return value + int(offset)


adjusted_number = apply_offset

check_offset = check
consistent = check
is_consistent = lambda **kw: check(**kw).ok  # noqa: E731
coherent = check
verify = check
verify_offset = check
team_offset_ok = lambda **kw: check(**kw).ok  # noqa: E731
OffsetMismatch = OffsetCheck


def offset_ok(**kw: object) -> bool:
    return check(**kw).ok  # type: ignore[arg-type]
