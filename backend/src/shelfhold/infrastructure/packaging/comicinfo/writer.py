"""The ComicInfo.xml writer (EF-EXP-011, EF-BIB-046 I21, ENF-SEC-015, EF-EXP-063).

Pure and deterministic: the same data gives the same bytes. The element order is the pinned XSD's
(`xs:sequence`), with the two elements Kavita reads outside the schema, `LocalizedSeries` and
`SeriesSort`, right after `Series`. Empty values are omitted.

What it never writes: the Shelfhold identity (no `Notes`, `ScanInformation`, `Tags` or `GTIN`;
`Web` only when the caller gives one, empty by default), an inverted person name ("Family, Given"
becomes "Given Family"; any other comma is replaced and reported), a sort name in a person field,
and any untrusted markup (the summary goes through `sanitize_upstream_text`, then XML escaping).
`Translator` is the Team label, a public metadata. Commas inside a value of a person-like field
(`Translator`, `Publisher`, one genre, every person) are replaced and reported (I21).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from shelfhold.catalog.public import (
    ContributorName,
    NoPersonFieldName,
    join_people,
    person_field_name,
)
from shelfhold.infrastructure.content_trust import Destination, sanitize_upstream_text
from shelfhold.sdk.age import AgeRating, to_comicinfo

PERSON_ROLES: Final = (
    "Writer", "Penciller", "Inker", "Colorist", "Letterer", "CoverArtist", "Editor",
)  # fmt: skip
ELEMENT_ORDER: Final = (
    "Title", "Series", "LocalizedSeries", "SeriesSort", "Number", "Count", "Volume",
    "Summary", "Year", "Month", "Day", *PERSON_ROLES, "Translator", "Publisher", "Genre", "Web",
    "PageCount", "LanguageISO", "Format", "Manga", "AgeRating",
)  # fmt: skip
_INVALID_XML: Final = re.compile("[^\t\n\r\x20-퟿-�\U00010000-\U0010ffff]")
SUMMARY_MAX: Final = 4000


@dataclass(frozen=True)
class ComicInfoPerson:
    """A contributor with all its known names; the writer picks the display form."""

    names: tuple[ContributorName, ...]
    is_org: bool = False


@dataclass(frozen=True)
class ComicInfoData:
    series: str
    title: str = ""  # chapter title: here and nowhere else (never in a file name)
    number: str = ""
    count: int | None = None
    volume: int | None = None  # a tome is written here, not in the name of a chapter
    series_sort: str = ""  # Kavita `SeriesSort`
    localized_series: str = ""
    summary: str = ""  # raw upstream text
    summary_html: bool = False
    year: int | None = None
    month: int | None = None
    day: int | None = None
    people: Mapping[str, Sequence[ComicInfoPerson]] = field(default_factory=dict)
    team_label: str = ""  # Translator
    publisher: str = ""
    genres: Sequence[str] = ()
    web: str = ""
    language_iso: str = ""
    format: str = ""
    right_to_left: bool | None = None
    age: AgeRating | None = None
    page_count: int | None = None


@dataclass(frozen=True)
class RenderedComicInfo:
    xml: bytes
    warnings: tuple[str, ...]


def _xml_text(text: str) -> str:
    cleaned = _INVALID_XML.sub("", text)
    return cleaned.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _no_comma(text: str, field_name: str, warnings: list[str]) -> str:
    """Kavita splits person-like fields at commas (I21): a comma inside one value is replaced."""
    if "," not in text:
        return text
    warnings.append(f"{field_name}: comma replaced")
    return " ".join(text.replace(",", " ").split())


def _people(
    persons: Sequence[ComicInfoPerson], role: str, lang: str | None, warnings: list[str]
) -> str:
    names: list[str] = []
    for person in persons:
        try:
            shown = person_field_name(person.names, lang, is_org=person.is_org)
        except NoPersonFieldName as error:
            raise ValueError(f"{role}: only a sort name is known ({error})") from None
        if shown.commas_replaced:
            warnings.append(f"{role}: comma replaced in a person name")
        safe = sanitize_upstream_text(shown.text)  # whatever the database holds (ENF-SEC-015)
        if safe:
            names.append(_no_comma(safe, role, warnings))
    return join_people(names)


def render_comicinfo(data: ComicInfoData, *, lang: str | None = None) -> RenderedComicInfo:
    warnings: list[str] = []
    unknown = set(data.people) - set(PERSON_ROLES)
    if unknown:
        raise ValueError(f"not a person field: {sorted(unknown)}")
    summary = ""
    if data.summary:
        summary = sanitize_upstream_text(
            data.summary,
            destination=Destination.HTML if data.summary_html else Destination.PLAIN,
            max_length=SUMMARY_MAX,
        )

    def number(value: int | None) -> str:
        return "" if value is None else str(value)

    values: dict[str, str] = {
        "Title": sanitize_upstream_text(data.title),
        "Series": data.series,
        "LocalizedSeries": data.localized_series,
        "SeriesSort": data.series_sort,
        "Number": data.number,
        "Count": number(data.count),
        "Volume": number(data.volume),
        "Summary": summary,
        "Year": number(data.year),
        "Month": number(data.month),
        "Day": number(data.day),
        "Translator": _no_comma(sanitize_upstream_text(data.team_label), "Translator", warnings),
        "Publisher": _no_comma(data.publisher, "Publisher", warnings),
        "Genre": ", ".join(_no_comma(g, "Genre", warnings) for g in data.genres),
        "Web": data.web,
        "PageCount": number(data.page_count),
        "LanguageISO": data.language_iso,
        "Format": data.format,
        "Manga": {None: "", True: "YesAndRightToLeft", False: "No"}[data.right_to_left],
        "AgeRating": to_comicinfo(data.age) or "",
    }
    for role in PERSON_ROLES:
        values[role] = _people(data.people.get(role, ()), role, lang, warnings)
    lines = ['<?xml version="1.0" encoding="utf-8"?>', "<ComicInfo>"]
    for element in ELEMENT_ORDER:
        text = _xml_text(values[element]).strip()
        if text:
            lines.append(f"  <{element}>{text}</{element}>")
    lines.append("</ComicInfo>")
    return RenderedComicInfo(("\n".join(lines) + "\n").encode("utf-8"), tuple(warnings))
