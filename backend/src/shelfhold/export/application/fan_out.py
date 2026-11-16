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

The bytes never come from a plan and never from another binding: `planned_content` is the only
thing that fills the ledger.
"""

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

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
from shelfhold.export.application.target_state import DestinationPausedError
from shelfhold.export.domain.bindings import (
    PARTITIONED,
    TreeOverlapError,
    effective_partitioning,
    is_family,
)
from shelfhold.export.domain.kavita_compat import blocks_family_shelf
from shelfhold.export.domain.path_map import clean_absolute, within
from shelfhold.infrastructure.clock import Clock, SystemClock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fsops import FsopsError
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export, platform, storage
from shelfhold.library.public import export_sources
from shelfhold.sdk.api_call import ApiCall
from shelfhold.sdk.export_target import TargetCapabilities
from shelfhold.sdk.fsops import ContentRef, PlannedContent

__all__ = [
    "AppliedFile",
    "BindingOutcome",
    "BindingView",
    "CachedInstanceProbe",
    "DestinationSource",
    "FanOut",
    "FanOutReport",
    "HostContentStore",
    "attach_secondary",
    "bindings_of",
    "ensure_tree",
    "ensure_trees",
    "is_secondary",
    "mirror_to_indexers",
    "planned_content",
    "record_applied",
    "refusal_of",
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
        ensure_tree(
            connection, destination_id=view.destination_id, target_id=view.target_id,
            local_root=os.path.realpath(root),
        )  # fmt: skip
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


_FORMATS = {"cbz": "cbz", "epub": "epub", "pdf": "pdf"}


def record_applied(
    connection: Connection, *, destination_id: str, files: Sequence[AppliedFile], now: datetime
) -> None:
    """The manifest of a binding after its plan ran: one line per file, then the copy to every
    indexer of the tree, in this transaction. Idempotent: it says what the disk holds, so a pass
    that is run again after a crash puts back what the crash kept from the manifest."""
    record = export.export_record
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
        elif found.state in ("planned", "error", "missing"):
            connection.execute(
                sa.update(record)
                .where(
                    record.c.shelf_destination_id == destination_id,
                    record.c.export_item_id == file.export_item_id,
                )
                .values(**values)
            )
        mirror_to_indexers(connection, destination_id, file.export_item_id)


# -- what a binding may be given ------------------------------------------------------------


def planned_content(connection: Connection, destination_id: str) -> PlannedContent:
    """The content that may be cited for *this* binding: the export items of the Editions filed
    on the shelf of the binding and, on a shelf below 18+, admitted by the same rule that files
    them (rated, under the ceiling, not suggested above it). The egress is the one the instance
    attested for the binding, never what the server declares. Nothing of another shelf is here
    (point m5 of #299): a plan that cites it is refused by the executor."""
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
    return PlannedContent(
        planned_refs=frozenset(refs),
        egress="externe" if row.egress == "externe" else "foyer",
        watermarked_refs=frozenset(watermarked),
    )


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


@dataclass(frozen=True)
class FanOutReport:
    outcomes: tuple[BindingOutcome, ...]


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
                planned = planned_content(connection, destination_id)
            if refusal is not None:
                log.info("fan_out.refused", destination_id=destination_id, code=refusal)
                return BindingOutcome(destination_id, "refused", refusal)
            capabilities = self._source.capabilities(view.target_id)
            if capabilities is None:
                return BindingOutcome(destination_id, "skipped", "no_destination")
            binding = PlanBinding(destination_id, view.target_id, capabilities)
            self._pipeline.plan_ledger.plan(call_id, destination_id, planned)
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
                destination_id, "indexer" if view.role == "secondary" else "done", None, executed
            )
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
            AppliedFile(w.export_item_id, w.rel_path, w.file_hash, w.size, w.mtime_ns)
            for w in report.files
        ]
        with write_transaction(self._engine) as connection:
            record_applied(
                connection, destination_id=destination_id, files=files, now=self._clock.now()
            )


# -- what the composition root gives the executor -----------------------------------------------


class HostContentStore:
    """The bytes the host owns for an export item: the file of its Unit under the host's roots.
    Only what the ledger planned is ever asked for; anything else (a sidecar nobody made, a
    volume) is refused here as well."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def read(self, destination_id: str, ref: ContentRef) -> bytes:
        if ref.kind != "export_item":
            raise PlanRefusedError("the host serves no such content here")
        coverage = export.unit_coverage
        with self._engine.connect() as connection:
            unit = connection.execute(
                sa.select(coverage.c.unit_id)
                .where(coverage.c.carrier_type == "export_item", coverage.c.carrier_id == ref.ref)
                .order_by(coverage.c.position)
                .limit(1)
            ).scalar()
            source = (
                None
                if unit is None
                else export_sources(connection, [unit], formats=("cbz", "epub")).get(unit)
            )
        if source is None or source.local_path is None:
            raise PlanRefusedError("the host holds no bytes for this item")
        return Path(source.local_path).read_bytes()


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
