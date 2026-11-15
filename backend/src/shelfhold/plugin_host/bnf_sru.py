"""The reader of the BnF's SRU answers (EF-BIB-013): the host's own, handed to the provider.

An SRU answer is XML from the network. The host has one door for XML, `safe_parse_xml`
(ENF-SEC-014): no DTD, no entity, no external reference, and caps on size, depth and elements. A
native provider may not import it, so the host builds the provider with this reader.

What it returns is plain data, one dict per notice, nothing interpreted: the provider decides
what may be proposed.
"""

from __future__ import annotations

from typing import Any

from shelfhold.infrastructure.xml import Element, UnsafeXmlError, XmlLimits, safe_parse_xml

__all__ = ["read_sru"]

_SRW = "{http://www.loc.gov/zing/srw/}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_XSI_TYPE = "{http://www.w3.org/2001/XMLSchema-instance}type"
_LIMITS = XmlLimits(max_bytes=2 * 1024 * 1024, max_depth=16, max_elements=20_000)
_MAX_NOTICES = 50  # a question asks for a handful: the rest is not read


def _texts(notice: Element, name: str) -> list[str]:
    found = (" ".join("".join(e.itertext()).split()) for e in notice.iter(_DC + name))
    return [text for text in found if text]


def read_sru(body: bytes) -> list[dict[str, Any]]:
    """The notices of a `searchRetrieveResponse`; `ValueError` when it is not one, or not safe."""
    try:
        root = safe_parse_xml(body.lstrip(), limits=_LIMITS)
    except UnsafeXmlError as error:
        raise ValueError(f"not a safe SRU answer ({error.reason})") from error
    if root.tag != _SRW + "searchRetrieveResponse":
        raise ValueError("not an SRU answer")
    notices: list[dict[str, Any]] = []
    for record in root.iter(_SRW + "record"):
        if len(notices) >= _MAX_NOTICES:
            break
        titles = _texts(record, "title")
        publishers, dates = _texts(record, "publisher"), _texts(record, "date")
        notices.append(
            {
                "identifiers": _texts(record, "identifier"),
                "title": titles[0] if titles else None,
                "creators": _texts(record, "creator"),
                "publisher": publishers[0] if publishers else None,
                "date": dates[0] if dates else None,
                "subjects": [
                    {
                        "text": " ".join("".join(e.itertext()).split()),
                        "scheme": e.get(_XSI_TYPE, ""),
                    }
                    for e in record.iter(_DC + "subject")
                    if "".join(e.itertext()).strip()
                ],
                "types": _texts(record, "type"),
            }
        )
    return notices
