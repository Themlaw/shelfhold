"""Which entries of an EPUB hold active content: the adoption report's question (EF-BIB-051).

An EPUB already in a library is not rewritten. It is only looked at, to tell the admin: a script,
an `on*` handler, a `javascript:` URL, a frame or an embedded object, a remote resource, a remote
`@import` or `url()`. Nothing here produces a derivative (`xhtml_neutralizer` is the only producer);
this reads and answers, over the same archive and XML readers. A page the reader refuses, or an
archive the neutralizer would refuse, counts as active: it is not safe to hand to a reader either.
"""

from __future__ import annotations

import re
from pathlib import PurePath
from typing import BinaryIO, Final

from shelfhold.infrastructure.archives import (
    DEFAULT_LIMITS,
    UnsafeArchiveError,
    safe_open_archive,
)
from shelfhold.infrastructure.content_trust.xhtml_neutralizer import parse_xml_document
from shelfhold.infrastructure.xml import Element

_PAGE_BYTES: Final = 32 * 1024 * 1024
_MARKUP: Final = frozenset({".xhtml", ".xht", ".html", ".htm", ".svg"})
_ACTIVE_TAGS: Final = frozenset(
    {"script", "iframe", "frame", "frameset", "object", "embed", "applet", "form"}
)
_URL_ATTRS: Final = frozenset(
    {"src", "data", "poster", "srcset", "action", "formaction", "background"}
)
_SCRIPT_URL: Final = re.compile(r"^[\s\x00-\x20]*(javascript|vbscript)[\s\x00-\x20]*:", re.I)
_REMOTE: Final = re.compile(r"^[\s\x00-\x20]*(https?:)?//", re.I)
_CSS_REMOTE: Final = re.compile(rb"(@import|url\()\s*['\"]?\s*(https?:)?//", re.I)


def _local(name: object) -> str:
    text = str(name)
    return text.rsplit("}", 1)[-1].lower() if "}" in text else text.lower()


def _markup_is_active(root: Element) -> bool:
    for element in root.iter():
        tag = _local(element.tag)
        if tag in _ACTIVE_TAGS:
            return True
        for key, value in element.attrib.items():
            attr, text = _local(key), str(value)
            if attr.startswith("on") or _SCRIPT_URL.match(text):
                return True
            remote_attr = attr in _URL_ATTRS or (attr == "href" and tag not in {"a", "area"})
            if remote_attr and _REMOTE.match(text):
                return True
    return False


def find_active_content(source: BinaryIO, *, name: str = "book.epub") -> tuple[str, ...]:
    """The archive entries that hold active content, in archive order (empty: none found)."""
    found: list[str] = []
    try:
        with safe_open_archive(source, name=name, limits=DEFAULT_LIMITS) as archive:
            for entry in archive.entries:
                if entry.is_dir:
                    continue
                ext = PurePath(entry.name).suffix.lower()
                if ext not in _MARKUP and ext != ".css":
                    continue
                data = archive.read(entry.name, max_bytes=_PAGE_BYTES)
                if ext == ".css":
                    if _CSS_REMOTE.search(bytes(data)):
                        found.append(entry.name)
                    continue
                try:
                    if _markup_is_active(parse_xml_document(bytes(data))):
                        found.append(entry.name)
                except Exception:  # a page the reader refuses is not one to hand to a reader
                    found.append(entry.name)
    except UnsafeArchiveError:
        return (name,)
    return tuple(found)
