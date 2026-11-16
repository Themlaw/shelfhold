"""Fan-out: one shelf, many bindings (M4 L06a, EF-EXP-002, EF-EXP-047, EF-BIB-044, ADR-0052).

A binding is a folder and a server. Two bindings on two folders are two **trees**, each written by
its own primary; a server that indexes a tree it did not make is a **secondary** (`tree_indexer`):
it never writes (T3) and holds a *copy* of the file state of the primary, written in the same
transaction as the primary's line (T9). A tree is one copy (T11): the replica is told once, by the
primary, so a tree with three indexers is not three copies.

What this module keeps, in one place:

- **The tree of a binding** (`ensure_tree`, `attach_secondary`): created with the binding, never
  guessed later; a server attached to a folder that is not the tree's, to a tree of another shelf
  or twice, is `target.tree_overlap`.
- **The barrier, per binding** (`refusal_of`): a shelf below 18+ is looked at again for *each*
  binding at every planning, never inherited from the shelf. The weakest indexer of a tree decides
  for the whole tree (T10): writing a file is delivering it to every indexer, the one whose
  resolution is paused included.
- **The state of a tree** (`mirror_to_indexers`, `record_applied`): the manifest is per tree, the
  copies are the same transaction as the line they copy.
- **The pass** (`FanOut.run`): each binding gets *its own* planned content (the Editions admitted
  on the shelf of that binding), its plan runs through the executor of L04, and one that fails
  marks nothing on the others.

The bytes never come from a plan and never from another binding's rendering: `planned_content` is
the only thing that fills the ledger, and `HostContentStore` reads them from the registry of copies
(M4 L06b: `/store`, the library, a verified file of the shelf; the format is the first of the
server's `accepted_content_types` the Variant has).
"""

import hashlib
import os
import stat
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, BinaryIO, Literal, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.catalog.public import Admission, RatingService
from shelfhold.export.application.plan_executor import (
    ExecutionReport,
    PlanBinding,
    PlannedLedger,
    PlanRefusedError,
)
from shelfhold.export.application.rendering import (
    RenderMismatchError,
    comicinfo_xml,
    first_page,
    render_cbz_bytes,
    series_of_item,
)
from shelfhold.export.application.target_state import DestinationPausedError
from shelfhold.export.domain.bindings import (
    PARTITIONED,
    TreeOverlapError,
    effective_partitioning,
    is_family,
)
from shelfhold.export.domain.kavita_compat import blocks_family_shelf
from shelfhold.export.domain.native_folder import cover_item_of
from shelfhold.export.domain.path_map import clean_absolute, within
from shelfhold.export.domain.planner import NO_NATIVE_FORMAT, ContentOffer, choose_content
from shelfhold.infrastructure.clock import Clock, SystemClock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fsops import FsopsError, open_regular
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export, platform, storage
from shelfhold.library.public import (
    byte_sources,
    forget_destination_copies,
    record_replica,
    relocate_root_paths,
    variant_contents,
)
from shelfhold.sdk.api_call import ApiCall
from shelfhold.sdk.export_target import TargetCapabilities, copy_can_verify
from shelfhold.sdk.fsops import ContentRef, PlannedContent

__all__ = [
    "AppliedFile",
    "BindingOutcome",
    "BindingView",
    "CachedInstanceProbe",
    "ContentNeedsDownloadError",
    "ContentRefusedError",
    "DestinationSource",
    "EpubFile",
    "FanOut",
    "FanOutReport",
    "HostContentStore",
    "NoNativeFormatError",
    "TreeBarrier",
    "attach_secondary",
    "bindings_of",
    "ensure_tree",
    "ensure_trees",
    "indexers_of",
    "is_secondary",
    "mark_indexers_missing",
    "mark_indexers_recycled",
    "mirror_to_indexers",
    "native_formats",
    "planned_content",
    "record_applied",
    "refusal_of",
    "register_destination_copies",
    "tree_mates",
]

log = structlog.get_logger()

# What a line says about the **file**: copied from the primary to every indexer (T9).
FILE_COLUMNS = (
    "locator_type", "rel_path", "template_ref", "format", "size", "mtime_ns", "content_hash",
    "sidecar_sha256", "file_hash", "spine_count", "written_by_shelfhold_at", "exported_at",
)  # fmt: skip
# What an indexer says about **its** view of the file: kept while the file itself is fine.
INDEXER_STATES = ("scanned", "resolved", "error")
FILE_OK = ("written", "scanned", "resolved")
CODE_UNPARTITIONED = "shelf.destination_not_partitioned"
CODE_CHILD_EXPOSURE = "target.child_exposure"


# -- the tree of a binding ------------------------------------------------------------------


@dataclass(frozen=True)
class BindingView:
    destination_id: str
    target_id: str
    transport: str
    tree_id: str | None
    role: str | None  # `primary` or `secondary`; None for a binding with no tree (pull, dropbox)
    partitioning: str  # of the binding's own server, as it counts today


def bindings_of(connection: Connection, shelf_id: str) -> list[BindingView]:
    d, t, i = export.shelf_destination, export.export_target, export.tree_indexer
    rows = connection.execute(
        sa.select(
            d.c.id, d.c.target_id, d.c.transport, t.c.kind, t.c.access_partitioning,
            t.c.access_partitioning_attested_at, i.c.tree_id, i.c.role,
        )
        .select_from(
            d.join(t, t.c.id == d.c.target_id).outerjoin(i, i.c.shelf_destination_id == d.c.id)
        )
        .where(d.c.shelf_id == shelf_id)
        .order_by(d.c.id)
    )  # fmt: skip
    return [
        BindingView(
            r.id, r.target_id, r.transport, r.tree_id, r.role,
            effective_partitioning(
                r.kind,
                r.access_partitioning,
                attested=r.access_partitioning_attested_at is not None,
            ),
        )
        for r in rows
    ]  # fmt: skip


def locate_root(connection: Connection, local: str) -> tuple[str, str] | None:
    """`(root_id, subpath)` of a folder under a physical root: the longest root that holds it."""
    path = clean_absolute(local)
    best: tuple[int, str, str] | None = None
    for row in connection.execute(
        sa.select(platform.physical_root.c.root_id, platform.physical_root.c.path_entry)
    ):
        rest = within(path, clean_absolute(row.path_entry.rstrip("/") or "/"))
        if rest is not None and (best is None or len(row.path_entry) > best[0]):
            best = (len(row.path_entry), row.root_id, rest.strip("/"))
    return None if best is None else (best[1], best[2])


def ensure_trees(connection: Connection, shelf_id: str) -> list[BindingView]:
    """The bindings of a shelf, each `tree` binding with its tree: one made before trees existed
    gets it here, once, from the folder it writes (idempotent)."""
    views = bindings_of(connection, shelf_id)
    if all(v.tree_id is not None or v.transport != "tree" for v in views):
        return views
    d, t = export.shelf_destination, export.export_target
    for view in views:
        if view.tree_id is not None or view.transport != "tree":
            continue
        row = connection.execute(
            sa.select(d.c.root_subpath, t.c.path_map)
            .select_from(d.join(t, t.c.id == d.c.target_id))
            .where(d.c.id == view.destination_id)
        ).one()
        writable = [e for e in row.path_map if e.get("mode", "rw") == "rw"]
        if not writable:
            continue
        base = writable[0]["local"]
        root = clean_absolute(f"{base}/{row.root_subpath}" if row.root_subpath else base)
        try:
            with connection.begin_nested():
                ensure_tree(
                    connection, destination_id=view.destination_id, target_id=view.target_id,
                    local_root=os.path.realpath(root),
                )  # fmt: skip
        except TreeOverlapError:
            # a folder that is already a tree, for a binding that was never attached: left with no
            # tree (nothing writes through it) rather than failing the shelf for every caller
            log.warning("fan_out.tree_overlap", destination_id=view.destination_id)
    return bindings_of(connection, shelf_id)


def tree_of(connection: Connection, destination_id: str) -> str | None:
    return connection.execute(
        sa.select(export.tree_indexer.c.tree_id).where(
            export.tree_indexer.c.shelf_destination_id == destination_id
        )
    ).scalar()


def ensure_tree(
    connection: Connection, *, destination_id: str, target_id: str, local_root: str | None
) -> str | None:
    """The tree a `tree` binding writes, made with it (its primary indexer). Idempotent; a folder
    that no registered root holds has no tree (nothing writes there either)."""
    found = tree_of(connection, destination_id)
    if found is not None or local_root is None:
        return found
    located = locate_root(connection, local_root)
    if located is None:
        return None
    root_id, subpath = located
    tree = export.tree
    tree_id = connection.execute(
        sa.select(tree.c.id).where(tree.c.root_id == root_id, tree.c.root_subpath == subpath)
    ).scalar()
    if tree_id is not None:  # the folder is already a tree: a binding is attached on purpose
        raise TreeOverlapError(f"{local_root} is the folder of a tree: attach to it")
    tree_id = new_ulid()
    connection.execute(sa.insert(tree).values(id=tree_id, root_id=root_id, root_subpath=subpath))
    connection.execute(
        sa.insert(export.tree_indexer).values(
            tree_id=tree_id, shelf_destination_id=destination_id, target_id=target_id,
            role="primary",
        )
    )  # fmt: skip
    return tree_id


def tree_place(connection: Connection, tree_id: str) -> tuple[str, str, str] | None:
    """`(root_id, subpath, shelf_id)` of a tree, the shelf being the one of its primary."""
    tree, indexer, d = export.tree, export.tree_indexer, export.shelf_destination
    row = connection.execute(
        sa.select(tree.c.root_id, tree.c.root_subpath, d.c.shelf_id)
        .select_from(
            tree.join(indexer, indexer.c.tree_id == tree.c.id).join(
                d, d.c.id == indexer.c.shelf_destination_id
            )
        )
        .where(tree.c.id == tree_id, indexer.c.role == "primary")
    ).first()
    return None if row is None else (row.root_id, row.root_subpath, row.shelf_id)


def attach_secondary(
    connection: Connection, *, tree_id: str, destination_id: str, target_id: str
) -> None:
    """A server that indexes a tree it did not make. The shelf is the tree's (T2) and a server
    indexes a tree once (T9): the checks of the schema answer here with the code of the overlap."""
    try:
        with connection.begin_nested():
            connection.execute(
                sa.insert(export.tree_indexer).values(
                    tree_id=tree_id, shelf_destination_id=destination_id, target_id=target_id,
                    role="secondary",
                )
            )  # fmt: skip
    except sa.exc.IntegrityError:
        raise TreeOverlapError(
            "a server indexes a tree once, and only the tree of its shelf"
        ) from None
    primary = connection.execute(
        sa.select(export.tree_indexer.c.shelf_destination_id).where(
            export.tree_indexer.c.tree_id == tree_id, export.tree_indexer.c.role == "primary"
        )
    ).scalar_one()
    for item in (
        connection.execute(
            sa.select(export.export_record.c.export_item_id).where(
                export.export_record.c.shelf_destination_id == primary
            )
        )
        .scalars()
        .all()
    ):  # what the tree already holds is what the new indexer is told
        mirror_to_indexers(connection, primary, item)


def indexers_of(connection: Connection, destination_id: str) -> list[str]:
    """The other bindings that index the tree of this one."""
    indexer = export.tree_indexer
    tree_id = tree_of(connection, destination_id)
    if tree_id is None:
        return []
    return list(
        connection.execute(
            sa.select(indexer.c.shelf_destination_id).where(
                indexer.c.tree_id == tree_id, indexer.c.shelf_destination_id != destination_id
            )
        ).scalars()
    )


def mark_indexers_missing(
    connection: Connection, destination_id: str, export_item_id: str, state: str = "missing"
) -> None:
    """A file that left the tree is gone for every indexer: their copy says so (T9) instead of
    keeping a `written` line for a folder that is empty."""
    if is_secondary(connection, destination_id):
        return
    record = export.export_record
    others = indexers_of(connection, destination_id)
    if others:
        connection.execute(
            sa.update(record)
            .where(
                record.c.shelf_destination_id.in_(others),
                record.c.export_item_id == export_item_id,
                record.c.state.in_(FILE_OK),
            )
            .values(state=state)
        )


def mark_indexers_recycled(
    connection: Connection, destination_id: str, export_item_id: str
) -> None:
    """The same, for a file moved to the bin by a withdrawal: the copies say `recycled`."""
    mark_indexers_missing(connection, destination_id, export_item_id, "recycled")


def is_secondary(connection: Connection, destination_id: str) -> bool:
    return (
        connection.execute(
            sa.select(export.tree_indexer.c.role).where(
                export.tree_indexer.c.shelf_destination_id == destination_id
            )
        ).scalar()
        == "secondary"
    )


# -- the barrier, per binding ---------------------------------------------------------------


@dataclass(frozen=True)
class Mate:
    destination_id: str
    target_id: str
    role: str | None
    partitioning: str
    version_seen: str | None


def tree_mates(connection: Connection, destination_id: str) -> list[Mate]:
    """The binding itself, then the other indexers of its tree: each counts for the whole tree."""
    d, t, i = export.shelf_destination, export.export_target, export.tree_indexer
    own_tree = tree_of(connection, destination_id)
    where = (
        d.c.id == destination_id
        if own_tree is None
        else sa.or_(d.c.id == destination_id, i.c.tree_id == own_tree)
    )
    rows = connection.execute(
        sa.select(
            d.c.id, d.c.target_id, i.c.role, t.c.kind, t.c.access_partitioning,
            t.c.access_partitioning_attested_at, t.c.version_seen,
        )
        .select_from(
            d.join(t, t.c.id == d.c.target_id).outerjoin(i, i.c.shelf_destination_id == d.c.id)
        )
        .where(where)
        .order_by((d.c.id != destination_id), d.c.id)
    ).all()  # fmt: skip
    return [
        Mate(
            r.id, r.target_id, r.role,
            effective_partitioning(
                r.kind,
                r.access_partitioning,
                attested=r.access_partitioning_attested_at is not None,
            ),
            r.version_seen,
        )
        for r in rows
    ]  # fmt: skip


def weakest_partitioning(mates: Sequence[Mate]) -> str:
    """The server that keeps accounts apart worst decides for the tree (T10)."""
    for mate in mates:
        if mate.partitioning not in PARTITIONED:
            return mate.partitioning
    return mates[0].partitioning


def tree_known_bad(mates: Sequence[Mate]) -> bool:
    return any(blocks_family_shelf(mate.version_seen) for mate in mates)


def refusal_of(connection: Connection, destination_id: str) -> str | None:
    """Why nothing may be planned for this binding at all, or None: looked at again at every
    planning, for this binding and for every indexer of its tree. Only a shelf below 18+ has
    anything to ask."""
    ceiling = connection.execute(
        sa.select(export.shelf.c.max_age_rating)
        .select_from(
            export.shelf.join(
                export.shelf_destination, export.shelf_destination.c.shelf_id == export.shelf.c.id
            )
        )
        .where(export.shelf_destination.c.id == destination_id)
    ).scalar_one()
    if not is_family(ceiling):
        return None
    mates = tree_mates(connection, destination_id)
    if weakest_partitioning(mates) not in PARTITIONED:
        return CODE_UNPARTITIONED
    if tree_known_bad(mates):
        return CODE_CHILD_EXPOSURE
    return None


# -- the state of a tree --------------------------------------------------------------------


def mirror_to_indexers(connection: Connection, destination_id: str, export_item_id: str) -> int:
    """T9: the line of the primary, copied to every secondary of its tree, in the caller's
    transaction. A secondary's own view (`scanned`, `resolved`, `error`, its remote reference)
    survives while the file is fine; anything the file itself says wins. A line of a secondary is
    never the source: it copies nothing."""
    indexer, record = export.tree_indexer, export.export_record
    mine = connection.execute(
        sa.select(indexer.c.tree_id, indexer.c.role).where(
            indexer.c.shelf_destination_id == destination_id
        )
    ).first()
    if mine is None or mine.role != "primary":
        return 0
    source = connection.execute(
        sa.select(record).where(
            record.c.shelf_destination_id == destination_id,
            record.c.export_item_id == export_item_id,
        )
    ).first()
    if source is None:
        return 0
    copied = 0
    for secondary in (
        connection.execute(
            sa.select(indexer.c.shelf_destination_id).where(
                indexer.c.tree_id == mine.tree_id, indexer.c.role == "secondary"
            )
        )
        .scalars()
        .all()
    ):
        own = connection.execute(
            sa.select(record.c.state).where(
                record.c.shelf_destination_id == secondary,
                record.c.export_item_id == export_item_id,
            )
        ).first()
        values: dict[str, Any] = {name: getattr(source, name) for name in FILE_COLUMNS}
        keeps_own = own is not None and own.state in INDEXER_STATES and source.state in FILE_OK
        if not keeps_own:
            values["state"] = source.state
            values["error_code"] = source.error_code
        if own is None:
            connection.execute(
                sa.insert(record).values(
                    shelf_destination_id=secondary, export_item_id=export_item_id, **values
                )
            )
        else:
            connection.execute(
                sa.update(record)
                .where(
                    record.c.shelf_destination_id == secondary,
                    record.c.export_item_id == export_item_id,
                )
                .values(**values)
            )
        copied += 1
    return copied


@dataclass(frozen=True)
class AppliedFile:
    export_item_id: str
    rel_path: str
    file_hash: str
    size: int
    mtime_ns: int
    dev: int | None = None
    ino: int | None = None


_FORMATS = {"cbz": "cbz", "epub": "epub", "pdf": "pdf"}


def record_applied(
    connection: Connection,
    *,
    destination_id: str,
    files: Sequence[AppliedFile],
    now: datetime,
    moved: Sequence[tuple[str, str]] = (),
    recycled: Sequence[str] = (),
) -> None:
    """The manifest of a binding after its plan ran, in plan order of effect: the files the plan
    renamed follow their new name, the ones it sent to the bin are `missing`, then one line per
    file written, whatever it said before (the line says what the disk holds), then the copy to
    every indexer of the tree, in this transaction. Idempotent: a pass run again after a crash puts
    back what the crash kept from the manifest.

    The `destination` replica of these files is `register_destination_copies`, called by the same
    transaction of the pass (`FanOut._keep`): this function is the manifest and nothing else."""
    record = export.export_record
    touched: set[str] = set()
    for source, target in moved:
        for item in _items_at(connection, destination_id, source):
            connection.execute(
                sa.update(record)
                .where(
                    record.c.shelf_destination_id == destination_id,
                    record.c.export_item_id == item,
                )
                .values(rel_path=target)
            )
            touched.add(item)
    for path in recycled:
        for item in _items_at(connection, destination_id, path):
            connection.execute(
                sa.update(record)
                .where(
                    record.c.shelf_destination_id == destination_id,
                    record.c.export_item_id == item,
                    record.c.state.in_(FILE_OK),
                )
                .values(state="missing")
            )
            touched.add(item)
    for file in files:
        values: dict[str, Any] = {
            "locator_type": "path", "rel_path": file.rel_path,
            "format": _FORMATS.get(file.rel_path.rsplit(".", 1)[-1].lower()),
            "size": file.size, "mtime_ns": file.mtime_ns, "content_hash": file.file_hash,
            "file_hash": file.file_hash, "written_by_shelfhold_at": now, "exported_at": now,
            "state": "written", "error_code": None,
        }  # fmt: skip
        found = connection.execute(
            sa.select(record.c.state).where(
                record.c.shelf_destination_id == destination_id,
                record.c.export_item_id == file.export_item_id,
            )
        ).first()
        if found is None:
            connection.execute(
                sa.insert(record).values(
                    shelf_destination_id=destination_id,
                    export_item_id=file.export_item_id,
                    **values,
                )
            )
        else:
            connection.execute(
                sa.update(record)
                .where(
                    record.c.shelf_destination_id == destination_id,
                    record.c.export_item_id == file.export_item_id,
                )
                .values(**values)
            )
        touched.add(file.export_item_id)
    for item in sorted(touched):
        mirror_to_indexers(connection, destination_id, item)


def register_destination_copies(
    connection: Connection,
    *,
    destination_id: str,
    files: Sequence[AppliedFile],
    now: datetime,
    moved: Sequence[tuple[str, str]] = (),
    recycled: Sequence[str] = (),
) -> int:
    """The `destination` copies of the files a plan wrote (point m6 of #303), in the transaction of
    the manifest. Only a file that is the archive itself is a copy the registry knows: its hash is
    the `original_sha256` of a `content` of the Unit. A rendering is a file of the manifest and
    nothing more: it is never a source of bytes for another binding (EF-BIB-048). Never counted
    as independent (a server may rewrite it): `independent_copies` is unchanged. A stale row of the
    item under the same tree is replaced, also when the new file is a rendering; the rows of
    another tree are not touched. The registry follows what the plan did to files: a `rename` moves
    the row, a `recycle` forgets it (the file is in the bin: no row says it is here)."""
    tree_id = tree_of(connection, destination_id)
    place = None if tree_id is None else tree_place(connection, tree_id)
    if place is None:
        return 0
    root_id, subpath, _shelf = place

    def in_root(rel: str) -> str:
        return f"{subpath}/{rel}" if subpath else rel

    relocate_root_paths(connection, root_id, [(in_root(a), in_root(b)) for a, b in moved])
    for path in recycled:
        for item in _items_at(connection, destination_id, path):
            forget_destination_copies(connection, item, physical_root_id=root_id, under=subpath)
    made = 0
    for file in files:
        unit = _unit_of(connection, file.export_item_id)
        if unit is None:
            continue
        forget_destination_copies(
            connection, file.export_item_id, physical_root_id=root_id, under=subpath
        )
        content = _canonical_content(connection, unit, file.file_hash)
        if content is None:
            continue  # a rendering: in the manifest, never in the registry as a source
        record_replica(
            connection, unit_id=unit, export_item_id=file.export_item_id, location="destination",
            content_hash=content, file_hash=file.file_hash, independent=False,
            mutable_by_third_party=True, physical_root_id=root_id, rel_path=in_root(file.rel_path),
            dev=file.dev, ino=file.ino, size=file.size, mtime_ns=file.mtime_ns, verified_at=now,
        )  # fmt: skip
        made += 1
    return made


def _canonical_content(connection: Connection, unit_id: str, file_hash: str) -> str | None:
    """The payload hash of the `content` of the Unit whose archive `file_hash` is, if any."""
    content = storage.content
    return connection.execute(
        sa.select(content.c.content_hash).where(
            content.c.unit_id == unit_id,
            content.c.original_sha256 == file_hash,
            content.c.content_hash.is_not(None),
        )
    ).scalar()


def _items_at(connection: Connection, destination_id: str, rel_path: str) -> list[str]:
    record = export.export_record
    return list(
        connection.execute(
            sa.select(record.c.export_item_id).where(
                record.c.shelf_destination_id == destination_id, record.c.rel_path == rel_path
            )
        ).scalars()
    )


# -- what a binding may be given ------------------------------------------------------------


def planned_content(
    connection: Connection, destination_id: str, accepted: Sequence[str] | None = None
) -> PlannedContent:
    """The content that may be cited for *this* binding: the export items of the Editions filed
    on the shelf of the binding and, on a shelf below 18+, admitted by the same rule that files
    them (rated, under the ceiling, not suggested above it). The egress is the one the instance
    attested for the binding, never what the server declares. Nothing of another shelf is here
    (point m5 of #299): a plan that cites it is refused by the executor. With `accepted` (the
    `accepted_content_types` of its server), an item with no content in a format the server takes is
    not here either (EF-EXP-046)."""
    d, shelf = export.shelf_destination, export.shelf
    row = connection.execute(
        sa.select(d.c.shelf_id, d.c.egress, shelf.c.max_age_rating)
        .select_from(d.join(shelf, shelf.c.id == d.c.shelf_id))
        .where(d.c.id == destination_id)
    ).one()
    item, edition = export.export_item, catalog.edition
    rows = connection.execute(
        sa.select(item.c.id, item.c.edition_id, storage.content.c.watermarked)
        .select_from(
            item.join(edition, edition.c.id == item.c.edition_id).outerjoin(
                storage.content, storage.content.c.id == item.c.content_id
            )
        )
        .where(edition.c.shelf_id == row.shelf_id)
        .order_by(item.c.id)
    ).all()
    family = is_family(row.max_age_rating)
    rating = RatingService()
    verdicts: dict[str, bool] = {}
    refs, watermarked = set(), set()
    for r in rows:
        if family:
            if r.edition_id not in verdicts:
                verdicts[r.edition_id] = (
                    rating.admission_of(connection, r.edition_id, shelf_id=row.shelf_id)
                    is Admission.ALLOWED
                )
            if not verdicts[r.edition_id]:
                continue
        refs.add(r.id)
        if r.watermarked:
            watermarked.add(r.id)
    if accepted is not None:
        refs &= set(native_formats(connection, sorted(refs), accepted))
        watermarked &= refs
    return PlannedContent(
        planned_refs=frozenset(refs),
        egress="externe" if row.egress == "externe" else "foyer",
        watermarked_refs=frozenset(watermarked),
    )


def _unit_of(connection: Connection, export_item_id: str) -> str | None:
    coverage = export.unit_coverage
    return connection.execute(
        sa.select(coverage.c.unit_id)
        .where(coverage.c.carrier_type == "export_item", coverage.c.carrier_id == export_item_id)
        .order_by(coverage.c.position)
        .limit(1)
    ).scalar()


def _offers(connection: Connection, unit_id: str) -> dict[str, ContentOffer]:
    return {
        c.content_id: ContentOffer(c.content_id, c.format, c.media_type)
        for c in variant_contents(connection, unit_id)
    }


def native_formats(
    connection: Connection, export_item_ids: Sequence[str], accepted: Sequence[str]
) -> dict[str, str]:
    """For each item the format of the first of `accepted` its Variant has (EF-EXP-046): the one
    the binding is served, whatever the other bindings of the shelf are served. An item with no
    native format is left out (the `Rendition` of M5 is reserved): it cannot be planned for this
    binding, and the others are not touched. Deterministic: `choose_content` decides."""
    found: dict[str, str] = {}
    for item_id in export_item_ids:
        unit = _unit_of(connection, item_id)
        if unit is None:
            continue
        chosen = choose_content(accepted, _offers(connection, unit).values())
        if chosen is not None:
            found[item_id] = chosen.format
    return found


# -- the pass --------------------------------------------------------------------------------


class DestinationSource(Protocol):
    """What the destinations describe, per binding: their capabilities, the plan of files and
    the calls, and the way a call leaves (the Fetcher of the target). A binding the source knows
    nothing about is left alone."""

    def capabilities(self, target_id: str) -> TargetCapabilities | None: ...

    def plan(self, binding: PlanBinding) -> object | None: ...

    def calls(self, binding: PlanBinding) -> Sequence[object]: ...

    def sender(self, binding: PlanBinding) -> Callable[[ApiCall], object]: ...


class _Pipeline(Protocol):
    plan_ledger: PlannedLedger

    def execute_destination_plan(
        self, binding: PlanBinding, call_id: str, raw_plan: object
    ) -> ExecutionReport: ...

    def send_destination_calls(
        self,
        binding: PlanBinding,
        call_id: str,
        calls: Sequence[object],
        *,
        send: Callable[[ApiCall], object],
    ) -> int: ...


Status = Literal["done", "refused", "failed", "paused", "indexer", "skipped"]


@dataclass(frozen=True)
class BindingOutcome:
    destination_id: str
    status: Status
    code: str | None = None
    executed: int = 0
    # items already written whose format the server no longer accepts (`export.no_native_format`):
    # left where they are and said here, never recycled by the host on its own (m4 of #305)
    lost_format: tuple[str, ...] = ()


@dataclass(frozen=True)
class FanOutReport:
    outcomes: tuple[BindingOutcome, ...]


def _lost_format(
    connection: Connection, destination_id: str, formats: dict[str, str]
) -> tuple[str, ...]:
    """The items of the manifest of the binding, written and still on the shelf, that no longer
    have a format the server accepts: the file is there and nothing here removes it."""
    record = export.export_record
    held = set(
        connection.execute(
            sa.select(record.c.export_item_id).where(
                record.c.shelf_destination_id == destination_id, record.c.state.in_(FILE_OK)
            )
        ).scalars()
    )
    on_shelf = planned_content(connection, destination_id).planned_refs
    return tuple(sorted((held & on_shelf) - set(formats)))


class FanOut:
    def __init__(
        self,
        engine: Engine,
        pipeline: _Pipeline,
        source: DestinationSource,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._engine = engine
        self._pipeline = pipeline
        self._source = source
        self._clock = clock or SystemClock()

    def run(self, shelf_id: str, call_id: str) -> FanOutReport:
        """One pass over the bindings of a shelf, in a fixed order. Each binding is judged alone:
        a refusal, a pause or a failure of one marks nothing on the others."""
        with write_transaction(self._engine) as connection:
            views = ensure_trees(connection, shelf_id)
        try:
            return FanOutReport(tuple(self._one(view, call_id) for view in views))
        finally:
            self._pipeline.plan_ledger.release(call_id)

    def _one(self, view: BindingView, call_id: str) -> BindingOutcome:
        destination_id = view.destination_id
        try:
            with self._engine.connect() as connection:
                refusal = refusal_of(connection, destination_id)
            if refusal is not None:
                log.info("fan_out.refused", destination_id=destination_id, code=refusal)
                return BindingOutcome(destination_id, "refused", refusal)
            capabilities = self._source.capabilities(view.target_id)
            if capabilities is None:
                return BindingOutcome(destination_id, "skipped", "no_destination")
            accepted = capabilities.accepted_content_types
            with self._engine.connect() as connection:
                planned = planned_content(connection, destination_id, accepted)
                formats = native_formats(connection, sorted(planned.planned_refs), accepted)
                lost = _lost_format(connection, destination_id, formats)
            if lost:
                log.warning(
                    "fan_out.no_native_format", destination_id=destination_id, items=len(lost),
                    code=NO_NATIVE_FORMAT,
                )  # fmt: skip
            binding = PlanBinding(destination_id, view.target_id, capabilities, formats)
            made = getattr(self._source, "sidecars", None)  # the annexes the host makes (a cover)
            self._pipeline.plan_ledger.plan(
                call_id, destination_id, planned,
                sidecars=frozenset(made(binding)) if made is not None else frozenset(),
            )  # fmt: skip
            executed = 0
            if view.role != "secondary":  # an indexer is never asked to write (T3)
                raw = self._source.plan(binding)
                if raw is not None:
                    report = self._pipeline.execute_destination_plan(binding, call_id, raw)
                    executed = report.executed
                    self._keep(destination_id, report)
            calls = self._source.calls(binding)
            if calls:
                self._pipeline.send_destination_calls(
                    binding, call_id, calls, send=self._source.sender(binding)
                )
            return BindingOutcome(
                destination_id, "indexer" if view.role == "secondary" else "done", None, executed,
                lost,
            )  # fmt: skip
        except DestinationPausedError as error:
            return BindingOutcome(destination_id, "paused", str(error))
        except PlanRefusedError as error:
            log.info("fan_out.failed", destination_id=destination_id, reason=error.reason)
            return BindingOutcome(destination_id, "failed", error.code)
        except (FsopsError, OSError) as error:
            log.warning("fan_out.disk", destination_id=destination_id, reason=type(error).__name__)
            return BindingOutcome(destination_id, "failed", "export.write_failed")

    def _keep(self, destination_id: str, report: ExecutionReport) -> None:
        """The manifest of the tree, and the copy to its indexers, in one transaction."""
        files = [
            AppliedFile(w.export_item_id, w.rel_path, w.file_hash, w.size, w.mtime_ns, w.dev, w.ino)
            for w in report.files
        ]
        with write_transaction(self._engine) as connection:
            record_applied(
                connection,
                destination_id=destination_id,
                files=files,
                now=self._clock.now(),
                moved=report.moved,
                recycled=report.recycled,
            )
            register_destination_copies(
                connection, destination_id=destination_id, files=files, now=self._clock.now(),
                moved=report.moved, recycled=report.recycled,
            )  # fmt: skip


# -- what the composition root gives the executor -----------------------------------------------


class ContentNeedsDownloadError(PlanRefusedError):
    """No copy of the archive can be read and verified: it is to be downloaded again (EF-BIB-045).
    Nothing is served and nothing is invented."""

    code = "export.source_missing"


class NoNativeFormatError(PlanRefusedError):
    """No `content` of the Variant is in a format the server of the binding accepts."""

    code = NO_NATIVE_FORMAT


DEFAULT_ACCEPTED = ("cbz", "epub")


MAX_READ = 4 << 30  # the most the host reads of a file whose size the registry does not hold
CHUNK = 1 << 20


class ContentRefusedError(PlanRefusedError):
    """A file is not what the registry says it is (not a regular file, another size, too big):
    it is no source, and nothing of it is read past what the check needs."""

    code = "export.source_unreadable"


@dataclass(frozen=True)
class EpubFile:
    """The EPUB `servable_epub` verified: the path, the hash it was verified at and the `content`
    it is the derivative of. The host reads it once and compares, never trusting the path."""

    path: str
    file_hash: str
    content_id: str


def _read_bounded(stream: BinaryIO, *, size: int | None) -> tuple[bytes, str]:
    """The bytes of the open descriptor and their hash, computed while reading. A regular file of
    the size the registry holds (or under `MAX_READ`): any other is refused before the first read
    and no more than that size is ever read, whatever the file grows to."""
    seen = os.fstat(stream.fileno())
    if not stat.S_ISREG(seen.st_mode):
        raise ContentRefusedError("not a regular file")
    cap = MAX_READ if size is None else size
    if seen.st_size > cap or (size is not None and seen.st_size != size):
        raise ContentRefusedError("not the size the registry holds")
    digest, data = hashlib.sha256(), bytearray()
    while chunk := stream.read(min(CHUNK, cap + 1 - len(data))):
        digest.update(chunk)
        data += chunk
        if len(data) > cap:
            raise ContentRefusedError("larger than it was")
    return bytes(data), "sha256:" + digest.hexdigest()


class HostContentStore:
    """The bytes the host owns for an export item. Only what the ledger planned is ever asked for;
    anything else (a sidecar nobody made, a volume) is refused here as well.

    The archive comes from `byte_sources` (EF-BIB-045): `/store`, the library, then a file a
    binding of the shelf wrote when it is the archive itself, its hash is the manifest's and its
    server can read a copy back (`readback` is not `none`). Each candidate is read and hashed
    before use; the first that is what it should be serves. None: a new download is asked for
    (`download`) and nothing is served. The rendering of another destination is never a source, so
    each binding's file is made again from the canonical archive (EF-BIB-048).

    The format is the first of the server's `accepted_content_types` the Variant has (EF-EXP-046);
    an EPUB only by the file `epub_path` names (its verified derivative, EF-BIB-051), never one
    of the copies above."""

    def __init__(
        self,
        engine: Engine,
        *,
        capabilities: Callable[[str], TargetCapabilities | None] | None = None,
        download: Callable[[str], None] | None = None,
        epub_path: Callable[[str], EpubFile | None] | None = None,
        fsops: Any = None,
        renders: Callable[[str], bool] | None = None,
    ) -> None:
        # the targets whose CBZ is given a ComicInfo made for the binding (a native folder)
        self._renders = renders
        self._engine = engine
        self._capabilities = capabilities
        self._download = download
        self._epub_path = epub_path
        self._fsops = fsops  # the confined facade, when the composition root has one

    @contextmanager
    def _open(self, path: str) -> Iterator[BinaryIO]:
        if self._fsops is None:
            with open_regular(path) as stream:
                yield stream
        else:
            with self._fsops.open_read(path) as stream:  # confined, no link, regular file
                yield stream

    def _caps(self, target_id: str) -> TargetCapabilities | None:
        return None if self._capabilities is None else self._capabilities(target_id)

    def read(self, destination_id: str, ref: ContentRef) -> bytes:
        if ref.kind == "sidecar":
            return self._cover(destination_id, ref)
        if ref.kind != "export_item":
            raise PlanRefusedError("the host serves no such content here")
        data, fmt, target_id = self._archive(destination_id, ref.ref)
        if fmt == "cbz" and self._renders is not None and self._renders(target_id):
            return self._with_comicinfo(ref.ref, data)  # a ComicInfo made for this binding
        return data

    def _with_comicinfo(self, item_id: str, data: bytes) -> bytes:
        with self._engine.connect() as connection:
            unit = _unit_of(connection, item_id)
            if unit is None:
                raise PlanRefusedError("the host holds no bytes for this item")
            xml = comicinfo_xml(connection, unit, series_of_item(connection, item_id))
        try:
            return render_cbz_bytes(data, xml)
        except (RenderMismatchError, ValueError, OSError) as error:
            raise ContentRefusedError("the archive cannot be given a ComicInfo") from error

    def _cover(self, destination_id: str, ref: ContentRef) -> bytes:
        """The cover of a series for a binding that makes one: the first page of the item the
        reference names, which must be planned for this binding like any other (same gates)."""
        item_id = cover_item_of(ref.ref)
        if item_id is None:
            raise PlanRefusedError("the host serves no such content here")
        data, fmt, _target = self._archive(destination_id, item_id)
        if fmt != "cbz":
            raise PlanRefusedError("only a CBZ gives a cover")
        try:
            return first_page(data)
        except (ValueError, OSError) as error:
            raise ContentRefusedError("the archive has no page to make a cover of") from error

    def _archive(self, destination_id: str, item_id: str) -> tuple[bytes, str, str]:
        """The canonical bytes of an item in the format the binding is served, with that format
        and the target of the binding. Only what the binding was planned (its shelf, the barrier
        of a family shelf), whatever a plan cites."""
        ref = ContentRef(kind="export_item", ref=item_id)
        d = export.shelf_destination
        with self._engine.connect() as connection:
            binding = connection.execute(
                sa.select(d.c.shelf_id, d.c.target_id).where(d.c.id == destination_id)
            ).first()
            if binding is None:
                raise PlanRefusedError("the host serves no such content to this binding")
            planned = planned_content(connection, destination_id)
            if ref.ref not in planned.planned_refs:
                # a second defence after the ledger: the item is one of this binding's shelf (and,
                # below 18+, one that shelf admits), never an item of another shelf
                raise PlanRefusedError("the host serves no such content to this binding")
            caps = self._caps(binding.target_id)
            accepted = caps.accepted_content_types if caps is not None else DEFAULT_ACCEPTED
            unit = _unit_of(connection, ref.ref)
            if unit is None:
                raise PlanRefusedError("the host holds no bytes for this item")
            contents = {c.content_id: c for c in variant_contents(connection, unit)}
            chosen = choose_content(
                accepted,
                (ContentOffer(c.content_id, c.format, c.media_type) for c in contents.values()),
            )
            if chosen is None:
                raise NoNativeFormatError("no content of the Variant is in an accepted format")
            if contents[chosen.content_id].watermarked and planned.egress == "externe":
                # the guard of L04 holds for the content chosen, not only for the item planned
                raise PlanRefusedError("a watermarked content never leaves for an external egress")
            sources = (
                []
                if chosen.format == "epub"
                else byte_sources(connection, unit, chosen.content_id, shelf_id=binding.shelf_id)
            )
        if chosen.format == "epub":
            return self._read_epub(unit, chosen.content_id), chosen.format, binding.target_id
        for source in sources:
            if source.kind == "destination":
                held = None if source.target_id is None else self._caps(source.target_id)
                if held is None or not copy_can_verify(held):
                    continue  # a server that cannot read a copy back does not vouch for it
            try:
                with self._open(source.local) as stream:
                    data, found = _read_bounded(stream, size=source.size)
            except (OSError, FsopsError, ContentRefusedError):
                continue  # a FIFO, a link, another size: no source, and nothing was waited for
            if found == source.file_hash:
                return data, chosen.format, binding.target_id
        if self._download is not None:
            self._download(unit)
        raise ContentNeedsDownloadError("no copy of the archive can be read and verified")

    def _read_epub(self, unit_id: str, content_id: str) -> bytes:
        """The derivative `servable_epub` verified, read once and compared to the hash it was
        verified at: a file put back at the path since (a restore, a repair) is refused. The one
        verified is the one chosen (the watermark was read on that `content`)."""
        served = None if self._epub_path is None else self._epub_path(unit_id)
        if not isinstance(served, EpubFile) or served.content_id != content_id:
            raise PlanRefusedError("the host serves no such EPUB here")
        try:
            with self._open(served.path) as stream:
                data, found = _read_bounded(stream, size=None)
        except (OSError, FsopsError, ContentRefusedError):
            raise ContentNeedsDownloadError("the verified derivative cannot be read") from None
        if found != served.file_hash:
            raise ContentRefusedError("the EPUB is not the one that was verified")
        return data


class TreeBarrier:
    """The barrier of a shelf, read again while a plan runs (the bound of the identity of the
    server: every `recheck_every` operations). It no longer holds when the binding is refused now
    (partitioning withdrawn, a version known bad) or when something the plan was given is no longer
    admitted (a ceiling lowered)."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def holds(self, destination_id: str, planned: PlannedContent) -> bool:
        with self._engine.connect() as connection:
            if refusal_of(connection, destination_id) is not None:
                return False
            return planned.planned_refs <= planned_content(connection, destination_id).planned_refs


class CachedInstanceProbe:
    """What the server of a target says about itself, asked at most once per `ttl` seconds: the
    executor asks before every operation, the server is not asked that often. `fetch` answers
    `None` when the server gives nothing (a server that was known and answers nothing pauses)."""

    def __init__(
        self, fetch: Callable[[str], str | None], *, ttl_s: float = 30.0, clock: Clock | None = None
    ) -> None:
        self._fetch = fetch
        self._ttl = ttl_s
        self._clock = clock or SystemClock()
        self._seen: dict[str, tuple[datetime, str | None]] = {}

    def fingerprint(self, target_id: str) -> str | None:
        now = self._clock.now()
        hit = self._seen.get(target_id)
        if hit is not None and (now - hit[0]).total_seconds() < self._ttl:
            return hit[1]
        value = self._fetch(target_id)
        self._seen[target_id] = (now, value)
        return value
