"""safe_parse_xml: the single XML entry point (ENF-SEC-014).

Everything that reads XML from outside (OPF, ComicInfo, OPDS, RDF, SVG) comes here. The parser is
`defusedxml` with every DTD refused, so external entities (file and network), parameter entities,
internal entity expansion (billion laughs, quadratic blowup) and external DTDs cannot be reached:
a document that declares one is rejected before any expansion. On top of that the input size, the
nesting depth and the element count are capped while parsing, so the work stays proportional to
the cap and not to what the document claims.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Final
from xml.etree.ElementTree import Element, ParseError

import defusedxml
from defusedxml import ElementTree as DefusedET


class UnsafeXmlError(ValueError):
    """The document is refused. `reason` is a stable machine-readable word."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason


@dataclass(frozen=True, slots=True)
class XmlLimits:
    max_bytes: int = 8 * 1024 * 1024
    max_depth: int = 64
    max_elements: int = 500_000


DEFAULT_LIMITS: Final = XmlLimits()

_REASONS: Final[dict[type[Exception], str]] = {
    defusedxml.DTDForbidden: "dtd",
    defusedxml.EntitiesForbidden: "entity",
    defusedxml.ExternalReferenceForbidden: "external",
    defusedxml.NotSupportedError: "unsupported",
}


def safe_parse_xml(data: bytes, *, limits: XmlLimits = DEFAULT_LIMITS) -> Element:
    """Parse `data` and return the root element, or raise UnsafeXmlError.

    Only bytes are accepted: the encoding declaration is the parser's business, and a str would
    let a caller decode it wrongly first.
    """
    if not isinstance(data, bytes | bytearray | memoryview):
        raise TypeError("safe_parse_xml expects bytes")
    if len(data) > limits.max_bytes:
        raise UnsafeXmlError("too_large", f"{len(data)} bytes, limit {limits.max_bytes}")
    root: Element | None = None
    depth = 0
    count = 0
    try:
        for event, element in DefusedET.iterparse(
            BytesIO(data), events=("start", "end"), forbid_dtd=True
        ):
            if event == "start":
                if root is None:
                    root = element
                depth += 1
                count += 1
                if depth > limits.max_depth:
                    raise UnsafeXmlError("too_deep", f"nesting beyond {limits.max_depth}")
                if count > limits.max_elements:
                    raise UnsafeXmlError("too_many_elements", f"beyond {limits.max_elements}")
            else:
                depth -= 1
    except defusedxml.DefusedXmlException as error:
        reason = next((r for t, r in _REASONS.items() if isinstance(error, t)), "external")
        raise UnsafeXmlError(reason, type(error).__name__) from None
    except ParseError as error:
        raise UnsafeXmlError("malformed", str(error)) from None
    if root is None:
        raise UnsafeXmlError("malformed", "no root element")
    return root
