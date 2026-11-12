"""The index, a projection of the catalog (EF-BIB-010).

One document per Work: its titles (the Work's own and every alternative, in every script) and the
names of the contributors credited on it (every `contributor_name`, in every script). Never a
coverage row, never an age: the parental filter is the query's. `rebuild` and the incremental
passes build the very same document, so the one reproduces the other.
"""

import hashlib
from dataclasses import dataclass

import sqlalchemy as sa

from shelfhold.catalog.application.search.capability import SearchCapability, ensure_tables
from shelfhold.catalog.application.search.romanize import romanized
from shelfhold.catalog.application.search.tables import fts_tables
from shelfhold.infrastructure.schema import catalog

__all__ = ["Document", "documents", "index_work", "rebuild", "sync"]

ENTITY = "work"
_TABLES = (fts_tables.FTS_TABLE, fts_tables.TRIGRAM_TABLE)


@dataclass(frozen=True)
class Document:
    work_id: str
    title: str
    names: str

    @property
    def digest(self) -> str:
        raw = f"{self.title}\x00{self.names}".encode()
        return "sha256:" + hashlib.sha256(raw).hexdigest()


def _join(values: set[str]) -> str:
    """The texts, and the reading of each Japanese one (D6): found by either writing."""
    readings = {r for value in values if (r := romanized(value))}
    return "\n".join(sorted(values | readings))


def documents(connection: sa.Connection, work_id: str | None = None) -> list[Document]:
    """The documents of every Work (or of one), by one read of the titles and one of the names."""
    w, wt, c, n = (
        catalog.work.c,
        catalog.work_title.c,
        catalog.contribution.c,
        catalog.contributor_name.c,
    )
    titles: dict[str, set[str]] = {}
    names: dict[str, set[str]] = {}
    where_work = [] if work_id is None else [w.id == work_id]
    for row in connection.execute(sa.select(w.id, w.title).where(*where_work)):
        titles.setdefault(row.id, set()).add(row.title)
    for row in connection.execute(
        sa.select(wt.work_id, wt.title).where(*([] if work_id is None else [wt.work_id == work_id]))
    ):
        titles.setdefault(row.work_id, set()).add(row.title)
    for credit in connection.execute(
        sa.select(c.entity_id, n.name, catalog.contributor.c.primary_name)
        .select_from(
            catalog.contribution.join(
                catalog.contributor_name, n.contributor_id == c.contributor_id
            ).join(catalog.contributor, catalog.contributor.c.id == c.contributor_id)
        )
        .where(c.entity_type == ENTITY, *([] if work_id is None else [c.entity_id == work_id]))
    ):
        names.setdefault(credit.entity_id, set()).update((credit.name, credit.primary_name))
    return [
        Document(wid, _join(titles[wid]), _join(names.get(wid, set()))) for wid in sorted(titles)
    ]


def _present(connection: sa.Connection) -> list[str]:
    return [
        t
        for t in _TABLES
        if connection.execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :n"), {"n": t}
        ).first()
    ]


def _delete(connection: sa.Connection, ids: list[str], tables: list[str]) -> None:
    for table in tables:
        for chunk in range(0, len(ids), 500):
            batch = ids[chunk : chunk + 500]
            marks = ", ".join(f":i{k}" for k in range(len(batch)))
            connection.execute(
                sa.text(f"DELETE FROM {table} WHERE entity_id IN ({marks})"),  # noqa: S608
                {f"i{k}": v for k, v in enumerate(batch)},
            )


def index_work(connection: sa.Connection, work_id: str) -> None:
    """One Work again, in the tables there are (none: nothing to do, search falls back)."""
    if not _present(connection):
        return
    _delete(connection, [work_id], _present(connection))
    for document in documents(connection, work_id):
        for table in _present(connection):
            _write(connection, table, document)


def _write(connection: sa.Connection, table: str, document: Document) -> None:
    connection.execute(
        sa.text(
            f"INSERT INTO {table} (entity_type, entity_id, digest, title, names) "  # noqa: S608
            "VALUES (:t, :i, :d, :title, :names)"
        ),
        {
            "t": ENTITY, "i": document.work_id, "d": document.digest,
            "title": document.title, "names": document.names,
        },
    )  # fmt: skip


def sync(connection: sa.Connection) -> int:
    """Bring the tables in line with the catalog: what changed is written again, what is gone is
    removed. The number of Works touched (written or removed)."""
    if not _present(connection):
        return 0
    current = {d.work_id: d for d in documents(connection)}
    touched = 0
    for table in _present(connection):
        known = {
            r.entity_id: r.digest
            for r in connection.execute(sa.text(f"SELECT entity_id, digest FROM {table}"))  # noqa: S608
        }
        stale = [i for i in known if i not in current]
        changed = [d for i, d in current.items() if known.get(i) != d.digest]
        _delete(connection, [*stale, *(d.work_id for d in changed)], [table])
        for document in changed:
            _write(connection, table, document)
        touched = max(touched, len(stale) + len(changed))
    return touched


def rebuild(connection: sa.Connection) -> tuple[int, SearchCapability]:
    """The whole index again, from the database alone (the tables made if this SQLite can)."""
    capability = ensure_tables(connection)
    present = _present(connection)
    for table in present:
        connection.execute(sa.text(f"DELETE FROM {table}"))  # noqa: S608
    docs = documents(connection)
    for document in docs:
        for table in present:
            _write(connection, table, document)
    return len(docs), capability
