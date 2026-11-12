"""The audit an SVG passes before the renderer sees it (EF-BIB-051 (6), ENF-SEC-014).

Runs inside the decode worker only. Closed by default: the document is drawn only when it is a plain
SVG, parsed by the hardened XML entry point (no DTD, so no entity of any kind), with

* the SVG namespace on every element and nothing else (a polyglot with XHTML is refused);
* no script, no foreignObject, no iframe, object or embed, no animation of a link, no SVG font;
* no event handler attribute;
* no reference that leaves the document: every `href` or `xlink:href` is `#fragment`, which also
  excludes `file:`, `http:`, `data:` (nested images included) and relative paths; no `url(...)`
  that is not `url(#fragment)`; no `@import` or `@font-face` in a style;
* a size the caps allow, read from `width`/`height` or the `viewBox`, a node count and a depth.

The renderer is then given no resource directory and no font, so it has nothing to read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from shelfhold.infrastructure.xml import Element, UnsafeXmlError, XmlLimits, safe_parse_xml

SVG_NS: Final = "http://www.w3.org/2000/svg"
XLINK_NS: Final = "http://www.w3.org/1999/xlink"
DEFAULT_MAX_BYTES: Final = 8 * 1024 * 1024
DEFAULT_MAX_NODES: Final = 20_000
DEFAULT_MAX_DEPTH: Final = 32

_FORBIDDEN_TAGS: Final = frozenset(
    {"script", "foreignobject", "iframe", "object", "embed", "font", "font-face", "font-face-src",
     "font-face-uri", "font-face-format", "font-face-name", "handler", "listener", "audio",
     "video", "canvas", "cursor"}
)  # fmt: skip
_ANIMATION_TAGS: Final = frozenset({"animate", "set", "animatetransform", "animatemotion"})
_URL_REF: Final = re.compile(r"url\(\s*(['\"]?)\s*([^)'\"]*)", re.IGNORECASE)
_AT_RULE: Final = re.compile(r"@(import|font-face|namespace|charset)", re.IGNORECASE)
_LENGTH: Final = re.compile(r"^\s*([0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)\s*(px)?\s*$")


class SvgRefusedError(ValueError):
    """The SVG is refused. `reason` is a stable word; `kind` is `refused` or `too_large`."""

    def __init__(self, reason: str, kind: str = "refused") -> None:
        super().__init__(reason)
        self.reason = reason
        self.kind = kind


@dataclass(frozen=True, slots=True)
class SvgFacts:
    width: int
    height: int
    nodes: int


def _local(tag: object) -> tuple[str, str]:
    if not isinstance(tag, str):
        raise SvgRefusedError("svg_odd_node")
    if tag.startswith("{"):
        namespace, _, name = tag[1:].partition("}")
        return namespace, name
    return "", tag


def _check_style(text: str) -> None:
    if _AT_RULE.search(text):
        raise SvgRefusedError("svg_css_at_rule")
    for match in _URL_REF.finditer(text):
        if not match.group(2).strip().startswith("#"):
            raise SvgRefusedError("svg_external_reference")
    if "expression(" in text.lower() or "javascript:" in text.lower():
        raise SvgRefusedError("svg_css_script")


def _check_attribute(key: str, value: str) -> None:
    namespace, name = _local(key)
    lowered = name.lower()
    if namespace not in ("", XLINK_NS):
        if namespace == "http://www.w3.org/XML/1998/namespace":
            return
        raise SvgRefusedError("svg_foreign_attribute")
    if lowered.startswith("on"):
        raise SvgRefusedError("svg_event_handler")
    if lowered == "href" and not value.startswith("#"):
        raise SvgRefusedError("svg_external_reference")
    if lowered == "style" or "url(" in value.lower():
        _check_style(value)
    if lowered in {"src", "data", "action", "formaction", "background"}:
        raise SvgRefusedError("svg_external_reference")


def _length(value: str | None) -> float | None:
    if value is None:
        return None
    match = _LENGTH.match(value)
    if match is None:
        raise SvgRefusedError("svg_unsupported_length")
    number = float(match.group(1))
    if not number == number or number in (float("inf"),):
        raise SvgRefusedError("svg_unsupported_length")
    return number


def _size(root: Element, max_pixels: int) -> tuple[int, int]:
    width, height = _length(root.get("width")), _length(root.get("height"))
    if width is None or height is None:
        box = root.get("viewBox")
        parts = box.replace(",", " ").split() if box else []
        if len(parts) != 4:
            raise SvgRefusedError("svg_no_size")
        try:
            vb_w, vb_h = float(parts[2]), float(parts[3])
        except ValueError:
            raise SvgRefusedError("svg_no_size") from None
        if width is None and height is None:
            width, height = vb_w, vb_h
        elif width is None:
            width = (height or 0) * vb_w / vb_h if vb_h > 0 else None
        else:
            height = width * vb_h / vb_w if vb_w > 0 else None
    if width is None or height is None or not (width > 0 and height > 0):
        raise SvgRefusedError("svg_no_size")
    if width * height > max_pixels or width > 1e6 or height > 1e6:
        raise SvgRefusedError("svg_too_many_pixels", "too_large")
    return max(1, round(width)), max(1, round(height))


def audit_svg(
    data: bytes,
    *,
    max_pixels: int,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> SvgFacts:
    """Facts about a plain SVG, or `SvgRefusedError`: nothing is drawn from a doubtful document."""
    if len(data) > max_bytes:
        raise SvgRefusedError("svg_too_large_bytes", "too_large")
    stripped = data.lstrip(b"\xef\xbb\xbf \t\r\n")
    if not stripped.startswith(b"<"):  # a polyglot with a binary magic in front is not an SVG
        raise SvgRefusedError("svg_not_xml")
    try:
        root = safe_parse_xml(
            data,
            limits=XmlLimits(max_bytes=max_bytes, max_depth=max_depth, max_elements=max_nodes),
        )
    except UnsafeXmlError as error:
        if error.reason in {"too_deep", "too_many_elements", "too_large"}:
            raise SvgRefusedError(f"svg_{error.reason}", "too_large") from None
        raise SvgRefusedError(f"svg_xml_{error.reason}") from None
    if _local(root.tag) != (SVG_NS, "svg"):
        raise SvgRefusedError("svg_not_svg")
    nodes = 0
    for element in root.iter():
        nodes += 1
        namespace, name = _local(element.tag)
        lowered = name.lower()
        if namespace != SVG_NS:
            raise SvgRefusedError("svg_foreign_element")
        if lowered in _FORBIDDEN_TAGS:
            raise SvgRefusedError(f"svg_{lowered}")
        if lowered in _ANIMATION_TAGS:
            target = (element.get("attributeName") or "").lower()
            if "href" in target:
                raise SvgRefusedError("svg_animated_reference")
        if lowered == "style":
            _check_style("".join(element.itertext()))
        for key, value in element.attrib.items():
            _check_attribute(key, value)
    width, height = _size(root, max_pixels)
    return SvgFacts(width, height, nodes)
