"""Upstream text is plain text unless it passed through nh3 here (ENF-SEC-015).

`sanitize_upstream_text` is the single entry point (import-linter), and the only module that
imports nh3. A title, a summary or a description that comes from a source, an extension, an OPDS
feed or a file is untrusted data: Shelfhold must never be an injection relay towards its own
atelier, Kavita (ComicInfo `Summary`, OPF) or a notification.

Three destinations, all fed by the same cleaning:

* `PLAIN` (default): no markup at all. Tags are removed (the content of script and style
  elements with them), entities are decoded once, and the result is re-cleaned until it no longer
  changes, so an encoded `&lt;script&gt;` can never come back as a tag. Characters are kept: a
  title such as `Re:Zero [Light Novel]` stays as it is. The consumer escapes for its own context.
* `HTML`: a minimal nh3 allowlist (`ALLOWED_HTML_TAGS`; `a` carries `href` only, http, https or
  mailto, with `rel="noopener noreferrer"` added). For a destination that renders HTML.
* `MARKDOWN`: `PLAIN`, then every Markdown construct backslash-escaped (links, images, code,
  headings, lists, HTML), for a destination that renders Markdown, such as a notification.

Control characters, bidirectional overrides, zero-width characters and Unicode tag characters
(invisible "ASCII smuggling") are removed first, so `<scr\\x00ipt>` is seen for what it is.
"""

from __future__ import annotations

import html
import re
import unicodedata
from enum import StrEnum
from typing import Final

import nh3

MAX_INPUT_CHARS: Final = 1_000_000
_MAX_PASSES: Final = 8

ALLOWED_HTML_TAGS: Final = frozenset(
    {"p", "br", "b", "strong", "i", "em", "ul", "ol", "li", "blockquote", "a"}
)
# What can appear in the output. nh3 is given only `href`: it adds `rel` itself (link_rel).
ALLOWED_HTML_ATTRIBUTES: Final = {"a": frozenset({"href", "rel"})}
_URL_SCHEMES: Final = frozenset({"http", "https", "mailto"})
_LINK_REL: Final = "noopener noreferrer"
_DROP_WITH_CONTENT: Final = frozenset(
    {"script", "style", "iframe", "object", "embed", "noscript", "template", "svg", "math",
     "head", "title", "textarea", "xmp", "noembed", "noframes", "frameset"}
)  # fmt: skip
_SAFE_HREF: Final = re.compile(r"(?:https?://|mailto:)[^\s\x00-\x1f]*\Z", re.IGNORECASE)

# Everything that is invisible, reorders text or cannot be written to XML 1.0:
# C0 and C1 controls but \n and \t; lone surrogates (they break UTF-8 encoding); U+FFFE and U+FFFF;
# soft hyphen, Arabic letter mark, Hangul and Khmer/Mongolian fillers; zero-width space and
# marks, bidi embeddings/overrides/isolates, BOM, word joiner and invisible operators,
# interlinear annotation; the Unicode tag block. ZWJ and ZWNJ stay: emoji and Persian need them.
_INVISIBLE: Final = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff\ufffe\uffff"
    "\u00ad\u061c\u115f\u1160\u17b4\u17b5\u180e\u3164\uffa0"
    "\u200b\u200e\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\ufff9-\ufffb"
    "\U000e0000-\U000e007f]"
)
_MARKDOWN_SPECIAL: Final = re.compile(r"([\\`*_\[\]()<>#!|~&{}])")
_MARKDOWN_LINE_START: Final = re.compile(r"(?m)^(\s*)([-+]|\d+[.)])(?=\s)")


class Destination(StrEnum):
    PLAIN = "plain"
    HTML = "html"
    MARKDOWN = "markdown"


def _clean_characters(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", _INVISIBLE.sub("", text))


def _href_filter(tag: str, attribute: str, value: str) -> str | None:
    if tag == "a" and attribute == "href":
        return value if _SAFE_HREF.match(value.strip()) else None
    return value


def _strip_markup(text: str) -> str:
    """Tags out, entities decoded, repeated until stable: the result parses as text only."""
    for _ in range(_MAX_PASSES):
        # An entity can spell an invisible character (`&#x202E;`): drop them after every decoding.
        cleaned = _INVISIBLE.sub(
            "",
            html.unescape(
                nh3.clean(
                    text, tags=set(), attributes={}, clean_content_tags=set(_DROP_WITH_CONTENT)
                )
            ),
        )
        if cleaned == text:
            return text
        text = cleaned
    # Still moving after eight layers of encoding: nothing legitimate looks like that.
    return _INVISIBLE.sub("", text.replace("<", "").replace(">", "").replace("&", ""))


def _html(text: str) -> str:
    return nh3.clean(
        text,
        tags=set(ALLOWED_HTML_TAGS),
        attributes={"a": {"href"}},
        url_schemes=set(_URL_SCHEMES),
        link_rel=_LINK_REL,
        attribute_filter=_href_filter,
        clean_content_tags=set(_DROP_WITH_CONTENT),
        strip_comments=True,
    )


def _escape_markdown(text: str) -> str:
    text = _MARKDOWN_SPECIAL.sub(r"\\\1", text)
    return _MARKDOWN_LINE_START.sub(lambda m: f"{m.group(1)}\\{m.group(2)}", text)


def _cut(text: str, max_length: int) -> str:
    return text if len(text) <= max_length else text[:max_length].rstrip()


def sanitize_upstream_text(
    text: str,
    *,
    destination: Destination | str = Destination.PLAIN,
    max_length: int | None = None,
) -> str:
    """Neutralise an untrusted title, summary or description for `destination`.

    `max_length` bounds the text itself. In HTML, a cut would leave a broken tag or entity, so an
    over-long text becomes escaped plain text (entities may then add characters).
    """
    if not isinstance(text, str):
        raise TypeError("upstream text must be a str")
    destination = Destination(destination)
    text = _clean_characters(text[:MAX_INPUT_CHARS])
    if destination is Destination.HTML:
        cleaned = _INVISIBLE.sub("", _html(text))  # nh3 decodes `&#x202E;` to the character
        if max_length is None or len(html.unescape(nh3.clean(cleaned, tags=set()))) <= max_length:
            return cleaned
        return html.escape(_cut(_strip_markup(text), max_length), quote=True)
    plain = _strip_markup(text)
    if max_length is not None:
        plain = _cut(plain, max_length)
    if destination is Destination.MARKDOWN:
        return _escape_markdown(plain)
    return plain
