"""`search_page`: the Works that hold every word of a query (EF-BIB-010, EF-BIB-022).

Each word is answered by the table that fits it, and the answers are intersected: a word of
letters by the FTS5 table (prefix, diacritics and case folded), a CJK word of three characters or
more by the trigram table, a shorter one by `LIKE` (a trigram needs three), and every word by
`LIKE` when the tables are not there (D7). The parental ceiling is a filter of this query, never
of the index. The order is the sort key of the title in the language of the Work's Edition:
the articles Le, La, Les, L', The (and their kin) are ignored.
"""

from dataclasses import dataclass

import sqlalchemy as sa

from shelfhold.catalog.application.queries import ReaderScope, visible_works_clause
from shelfhold.catalog.application.search.capability import SearchCapability, search_capability
from shelfhold.catalog.application.search.query import (
    Term,
    like_pattern,
    match_expression,
    terms_of,
)
from shelfhold.catalog.application.search.tables import fts_tables
from shelfhold.catalog.domain.sort_key import sort_key
from shelfhold.infrastructure.schema import catalog

__all__ = ["DEFAULT_LIMIT", "MAX_LIMIT", "SearchHit", "SearchPage", "search_page"]

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass(frozen=True)
class SearchHit:
    work_id: str
    title: str
    kind: str
    sort_key: str


def _by_match(connection: sa.Connection, table: str, term: Term, *, trigram: bool) -> set[str]:
    rows = connection.execute(
        sa.text(f"SELECT entity_id FROM {table} WHERE {table} MATCH :m"),  # noqa: S608
        {"m": match_expression(term, trigram=trigram)},
    )
    return {r.entity_id for r in rows}


def _by_like(connection: sa.Connection, term: Term) -> set[str]:
    pattern = like_pattern(term)
    w, wt = catalog.work.c, catalog.work_title.c
    c, n = catalog.contribution.c, catalog.contributor_name.c
    escape = "\\"
    queries = (
        sa.select(w.id).where(w.title.like(pattern, escape=escape)),
        sa.select(wt.work_id).where(wt.title.like(pattern, escape=escape)),
        sa.select(c.entity_id)
        .select_from(
            catalog.contribution.join(
                catalog.contributor_name, n.contributor_id == c.contributor_id
            )
        )
        .where(c.entity_type == "work", n.name.like(pattern, escape=escape)),
    )
    found: set[str] = set()
    for query in queries:
        found.update(r[0] for r in connection.execute(query))
    return found


def _matching(connection: sa.Connection, terms: list[Term], caps: SearchCapability) -> set[str]:
    result: set[str] | None = None
    for term in terms:
        if not caps.fts5 or (term.cjk and (len(term) < 3 or not caps.trigram)):
            ids = _by_like(connection, term)
        elif term.cjk:
            ids = _by_match(connection, fts_tables.TRIGRAM_TABLE, term, trigram=True)
        else:
            ids = _by_match(connection, fts_tables.FTS_TABLE, term, trigram=False)
        result = ids if result is None else result & ids
        if not result:
            return set()
    return result or set()


@dataclass(frozen=True)
class SearchPage:
    hits: list[SearchHit]  # the page asked for
    total: int  # the Works the reader may see that match, all pages together


def search_page(
    connection: sa.Connection,
    query: str,
    *,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    scope: ReaderScope | None = None,
) -> SearchPage:
    """The Works whose titles or contributor names hold every word of `query`, in title order,
    and how many there are.

    `scope` is the reader's (`queries.visible_works_clause`, the one filter of L09): it is a
    condition of this query, applied before the page is cut and before the total is made, so a
    Work the reader may not see is neither returned nor counted. No scope means the whole
    catalogue (the indexer, the CLI)."""
    terms = terms_of(query)
    if not terms:
        return SearchPage([], 0)
    ids = _matching(connection, terms, search_capability(connection))
    # no early return on "nothing matched": a hidden title and an absent one cost the same
    # statements, so the time of an answer tells nothing about which of the two it is
    w = catalog.work.c
    wanted: list[sa.ColumnElement[bool]] = [] if scope is None else [visible_works_clause(scope)]
    langs = {
        r.work_id: r.lang
        for r in connection.execute(
            sa.select(
                catalog.edition.c.work_id, sa.func.min(catalog.edition.c.lang).label("lang")
            ).group_by(catalog.edition.c.work_id)
        )
    }
    hits: list[SearchHit] = []
    ordered = sorted(ids)
    for start in range(0, max(len(ordered), 1), 500):
        rows = connection.execute(
            sa.select(w.id, w.title, w.kind).where(w.id.in_(ordered[start : start + 500]), *wanted)
        )
        hits += [SearchHit(r.id, r.title, r.kind, sort_key(r.title, langs.get(r.id))) for r in rows]
    hits.sort(key=lambda hit: (hit.sort_key, hit.work_id))
    start = max(0, offset)
    return SearchPage(hits[start : start + max(0, min(limit, MAX_LIMIT))], len(hits))
