# SPDX-License-Identifier: MIT
"""An independent detector of active content in XHTML, SVG and CSS bytes.

It shares nothing with the neutralizer (no allowlist, no parser of the core): it reads the text the
way a hostile document would be read by a browser, decoding entities and ignoring the characters a
URL parser ignores. A neutralized document must give an empty list. Remote hyperlinks (`a href` to
http, https or mailto) are navigation and not resources, so they are not findings.
"""

from __future__ import annotations

import html
import re
from typing import Final

_FORBIDDEN_TAGS: Final = (
    "script", "iframe", "frame", "frameset", "object", "embed", "applet", "form", "input",
    "button", "select", "textarea", "base", "foreignobject", "animate", "animatemotion",
    "animatetransform", "set", "video", "audio", "source", "track", "math", "handler",
    "listener", "portal", "template", "noscript",
)  # fmt: skip
_TAG = re.compile(r"<\s*/?\s*([a-zA-Z][\w:.-]*)([^>]*)>", re.S)
_ATTR = re.compile(r"""([^\s"'=<>/]+)\s*=\s*("[^"]*"|'[^']*'|[^\s"'>]+)""", re.S)
_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*:")
_SCRIPT_SCHEMES: Final = ("javascript:", "vbscript:", "livescript:", "mocha:")
_RESOURCE_ATTRS: Final = frozenset(
    {"src", "href", "xlink:href", "poster", "action", "formaction", "data", "srcset", "background",
     "longdesc", "manifest", "ping", "cite"}
)  # fmt: skip
_CSS_URL = re.compile(r"url\s*\(\s*([^)]*)\)", re.I)


def _squash(value: str) -> str:
    """What a URL parser sees: no control, space or invisible character, lower case."""
    return "".join(ch for ch in html.unescape(value) if ch.isprintable() and not ch.isspace()).lower()


def _css_findings(css: str) -> list[str]:
    text = re.sub(r"/\*.*?\*/", "", html.unescape(css), flags=re.S)
    found: list[str] = []
    if "\\" in text:
        found.append("css-escape")
    low = text.lower()
    if "@import" in low:
        found.append("css-import")
    if re.search(r"expression\s*\(|behaviou?r\s*:|-moz-binding|javascript:", low):
        found.append("css-script")
    for match in _CSS_URL.finditer(text):
        target = _squash(match.group(1).strip("'\" "))
        if target and not target.startswith("#") and (
            _SCHEME.match(target) or target.startswith(("//", "\\\\", "/\\", "\\/"))
        ):
            found.append(f"css-remote-url:{target[:40]}")
    return found


def find_active_patterns(data: bytes) -> list[str]:
    """Every active pattern left in `data` (a XHTML, SVG or CSS document), in order of appearance."""
    text = data.decode("utf-8", "replace")
    found: list[str] = []
    for tag in _TAG.finditer(text):
        name = tag.group(1).lower().rsplit(":", 1)[-1]
        attrs = {m.group(1).lower(): m.group(2).strip("'\"") for m in _ATTR.finditer(tag.group(2))}
        if name in _FORBIDDEN_TAGS:
            found.append(f"tag:{name}")
        for attr, value in attrs.items():
            plain = attr.rsplit(":", 1)[-1] if attr.startswith(("xlink:", "xml:")) else attr
            if plain.startswith("on"):
                found.append(f"handler:{attr}")
            squashed = _squash(value)
            if any(squashed.startswith(s) for s in _SCRIPT_SCHEMES):
                found.append(f"script-url:{attr}")
            if squashed.startswith("data:") and not (
                name == "img" and re.match(r"data:image/(png|jpeg|gif|webp|avif)[;,]", squashed)
            ):
                found.append(f"data-url:{attr}")
            if attr in _RESOURCE_ATTRS and name != "a" and (
                _SCHEME.match(squashed) or squashed.startswith(("//", "\\\\", "/\\", "\\/"))
            ) and not (name == "use" and squashed.startswith("#")):
                found.append(f"remote:{name}[{attr}]")
            if attr == "href" and name == "a" and squashed.startswith(("//", "\\")):
                found.append("remote:a[//]")
            if name == "a" and _SCHEME.match(squashed) and not squashed.startswith(
                ("http:", "https:", "mailto:")
            ):
                found.append(f"a-scheme:{squashed.split(':')[0]}")
            if attr == "http-equiv":
                found.append("meta-http-equiv")
            if attr == "style":
                found += _css_findings(value)
            if name == "link" and attr == "rel" and "import" in value.lower():
                found.append("link-import")
    for block in re.finditer(r"<\s*style\b[^>]*>(.*?)<\s*/\s*style\s*>", text, re.S | re.I):
        found += _css_findings(block.group(1))
    return found


def find_active_css(data: bytes) -> list[str]:
    """The same for a bare stylesheet."""
    return _css_findings(data.decode("utf-8", "replace"))
