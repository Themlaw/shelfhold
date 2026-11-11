"""Targeted rewrite of an OPF (EF-EXP-033).

Only these elements are written: `calibre:series`, `calibre:series_index`, the EPUB 3
`belongs-to-collection` of the series (with its `collection-type` and `group-position`), and
`dc:rights` / `dc:source` when the book has none. They are spliced into the text of the document,
which is otherwise copied as it was: no serialisation, so prefixes, comments, attribute order,
the `spine` (`page-progression-direction`), the writing mode and the `schema:access*` metadata
come out character for character. The edit has no age: nothing here can write one, and an age
that the book already carries in its metadata (`typicalAgeRange`, an `18+` subject, a rating, a
`refines` of one of those) is removed from the exported OPF (D1, Z3-a); the original OPF is kept in
`/data/meta`, so nothing is lost.

The document is read by `safe_parse_xml` and the result is read again by it, then compared with
the original: whatever was in `<metadata>` and is not one of the replaced series elements must
still be there, or the rewrite is refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from shelfhold.infrastructure.archives import SafeArchive
from shelfhold.infrastructure.xml import Element, safe_parse_xml

OPF_NS: Final = "{http://www.idpf.org/2007/opf}"
DC_URI: Final = "http://purl.org/dc/elements/1.1/"
DC: Final = f"{{{DC_URI}}}"
_CONTAINER_NS: Final = "{urn:oasis:names:tc:opendocument:xmlns:container}"
_OPF_MEDIA_TYPE: Final = "application/oebps-package+xml"

_PREFIX: Final = r"(?:[\w.-]+:)?"
_METADATA_OPEN: Final = re.compile(rf"<(?P<p>{_PREFIX})metadata\b(?:[^>\"']|\"[^\"]*\"|'[^']*')*>")
_META: Final = re.compile(
    rf"(?P<lead>[ \t]*)<(?P<p>{_PREFIX})meta\b(?P<attrs>(?:[^>\"']|\"[^\"]*\"|'[^']*')*?)"
    r"(?:/>|>(?P<body>.*?)</(?P=p)meta\s*>)[ \t]*(?P<eol>\r?\n)?",
    re.DOTALL,
)
_ATTR: Final = re.compile(r"""\b(?P<name>[\w:.-]+)\s*=\s*(?:"(?P<d>[^"]*)"|'(?P<s>[^']*)')""")
_DC_DECLARATION: Final = re.compile(rf"""xmlns:(?P<p>[\w.-]+)\s*=\s*["']{re.escape(DC_URI)}["']""")
_INDEX: Final = re.compile(r"\d+(?:\.\d+)?")
_ILLEGAL: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_SERIES_NAMES: Final = frozenset({"calibre:series", "calibre:series_index"})
_SERIES_PROPS: Final = frozenset({"belongs-to-collection", "collection-type", "group-position"})
_ELEMENT: Final = re.compile(
    rf"(?P<lead>[ \t]*)<(?P<p>{_PREFIX})(?P<local>meta|subject)\b"
    r"(?P<attrs>(?:[^>\"']|\"[^\"]*\"|'[^']*')*?)"
    r"(?:/>|>(?P<body>.*?)</(?P=p)(?P=local)\s*>)[ \t]*(?P<eol>\r?\n)?",
    re.DOTALL,
)
# An age in a metadata name or property (any prefix the book declared), and in a subject.
_AGE_KEY: Final = re.compile(
    r"age[-_ ]?(?:rat|range|limit|group|class|restrict|suit)|typicalage|rating|matur|adult"
    r"|audience|pegi|esrb|advisory|contentwarn|nsfw|explicit",
    re.IGNORECASE,
)
_AGE_SUBJECT: Final = re.compile(
    r"\d{1,2}\s*\+|\+\s*\d{1,2}\b|\b(?:r|x)[-\s]?(?:18|rated)\b|\bages?\b"
    r"|\d{1,2}\s*(?:ans|years?|yrs?)\b|matur|adult|nsfw|explicit|erwachsen",
    re.IGNORECASE,
)


class OpfError(ValueError):
    """The OPF cannot be edited as asked; `reason` is a stable word."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class OpfEdit:
    """What may be written. There is no field for an age, by design."""

    series: str | None = None
    series_index: str | None = None  # `3`, `3.5`; anything else is not written
    rights: str | None = None  # written only if the OPF has no `dc:rights`
    source: str | None = None  # written only if the OPF has no `dc:source`


@dataclass(frozen=True, slots=True)
class OpfRewrite:
    opf: bytes
    changed: bool


def _text(value: str) -> str:
    return _ILLEGAL.sub("", value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attr(value: str) -> str:
    return _text(value).replace('"', "&quot;")


def _attributes(raw: str) -> dict[str, str]:
    return {m["name"]: m["d"] if m["d"] is not None else m["s"] for m in _ATTR.finditer(raw)}


def find_opf_path(archive: SafeArchive) -> str:
    """The `full-path` of the package document, from `META-INF/container.xml`."""
    names = {entry.name for entry in archive.entries}
    if "META-INF/container.xml" not in names:
        raise OpfError("no_container")
    root = safe_parse_xml(archive.read("META-INF/container.xml"))
    roots = [r for r in root.iter(f"{_CONTAINER_NS}rootfile") if r.get("full-path")]
    preferred = [r for r in roots if r.get("media-type") == _OPF_MEDIA_TYPE] or roots
    if not preferred:
        raise OpfError("no_rootfile")
    path = str(preferred[0].get("full-path"))
    if path not in names:
        raise OpfError("opf_missing", path)
    return path


def _collection_ids_to_drop(metadata: Element) -> set[str]:
    """The `belongs-to-collection` of a series (or of no stated type); a `set` is the book's own."""
    types = {
        m.get("refines", "").lstrip("#"): (m.text or "").strip()
        for m in metadata
        if m.get("property") == "collection-type"
    }
    return {
        m.get("id") or ""
        for m in metadata
        if m.get("property") == "belongs-to-collection" and types.get(m.get("id") or "") != "set"
    }


def _local(name: str | None) -> str:
    return (name or "").rpartition(":")[2]


def _is_age(local: str, name: str | None, prop: str | None, text: str) -> bool:
    """An age in any of the forms a book is known to carry (any case, any prefix)."""
    if local.lower() == "subject":
        return _AGE_SUBJECT.search(text) is not None
    return any(_AGE_KEY.search(_local(key)) for key in (name, prop) if key)


def _age_ids(metadata: Element) -> set[str]:
    """Ids of the age elements and, to a fixed point, of whatever refines them."""
    ids: set[str] = set()
    for child in metadata:
        tag = child.tag.rpartition("}")[2]
        if tag in {"meta", "subject"} and _is_age(
            tag, child.get("name"), child.get("property"), child.text or ""
        ):
            ids.add(child.get("id") or "")
    ids.discard("")
    grown = True
    while grown:
        grown = False
        for child in metadata:
            ident = child.get("id") or ""
            if ident and ident not in ids and child.get("refines", "").lstrip("#") in ids:
                ids.add(ident)
                grown = True
    return ids


def _drop(inner: str, drop_ids: set[str], age_ids: set[str], *, series: bool) -> str:
    def dropped(local: str, attrs: dict[str, str], body: str) -> bool:
        refines = attrs.get("refines", "").lstrip("#")
        if _is_age(local, attrs.get("name"), attrs.get("property"), body):
            return True
        if attrs.get("id", "") in age_ids or (refines != "" and refines in age_ids):
            return True
        if not series:
            return False
        if attrs.get("name") in _SERIES_NAMES:
            return True
        prop = attrs.get("property")
        if prop == "belongs-to-collection":
            return attrs.get("id", "") in drop_ids
        return (
            refines in drop_ids and refines != "" and prop in {"collection-type", "group-position"}
        )

    def visit(match: re.Match[str]) -> str:
        attrs = _attributes(match["attrs"])
        return "" if dropped(match["local"], attrs, match["body"] or "") else match[0]

    return _ELEMENT.sub(visit, inner)


def _fresh_id(existing: set[str]) -> str:
    n = 1
    while f"coll{n}" in existing:
        n += 1
    return f"coll{n}"


def rewrite_opf(raw: bytes, edit: OpfEdit) -> OpfRewrite:
    root = safe_parse_xml(raw)
    metadata = root.find(f"{OPF_NS}metadata")
    if metadata is None:
        raise OpfError("no_metadata")
    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError:
        raise OpfError("encoding", "only UTF-8 package documents are edited") from None
    opening = _METADATA_OPEN.search(text)
    if opening is None or opening["p"] is None:
        raise OpfError("no_metadata")
    prefix = opening["p"]
    closing = re.compile(rf"</{re.escape(prefix)}metadata\s*>").search(text, opening.end())
    if closing is None:
        raise OpfError("no_metadata")
    inner = text[opening.end() : closing.start()]
    epub3 = str(root.get("version", "")).startswith("3")
    have = {child.tag for child in metadata}

    ids = {c.get("id") for c in root.iter() if c.get("id")}
    drop = _collection_ids_to_drop(metadata)
    drop.discard("")
    index = edit.series_index if edit.series_index and _INDEX.fullmatch(edit.series_index) else None
    lines: list[str] = []
    open_, close = f"<{prefix}meta", f"</{prefix}meta>"
    original_inner = inner
    inner = _drop(inner, drop, _age_ids(metadata), series=bool(edit.series))
    if edit.series:
        name = _attr(edit.series)
        lines.append(f'<{prefix}meta name="calibre:series" content="{name}"/>')
        if index:
            lines.append(f'<{prefix}meta name="calibre:series_index" content="{index}"/>')
        if epub3:
            cid = _fresh_id({str(i) for i in ids} - drop)
            lines.append(
                f'{open_} property="belongs-to-collection" id="{cid}">{_text(edit.series)}{close}'
            )
            lines.append(f'{open_} refines="#{cid}" property="collection-type">series{close}')
            if index:
                lines.append(f'{open_} refines="#{cid}" property="group-position">{index}{close}')
    declared = _DC_DECLARATION.search(text)
    dc_prefix = declared["p"] if declared else "dc"
    local = "" if declared else f' xmlns:{dc_prefix}="{DC_URI}"'
    for tag, value in (("rights", edit.rights), ("source", edit.source)):
        if value and value.strip() and f"{DC}{tag}" not in have:
            lines.append(f"<{dc_prefix}:{tag}{local}>{_text(value.strip())}</{dc_prefix}:{tag}>")
    if not lines and inner == original_inner:
        return OpfRewrite(bytes(raw), changed=False)

    eol = "\r\n" if "\r\n" in inner else "\n"
    found = re.search(r"\n([ \t]*)<", inner)
    indent = found[1] if found else "    "
    trailing = len(inner) - len(inner.rstrip(" \t"))
    head, tail = inner[: len(inner) - trailing], inner[len(inner) - trailing :]
    if head and not head.endswith("\n"):
        head += eol
    block = "".join(f"{indent}{line}{eol}" for line in lines)
    out = text[: opening.end()] + head + block + tail + text[closing.start() :]
    result = out.encode("utf-8")
    _check(root, safe_parse_xml(result), edit)
    return OpfRewrite(result, changed=True)


def _signature(element: Element) -> tuple[str, tuple[tuple[str, str], ...], str]:
    return element.tag, tuple(sorted(element.attrib.items())), (element.text or "").strip()


def _is_series(child: Element) -> bool:
    return child.get("name") in _SERIES_NAMES or child.get("property") in _SERIES_PROPS


def _carries_age(child: Element) -> bool:
    tag = child.tag.rpartition("}")[2]
    return tag in {"meta", "subject"} and _is_age(
        tag, child.get("name"), child.get("property"), child.text or ""
    )


def _check(before: Element, after: Element, edit: OpfEdit) -> None:
    """The rewrite wrote nothing outside the list, kept the book's own metadata, and left no age.

    Allowed to appear: the series `meta`, the collection (and the two `meta` that refine it, by the
    id written here), `dc:rights`, `dc:source`. Allowed to go: the series it replaces and the ages.
    Anything else new, or anything else gone, refuses the whole rewrite.
    """
    old, new = before.find(f"{OPF_NS}metadata"), after.find(f"{OPF_NS}metadata")
    if old is None or new is None:
        raise OpfError("rewrite_refused", "metadata lost")
    age_ids = _age_ids(old)
    ids_kept = {c.get("id") for c in old if c.get("id") and not _is_series(c)}
    kept = [_signature(c) for c in new]
    for child in old:
        gone = (
            _carries_age(child)
            or child.get("id") in age_ids
            or (child.get("refines", "").lstrip("#") in age_ids)
        )
        removable = gone or (edit.series is not None and _is_series(child))
        if not removable and _signature(child) not in kept:
            raise OpfError("rewrite_refused", f"{child.tag} changed")
    was = [_signature(c) for c in old]
    written_ids = {c.get("id") for c in new if c.get("property") == "belongs-to-collection"}
    for child in new:
        if _carries_age(child) or child.get("id") in age_ids:
            raise OpfError("rewrite_refused", "an age is still there")
        if _signature(child) in was:
            was.remove(_signature(child))
            continue
        if not _may_be_written(child, edit, ids_kept, written_ids):
            raise OpfError("rewrite_refused", f"{child.tag} is not on the list")
    for part in ("manifest", "spine", "guide"):
        a, b = before.find(f"{OPF_NS}{part}"), after.find(f"{OPF_NS}{part}")
        if (a is None) != (b is None) or (a is not None and b is not None and _tree(a) != _tree(b)):
            raise OpfError("rewrite_refused", f"{part} changed")


def _may_be_written(
    child: Element, edit: OpfEdit, ids_kept: set[str | None], written_ids: set[str | None]
) -> bool:
    """Exact names, exact case: a look-alike (`Calibre:Series`, a renamed namespace) is refused."""
    attrs = dict(child.attrib)
    if child.tag in {f"{DC}rights", f"{DC}source"}:
        return not attrs
    if child.tag != f"{OPF_NS}meta" or edit.series is None:
        return False
    name, prop = attrs.get("name"), attrs.get("property")
    if name in _SERIES_NAMES:
        return set(attrs) == {"name", "content"}
    if prop == "belongs-to-collection":
        return set(attrs) == {"property", "id"} and attrs["id"] not in ids_kept
    if prop in {"collection-type", "group-position"}:
        return set(attrs) == {"refines", "property"} and attrs["refines"][1:] in written_ids
    return False


def _tree(element: Element) -> list[tuple[str, tuple[tuple[str, str], ...], str]]:
    return [_signature(e) for e in element.iter()]
