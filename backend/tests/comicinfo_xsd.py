"""Pinned ComicInfo schemas (tests/fixtures/comicinfo/SOURCES.md), offline."""

import re
from functools import cache
from pathlib import Path

import xmlschema

PINNED = Path(__file__).parent / "fixtures" / "comicinfo"
KAVITA_ELEMENTS = ("LocalizedSeries", "SeriesSort")
_SERIES = '<xs:element minOccurs="0" maxOccurs="1" default="" name="Series" type="xs:string"/>'


@cache
def schema(version: str) -> xmlschema.XMLSchema:
    """`v2.0` or `v2.1-draft`, exactly as pinned."""
    return xmlschema.XMLSchema((PINNED / version / "ComicInfo.xsd").read_text("utf-8"))


@cache
def kavita_schema() -> xmlschema.XMLSchema:
    """The 2.1 draft plus the two elements Kavita reads, inserted after `Series` (in memory)."""
    text = (PINNED / "v2.1-draft" / "ComicInfo.xsd").read_text("utf-8")
    assert _SERIES in text
    extra = "".join(
        f'\n            <xs:element minOccurs="0" maxOccurs="1" default="" name="{n}" type="xs:string"/>'
        for n in KAVITA_ELEMENTS
    )
    return xmlschema.XMLSchema(text.replace(_SERIES, _SERIES + extra))


def strip_kavita_elements(xml: str) -> str:
    return re.sub(rf"\s*<({'|'.join(KAVITA_ELEMENTS)})>.*?</\1>", "", xml, flags=re.S)
