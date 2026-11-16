"""The page map of a block against the page count Kavita announces (M4 L50, ADR-0053 §2).

A block is made of chapters, and the page of the block an e-reader sends is translated by
Shelfhold's own table of pages (`bundle_member.first_page`, `page_count`). That page can be handed
to Kavita, and Kavita's page taken back, only for a chapter where the two count the same pages: a
member is `verified` when its pages are in an order Kavita would give (`candidate`: canonical names)
**and** Kavita announces for its chapter the very number of pages Shelfhold counted. Otherwise it is
`unverified`, and nothing is pushed for it nor given back in the frame of the block.

The comparison is the structure's: Kavita's page counts come from `series-detail`, read with the
target's dedicated key (nothing of any account). It goes by file: a member whose file is no longer
the one the version was made from (replaced since) is left alone, Kavita's count is now that of
another file.

Closed codes (`CODES`) say why a chapter is not relayed; they reach the log, the pass's outcome and
the Health page, never a title, a path or an identifier of Kavita.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Protocol

import sqlalchemy as sa
import structlog

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import bundle, export
from shelfhold.reading.bundle import store
from shelfhold.reading.bundle.plan import LIVE

log = structlog.get_logger(__name__)

__all__ = ["CODES", "PageMapReport", "relayable", "verify_page_maps"]

PAGE_COUNT_MISMATCH: Final = "page_count_mismatch"  # Kavita counts another number of pages
NOT_VERIFIED: Final = "page_map_unverified"  # not verified: names Kavita might sort otherwise
CODES: Final = (PAGE_COUNT_MISMATCH, NOT_VERIFIED)


class _Chapter(Protocol):
    @property
    def carrier_id(self) -> str: ...

    @property
    def pages(self) -> int: ...


@dataclass
class PageMapReport:
    verified: int = 0
    mismatched: int = 0
    unverified: int = 0
    by_code: dict[str, int] = field(default_factory=dict)


def verify_page_maps(engine: sa.Engine, chapters: Sequence[_Chapter], key: bytes) -> PageMapReport:
    """Compare Kavita's page count of each chapter with the members that hold its file, and write
    the state: `verified` on a match, `unverified` on a difference (the count is kept), a member
    that never was a candidate stays out of it. One read per pass, one write for what changed."""
    report = PageMapReport()
    if not chapters:
        return report
    pages = {c.carrier_id: int(c.pages) for c in chapters}
    m, v, rec = bundle.bundle_member, bundle.bundle_version, export.export_record
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(
                m.c.version_id,
                m.c.rank,
                m.c.export_item_id,
                m.c.content_fp,
                m.c.page_count,
                m.c.page_map_state,
                m.c.kavita_pages,
            )
            .join(v, v.c.id == m.c.version_id)
            .where(m.c.export_item_id.in_(pages), v.c.purged_at.is_(None))
        ).all()
        files: dict[str, set[str]] = {}
        for held in connection.execute(
            sa.select(rec.c.export_item_id, rec.c.file_hash).where(
                rec.c.export_item_id.in_(pages),
                rec.c.state.in_(LIVE),
            )
        ):
            if held.file_hash is not None:
                files.setdefault(str(held.export_item_id), set()).add(
                    store.member_fp(key, str(held.file_hash))
                )
    writes: list[tuple[str, int, str, int | None]] = []
    for r in rows:
        item = str(r.export_item_id)
        if r.content_fp is None or str(r.content_fp) not in files.get(item, set()):
            # Purged, or the file was replaced: Kavita's count is another file's, and what was
            # verified of the old one says nothing of this one (it is told apart again if the file
            # comes back, by the comparison above).
            if r.page_map_state == "verified":
                kept = None if r.kavita_pages is None else int(r.kavita_pages)
                writes.append((str(r.version_id), int(r.rank), "unverified", kept))
                report.unverified += 1
            continue
        announced = pages[item]
        if r.page_map_state == "unverified" and r.kavita_pages is None:
            report.unverified += 1  # never a candidate: never compared
            report.by_code[NOT_VERIFIED] = report.by_code.get(NOT_VERIFIED, 0) + 1
            continue
        state = "verified" if announced == int(r.page_count) else "unverified"
        if state == "verified":
            report.verified += 1
        else:
            report.mismatched += 1
            report.by_code[PAGE_COUNT_MISMATCH] = report.by_code.get(PAGE_COUNT_MISMATCH, 0) + 1
        if state != r.page_map_state or announced != r.kavita_pages:
            writes.append((str(r.version_id), int(r.rank), state, announced))
    if writes:
        with write_transaction(engine) as connection:
            for version_id, rank, state, counted in writes:
                connection.execute(
                    sa.update(m)
                    .where(m.c.version_id == version_id, m.c.rank == rank)
                    .values(page_map_state=state, kavita_pages=counted)
                )
    if report.mismatched:
        log.info("progress_sync.page_map", code=PAGE_COUNT_MISMATCH, chapters=report.mismatched)
    return report


def relayable(connection: sa.Connection, version_id: object, carrier_id: str) -> bool:
    """Whether the chapter's member of that version is `verified`: the only state whose page the
    relay hands to Kavita. No member, or another state: no."""
    m = bundle.bundle_member
    state = connection.execute(
        sa.select(m.c.page_map_state).where(
            m.c.version_id == str(version_id), m.c.export_item_id == carrier_id
        )
    ).scalar()
    return state == "verified"
