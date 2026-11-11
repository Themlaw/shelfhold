"""OpenSearch (EF-SRC-006): the description a catalogue serves, and the template it gives.

A template is filled with the term the person typed, percent-encoded: nothing a term holds can add
a parameter, a host or a line. Parameters the host does not know are refused when required and
dropped when optional. Whether the resulting address is allowed is not decided here: the caller
checks it against the catalogue's origin, and the Fetcher against the DomainRules, before any
request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote_plus

from shelfhold.plugin_host.adapters.native.opds.atom import ParseXml
from shelfhold.plugin_host.adapters.native.opds.model import FeedError, absolute

__all__ = ["SearchUrl", "expand", "parse_description", "pick_atom"]

MAX_TERMS = 256
MAX_URLS = 16
_TOKEN = re.compile(r"\{([^{}]*)\}")
_OPTIONAL_EMPTY = "\x00"
_TERMS = {"searchTerms", "query", "q"}
_DEFAULTS = {
    "startPage": "1", "startIndex": "1", "inputEncoding": "UTF-8", "outputEncoding": "UTF-8",
}  # fmt: skip


@dataclass(frozen=True)
class SearchUrl:
    template: str  # absolute, the `{...}` still in it
    type: str


def parse_description(body: bytes, url: str, parse_xml: ParseXml) -> list[SearchUrl]:
    try:
        root = parse_xml(body)
    except (ValueError, TypeError, RecursionError):
        raise FeedError("malformed_xml") from None
    if str(root.tag).rsplit("}", 1)[-1] != "OpenSearchDescription":
        raise FeedError("not_opensearch")
    found: list[SearchUrl] = []
    for child in root:
        if str(child.tag).rsplit("}", 1)[-1] != "Url" or len(found) >= MAX_URLS:
            continue
        template = child.attrib.get("template", "")
        resolved = absolute(url, template)
        if resolved is not None:
            found.append(SearchUrl(resolved, child.attrib.get("type", "")))
    return found


def pick_atom(urls: list[SearchUrl]) -> SearchUrl | None:
    """The first template that answers with an OPDS (Atom) catalogue, never the HTML form."""
    for candidate in urls:
        if candidate.type.lower().startswith("application/atom+xml"):
            return candidate
    return None


def expand(template: str, terms: str, *, page: int | None = None) -> str:
    """The address of a search: `{searchTerms}` and the optional parameters filled in."""
    encoded = quote_plus(terms[:MAX_TERMS], safe="")
    values: dict[str, str | None] = {
        "searchTerms": encoded, "query": encoded, "q": encoded,
        "startPage": str(page) if page is not None else None,
        "count": None, "language": None, "startIndex": None,
        "inputEncoding": None, "outputEncoding": None,
    }  # fmt: skip
    used_terms = False

    def fill(match: re.Match[str]) -> str:
        nonlocal used_terms
        token = match.group(1)
        if token.startswith("?"):  # RFC 6570 form-style query: {?query,page}
            pairs = []
            for name in (n.strip() for n in token[1:].split(",") if n.strip()):
                value = values.get(name)  # an unknown or empty name is simply left out
                if value is not None:
                    used_terms = used_terms or name in _TERMS
                    pairs.append(f"{name}={value}")
            return "?" + "&".join(pairs) if pairs else ""
        optional = token.endswith("?")
        name = token.removesuffix("?").split(":", 1)[-1]
        value = values.get(name)
        if value is None and not optional:
            value = _DEFAULTS.get(name)
        if value is None:
            if optional:  # unknown or empty: the whole `name=` goes
                return _OPTIONAL_EMPTY
            raise FeedError("template_parameter")
        used_terms = used_terms or name in _TERMS
        return value

    url = _TOKEN.sub(fill, template)
    if not used_terms:
        raise FeedError("template_without_terms")
    return _clean(url)


def _clean(url: str) -> str:
    """Drop the `name=` of every optional parameter that stayed empty."""
    head, sep, query = url.partition("?")
    if not sep:
        return url.replace(_OPTIONAL_EMPTY, "")
    pairs = [p for p in query.split("&") if p and _OPTIONAL_EMPTY not in p]
    kept = head.replace(_OPTIONAL_EMPTY, "")
    return f"{kept}?{'&'.join(pairs)}" if pairs else kept
