"""M4 L21b (EF-BIB-013, ENF-LEG-005): what a person types never leaves its literal.

The CQL of the BnF SRU and the SPARQL of both endpoints are built around a quoted literal. A
reader (written here, not the code under test) takes the literal out of the query the way the
server would, and the rest of the query must be exactly what the provider wrote: nothing typed
reaches it. Control characters never travel; a very long input is cut.
"""

from __future__ import annotations

import pytest

from shelfhold.plugin_host.adapters.native.bnf import MAX_QUERY, escape_sparql, escape_sru
from shelfhold.plugin_host.adapters.native.wikidata import escape_sparql as wikidata_escape

HOSTILE = [
    'a"b\\c',
    '" } UNION { ?s ?p ?o } #',
    'x" or bib.author all "y',
    "line\nbreak\rand\ttab",
    "*wild?card^",
    '\\"',
    '\\\\" } # ',
    "nul\x00byte\x1f\x7f",
    "plain title",
    "accentué — « guillemets » 日本語",
    '"""',
    "\\",
    "",
    "x" * 5000,
]


def literal_of(query: str, opening: int) -> tuple[str, str]:
    """The text of the literal that opens at `opening`, backslashes undone, and what follows."""
    assert query[opening] == '"'
    out: list[str] = []
    i = opening + 1
    while True:
        assert i < len(query), "the literal never closes"
        char = query[i]
        if char == '"':
            return "".join(out), query[i + 1 :]
        if char == "\\":
            i += 1
            assert i < len(query)
            char = query[i]
        out.append(char)
        i += 1


def cleaned(value: str) -> str:
    return " ".join("".join(c if c.isprintable() else " " for c in value).split())[:MAX_QUERY]


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
@pytest.mark.parametrize("typed", HOSTILE)
@pytest.mark.parametrize("escape", [escape_sru, escape_sparql, wikidata_escape])
def test_a_typed_value_stays_inside_its_literal(typed: str, escape: object) -> None:
    query = '<head> "' + escape(typed) + '" <tail>'  # type: ignore[operator]
    inside, rest = literal_of(query, query.index('"'))
    assert rest == " <tail>"  # nothing typed escaped to the rest of the query
    assert query.startswith('<head> "')
    # what the server reads back is what was typed, minus controls, and bounded
    assert inside == cleaned(typed)
    assert all(c.isprintable() for c in inside)
    assert len(inside) <= MAX_QUERY


@pytest.mark.req("EF-BIB-013", "ENF-LEG-005")
def test_cql_masking_characters_are_escaped_and_sparql_ones_are_not_doubled() -> None:
    assert escape_sru("a*b?c^d") == "a\\*b\\?c\\^d"  # a CQL term is never a wildcard
    assert escape_sparql("a*b?c^d") == "a*b?c^d"  # nothing special in a SPARQL literal
    assert escape_sru('"') == '\\"' and escape_sru("\\") == "\\\\"
    assert escape_sparql('"') == '\\"' and escape_sparql("\\") == "\\\\"
