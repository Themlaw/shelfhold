"""Download filters of a followed series (EF-SRC-039), decided before any request.

Pure: the caller gives the facts of a Variant and gets the reason it is refused, or None. The
filters of a series are a JSON object of the `series_follow` row; one that cannot be read refuses
everything with the reason `invalid` (never a silent "no filter"): the Unit stays visible with
that reason and nothing is asked until the filters are fixed.

Keys (all optional): `news_only` (bool; the watermark of EF-EXP-053 does it), `languages` (BCP 47
tags, a tag accepts its sub-tags: `fr` accepts `fr-CA`), `teams_accept`, `teams_refuse` (team
labels), `exclude_extras` (bool), `cap` (Units asked as news per cycle; the surplus is asked as
catch-up, after every series' news).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

__all__ = [
    "DEFAULT_NEWS_CAP",
    "REASONS",
    "DownloadFilters",
    "UnitFacts",
    "parse_filters",
    "refusal",
]

DEFAULT_NEWS_CAP: Final = 50
REASONS: Final = ("language", "team", "extra", "invalid")
_KEYS: Final = frozenset(
    {"news_only", "languages", "teams_accept", "teams_refuse", "exclude_extras", "cap"}
)
_TAG: Final = re.compile(r"[a-z]{2,3}(-[a-z0-9]{1,8})*\Z")
_EXTRA_TRACKS: Final = frozenset({"extra", "special", "unnumbered"})


@dataclass(frozen=True)
class DownloadFilters:
    languages: tuple[str, ...] = ()  # empty: any language
    teams_accept: tuple[str, ...] = ()  # empty: any team
    teams_refuse: tuple[str, ...] = ()
    exclude_extras: bool = False
    cap: int | None = None
    invalid: bool = False

    @property
    def is_open(self) -> bool:
        return not (
            self.invalid or self.languages or self.teams_accept or self.teams_refuse
            or self.exclude_extras
        )  # fmt: skip


@dataclass(frozen=True)
class UnitFacts:
    """What the filters need to know of one offer of a Unit."""

    lang: str  # the Variant's language, else its Edition's; `und` when neither is known
    team: str | None  # the team label as the Edition knows it
    track: str  # the Unit's track
    segment: str  # the Unit's numbering segment: `''` is the main one


_INVALID = DownloadFilters(invalid=True)


def _texts(value: object, *, tags: bool = False) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        return None
    out = tuple(dict.fromkeys(v.strip().casefold() for v in value))
    if tags and not all(_TAG.fullmatch(v) for v in out):
        return None
    return out


def parse_filters(raw: object) -> DownloadFilters:
    """`None` (the series inherits) is open; what is not understood refuses everything."""
    if raw is None:
        return DownloadFilters()
    if not isinstance(raw, Mapping) or not set(raw) <= _KEYS:
        return _INVALID
    languages = _texts(raw.get("languages", []), tags=True)
    accept = _texts(raw.get("teams_accept", []))
    refuse = _texts(raw.get("teams_refuse", []))
    extras, news, cap = raw.get("exclude_extras", False), raw.get("news_only", True), raw.get("cap")
    if (
        languages is None or accept is None or refuse is None
        or not isinstance(extras, bool) or not isinstance(news, bool)
        or (cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 1))
    ):  # fmt: skip
        return _INVALID
    return DownloadFilters(languages, accept, refuse, extras, cap)


def _language_ok(accepted: tuple[str, ...], lang: str) -> bool:
    tag = lang.casefold()
    return any(tag == a or tag.startswith(a + "-") for a in accepted)


def refusal(filters: DownloadFilters, facts: UnitFacts) -> str | None:
    """The reason these filters refuse this offer, or None: language, then team, then extra."""
    if filters.invalid:
        return "invalid"
    if filters.languages and not _language_ok(filters.languages, facts.lang):
        return "language"  # an unknown language (`und`) is accepted by no language filter
    team = None if facts.team is None else facts.team.casefold()
    if team is not None and team in filters.teams_refuse:
        return "team"
    if filters.teams_accept and team not in filters.teams_accept:
        return "team"
    if filters.exclude_extras and (facts.track in _EXTRA_TRACKS or facts.segment != ""):
        return "extra"
    return None
