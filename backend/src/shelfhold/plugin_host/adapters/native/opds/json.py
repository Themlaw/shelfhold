"""OPDS 2.0 (JSON) reader (EF-SRC-006): the same `model.Feed` as the Atom reader."""

from __future__ import annotations

import json
from typing import Any

from shelfhold.plugin_host.adapters.native.opds.model import (
    ACCEPTED_TYPES,
    FREE_RELS,
    LOCKED_RELS,
    MAX_ENTRIES,
    MAX_LINKS,
    MAX_TEXT,
    Entry,
    Feed,
    FeedError,
    Link,
    absolute,
    digest_of,
    media_type,
)

__all__ = ["MAX_BYTES", "parse_opds2"]

MAX_BYTES = 8 * 1024 * 1024


def _str(value: Any, limit: int = MAX_TEXT) -> str:
    return value[:limit] if isinstance(value, str) else ""


def parse_opds2(body: bytes, url: str) -> Feed:
    if len(body) > MAX_BYTES:
        raise FeedError("too_large")
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        raise FeedError("malformed_json") from None
    if not isinstance(document, dict):
        raise FeedError("not_opds2")
    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise FeedError("not_opds2")
    links = _links(document.get("links"), url)
    entries: list[Entry] = []
    for item in _items(document, "navigation"):
        entries.append(_navigation(item, url))
    for item in _items(document, "publications"):
        entries.append(_publication(item, url))
    for group in _items(document, "groups"):
        for key in ("navigation", "publications"):
            for item in _items(group, key):
                entries.append(
                    _navigation(item, url) if key == "navigation" else _publication(item, url)
                )
    entries = entries[:MAX_ENTRIES]
    following = next((k.href for k in links if k.rel == "next"), None)
    search = next((k for k in links if k.rel == "search"), None)
    return Feed("opds2", _str(metadata.get("title")), tuple(entries), following, search)


def _items(container: Any, key: str) -> list[dict[str, Any]]:
    found = container.get(key) if isinstance(container, dict) else None
    if not isinstance(found, list):
        return []
    return [i for i in found[:MAX_ENTRIES] if isinstance(i, dict)]


def _links(raw: Any, base: str) -> list[Link]:
    out: list[Link] = []
    if not isinstance(raw, list):
        return out
    for item in raw[:MAX_LINKS]:
        if not isinstance(item, dict):
            continue
        href = item.get("href")
        resolved = absolute(base, href if isinstance(href, str) else None)
        if resolved is None:
            continue
        rel = item.get("rel")
        rel_text = rel[0] if isinstance(rel, list) and rel else rel
        out.append(
            Link(
                href=resolved,
                rel=_str(rel_text, 256),
                type=_str(item.get("type"), 256),
                title=_str(item.get("title")),
                templated=item.get("templated") is True,
            )
        )
    return out


def _navigation(item: dict[str, Any], base: str) -> Entry:
    href = item.get("href")
    target = absolute(base, href if isinstance(href, str) else None)
    title = _str(item.get("title"))
    return Entry(
        id=target or title, title=title, subsection=target, digest=digest_of(_canonical(item))
    )


def _publication(item: dict[str, Any], base: str) -> Entry:
    meta = item.get("metadata")
    meta = meta if isinstance(meta, dict) else {}
    links = _links(item.get("links"), base)
    free = [k for k in links if k.rel in FREE_RELS and media_type(k.type) in ACCEPTED_TYPES]
    free.sort(key=lambda k: 0 if media_type(k.type) == "application/epub+zip" else 1)
    locked = not free and any(
        k.rel in LOCKED_RELS and media_type(k.type) in ACCEPTED_TYPES for k in links
    )
    authors = meta.get("author")
    names: list[str] = []
    for person in authors if isinstance(authors, list) else [authors]:
        name = person.get("name") if isinstance(person, dict) else person
        if isinstance(name, str) and name.strip():
            names.append(name[:MAX_TEXT])
    subjects = meta.get("subject")
    categories: list[str] = []
    for subject in subjects if isinstance(subjects, list) else [subjects]:
        label = subject.get("name") if isinstance(subject, dict) else subject
        if isinstance(label, str) and label.strip() and len(categories) < MAX_LINKS:
            categories.append(label.strip()[:MAX_TEXT])
    language = meta.get("language")
    if isinstance(language, list):
        language = language[0] if language else ""
    ident = _str(meta.get("identifier"), 1024).strip()
    own = next((k.href for k in links if k.rel == "self"), "")
    return Entry(
        id=ident or own, title=_str(meta.get("title")), summary=_str(meta.get("description")),
        authors=tuple(names), language=_str(language, 35).strip(), acquisitions=tuple(free),
        locked=locked, digest=digest_of(_canonical(item)), links=tuple(links),
        categories=tuple(categories),
    )  # fmt: skip


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
