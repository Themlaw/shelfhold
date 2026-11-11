"""Targeted rewrite of an OPF (EF-EXP-033).

Only these elements are written: `calibre:series`, `calibre:series_index`, the EPUB 3
`belongs-to-collection` of the series (with its `collection-type` and `group-position`), and
`dc:rights` / `dc:source` when the book has none. They are spliced into the text of the document,
which is otherwise copied as it was: no serialisation, so prefixes, comments, attribute order,
the `spine` (`page-progression-direction`), the writing mode and the `schema:access*` metadata
come out character for character. The edit has no age: nothing here can write one (D1, Z3-a).

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
_ILLEGAL: Final = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_SERIES_NAMES: Final = frozenset({"calibre:series", "calibre:series_index"})


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


def _drop_series(inner: str, drop_ids: set[str]) -> str:
    def dropped(attrs: dict[str, str]) -> bool:
        if attrs.get("name") in _SERIES_NAMES:
            return True
        prop = attrs.get("property")
        if prop == "belongs-to-collection":
            return attrs.get("id", "") in drop_ids
        refines = attrs.get("refines", "").lstrip("#")
        return (
            refines in drop_ids and refines != "" and prop in {"collection-type", "group-position"}
        )

    def visit(match: re.Match[str]) -> str:
        return "" if dropped(_attributes(match["attrs"])) else match[0]

    return _META.sub(visit, inner)


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
    if edit.series:
        inner = _drop_series(inner, drop)
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
    if not lines:
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


def _check(before: Element, after: Element, edit: OpfEdit) -> None:
    """Everything of the book's own is still there and the rest of the package is untouched."""
    old, new = before.find(f"{OPF_NS}metadata"), after.find(f"{OPF_NS}metadata")
    if old is None or new is None:
        raise OpfError("rewrite_refused", "metadata lost")
    kept = [_signature(c) for c in new]
    for child in old:
        series = child.get("name") in _SERIES_NAMES or child.get("property") in {
            "belongs-to-collection", "collection-type", "group-position",
        }  # fmt: skip
        if (edit.series is None or not series) and _signature(child) not in kept:
            raise OpfError("rewrite_refused", f"{child.tag} changed")
    for part in ("manifest", "spine", "guide"):
        a, b = before.find(f"{OPF_NS}{part}"), after.find(f"{OPF_NS}{part}")
        if (a is None) != (b is None) or (a is not None and b is not None and _tree(a) != _tree(b)):
            raise OpfError("rewrite_refused", f"{part} changed")


def _tree(element: Element) -> list[tuple[str, tuple[tuple[str, str], ...], str]]:
    return [_signature(e) for e in element.iter()]
