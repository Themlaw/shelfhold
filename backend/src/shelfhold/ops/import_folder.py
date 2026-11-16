"""`shelfhold import scan [--dry-run]` and the scheduled scan (EF-SRC-012).

One service for the command, the socket and the job. The folder is `import_dir`; without it the
import is off and says so. A scan looks at the folder, and unless it is a dry run it takes in what
is ready: Work, Edition, Unit and Variant by the road of the sources (`ImportTargets`), the file
stored by the same stores as any received file (CBZ, EPUB through the neutralizer, PDF kept and not
exported), then removed from the folder only once a copy with the same hash was read back. It is
not a reserved action by itself: the one deletion is the folder's own, classed in the registry.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
import structlog

from shelfhold.acquisition.public import ImportTargets, TreeTargets
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.locks import work_lock_for
from shelfhold.i18n import t
from shelfhold.infrastructure.packaging.cbz import PageTarget
from shelfhold.infrastructure.schema import catalog
from shelfhold.library.public import (
    ImportFolder,
    ImportFolderOverlapError,
    KeepCopier,
    PageFormatCbzStore,
    PassthroughStore,
    PdfStore,
    SvgCbzStore,
    UntrustedEpubStore,
    make_precheck,
)
from shelfhold.ops.dedup import suggest_on_arrival
from shelfhold.ops.errors import CodedRefusal
from shelfhold.ops.reclaim import require_space
from shelfhold.ops.requests import build_service
from shelfhold.plugin_host.adapters.meta.suwayomi.tree_import import SuwayomiTree
from shelfhold.plugin_host.adapters.native.import_folder import build_manifest
from shelfhold.work_locks import work_holder

if TYPE_CHECKING:
    from shelfhold.composition import Composition, WriteContext

__all__ = ["ImportOps"]

log = structlog.get_logger()
OFF = "import.not_configured"
TREE_BAD = "import.tree_unreadable"


@contextmanager
def _no_hold(_work_id: str) -> Iterator[None]:
    yield


class StoreRouter:
    """The store of each kind, by the extension the bytes were filed under (never the name)."""

    def __init__(self, comp: Composition, w: WriteContext) -> None:
        settings = comp.settings
        store_dir = str(settings.store_dir)
        mounted = any(e.path == store_dir for e in w.loaded.expected)
        plain = PassthroughStore(
            comp.engine, w.fsops, w.loaded.locate, settings.data_dir,
            tombstones=w.tombstones, hold_work=_no_hold,
            min_page_ratio=settings.revision_min_page_ratio,
        )  # fmt: skip
        keeper = KeepCopier(
            comp.engine, w.fsops, w.loaded.locate, store_dir, settings.data_dir,
            IntentJournal(comp.engine, w.fsops, clock=comp.clock),
            hold_work=_no_hold,
        )  # fmt: skip
        hold = work_holder(comp)  # one hold for both writes
        svg = SvgCbzStore(plain, keeper, settings.data_dir, comp.engine, hold)
        self._stores: dict[str, Any] = {
            # EF-BIB-051 (6): SVG pages drawn to PNG; EF-EXP-046 (ADR-0054): pages in other formats
            # normalized; both declared as a derivative, the original kept in /store
            ".cbz": PageFormatCbzStore(
                svg, plain, keeper, settings.data_dir, comp.engine, hold,
                target=PageTarget(settings.page_target_format, settings.page_target_quality),
                keeps_originals=mounted,
            ),
            ".epub": UntrustedEpubStore(
                comp.engine, w.fsops, w.loaded.locate, settings.data_dir,
                store_dir=store_dir if mounted else None, tombstones=w.tombstones,
                hold_work=work_lock_for(comp.engine, clock=comp.clock),
            ),
            ".pdf": PdfStore(
                comp.engine, w.fsops, w.loaded.locate, tombstones=w.tombstones,
                hold_work=work_holder(comp),
            ),
        }  # fmt: skip

    def ingest(self, **arguments: Any) -> Any:
        return self._stores[PurePath(str(arguments["dest"])).suffix].ingest(**arguments)

    def finish_derivative(self, *, unit_id: str, staged: Path) -> None:
        self._stores[".cbz"].finish_derivative(unit_id=unit_id, staged=staged)

    def derivative_missing(self, unit_id: str, staged: Path) -> bool:
        return bool(self._stores[".cbz"].derivative_missing(unit_id, staged))


class ImportOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    @property
    def folder(self) -> Path | None:
        return self._comp.settings.import_dir

    @contextmanager
    def _open(self, w: WriteContext) -> Any:
        comp = self._comp
        folder = self.folder
        if folder is None:
            raise CodedRefusal(OFF, t("errors.import.not_configured.title"))
        targets = ImportTargets(comp.engine, comp.clock)
        settings = comp.settings
        yield ImportFolder(
            folder, engine=comp.engine, fsops=w.fsops, store=StoreRouter(comp, w),
            staging_dir=settings.data_dir / "tmp", target=targets,
            place=lambda _unit, variant, extension: w.received_path(variant, extension),
            keep_apart=(settings.data_dir, settings.store_dir, settings.library_dir),
            max_bytes=settings.received_cbz_max_bytes, precheck=make_precheck(w.tombstones),
            default_trust=build_manifest().content_trust,  # the manifest says `untrusted`
        )  # fmt: skip

    def scan(self, *, dry_run: bool = False, declared_purchase: bool = False) -> dict[str, Any]:
        """What the folder holds and what was done about it; `dry_run` reads and writes nothing."""
        comp = self._comp
        if self.folder is None:
            raise CodedRefusal(OFF, t("errors.import.not_configured.title"))
        with comp.write_context() as w, self._open(w) as folder:
            if dry_run:
                scan = folder.scan()
                return _report(scan, [], dry_run=True)
            require_space(comp)  # copied into /data: not under the reserve
            scan, outcomes = folder.run(declared_purchase=declared_purchase)
            report = _report(scan, outcomes, dry_run=False)
        if any(o.state == "stored" for o in outcomes):
            suggest_on_arrival(comp)  # EF-BIB-004: an identical title is suggested, not merged
            self._match_requests([o.unit_id for o in outcomes if o.state == "stored"])
        return report

    def _match_requests(self, unit_ids: list[str | None]) -> None:
        """EF-USR-019: what was filed answers the requests put aside to be bought or borrowed.
        After the filing has committed, and never raising into it: a request that cannot be
        matched waits for the next file."""
        comp = self._comp
        ids = [u for u in unit_ids if u]
        if not ids:
            return
        try:
            with comp.engine.connect() as connection:
                works = set(
                    connection.execute(
                        sa.select(catalog.edition.c.work_id)
                        .select_from(
                            catalog.unit.join(
                                catalog.edition, catalog.edition.c.id == catalog.unit.c.edition_id
                            )
                        )
                        .where(catalog.unit.c.id.in_(ids))
                    ).scalars()
                )
            service = build_service(comp)
            for work_id in sorted(works):
                service.arrived_from_import(work_id)
        except Exception:
            log.warning("import.request_match_failed")

    def tree(self, path: str, *, dry_run: bool = False) -> dict[str, Any]:
        """`import tree <path>` (EF-INT-009): the Units of an existing Suwayomi download tree,
        from the files already on the disk. The tree is read and never changed."""
        comp = self._comp
        root = Path(path)
        if not root.is_absolute() or not root.is_dir() or root.is_symlink():
            raise CodedRefusal(TREE_BAD, t("errors.import.tree_unreadable.title"))
        settings = comp.settings
        with comp.write_context() as w:
            tombstones = comp.read_context()[2]
            try:
                folder = SuwayomiTree(
                    root, targets=TreeTargets(comp.engine, comp.clock, tombstones),
                    engine=comp.engine, fsops=w.fsops, store=StoreRouter(comp, w),
                    staging_dir=settings.data_dir / "tmp",
                    place=lambda _unit, variant, extension: w.received_path(variant, extension),
                    keep_apart=(
                        settings.data_dir, settings.store_dir, settings.library_dir,
                        *(Path(e.path) for e in w.loaded.expected),
                    ),
                    max_bytes=settings.received_cbz_max_bytes,
                    precheck=make_precheck(w.tombstones),
                )  # fmt: skip
            except ImportFolderOverlapError:
                raise CodedRefusal(TREE_BAD, t("errors.import.tree_unreadable.title")) from None
            if not dry_run:
                require_space(comp)
            report = folder.import_tree(dry_run=dry_run)
        return {
            "dry_run": dry_run, "ready": list(report.ready),
            "waiting": [{"path": v.rel, "code": v.code} for v in report.waiting],
            "refused": [{"path": v.rel, "code": v.code} for v in report.refused],
            "outcomes": [
                {"path": o.rel, "state": o.state, "code": o.code, "kind": o.kind}
                for o in report.outcomes
            ],
        }  # fmt: skip


def _report(scan: Any, outcomes: list[Any], *, dry_run: bool) -> dict[str, Any]:
    return {
        "dry_run": dry_run,
        "ready": [f.rel for f in scan.ready] if dry_run else [],
        "waiting": [{"path": v.rel, "code": v.code} for v in scan.waiting],
        "refused": [{"path": v.rel, "code": v.code} for v in scan.refused],
        "outcomes": [
            {"path": o.rel, "state": o.state, "code": o.code, "removed": o.removed,
             "kind": o.kind, "watermark": o.watermark}
            for o in outcomes
        ],
    }  # fmt: skip
