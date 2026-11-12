"""`shelfhold import scan [--dry-run]` and the scheduled scan (EF-SRC-012).

One service for the command, the socket and the job. The folder is `import_dir`; without it the
import is off and says so. A scan looks at the folder, and unless it is a dry run it takes in what
is ready: Work, Edition, Unit and Variant by the road of the sources (`ImportTargets`), the file
stored by the same stores as any received file (CBZ, EPUB through the neutralizer, PDF kept and not
exported), then removed from the folder only once a copy with the same hash was read back. It is
not a reserved action by itself: the one deletion is the folder's own, classed in the registry.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any

from shelfhold.acquisition.public import ImportTargets, TreeTargets
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.locks import work_lock_for
from shelfhold.i18n import t
from shelfhold.library.public import (
    ImportFolder,
    ImportFolderOverlapError,
    KeepCopier,
    PassthroughStore,
    PdfStore,
    SvgCbzStore,
    UntrustedEpubStore,
    make_precheck,
)
from shelfhold.ops.errors import CodedRefusal
from shelfhold.plugin_host.adapters.meta.suwayomi.tree_import import SuwayomiTree
from shelfhold.plugin_host.adapters.native.import_folder import build_manifest
from shelfhold.work_locks import work_holder

if TYPE_CHECKING:
    from shelfhold.composition import Composition, WriteContext

__all__ = ["ImportOps"]

OFF = "import.not_configured"
TREE_BAD = "import.tree_unreadable"


class StoreRouter:
    """The store of each kind, by the extension the bytes were filed under (never the name)."""

    def __init__(self, comp: Composition, w: WriteContext) -> None:
        settings = comp.settings
        store_dir = str(settings.store_dir)
        mounted = any(e.path == store_dir for e in w.loaded.expected)
        self._stores: dict[str, Any] = {
            ".cbz": SvgCbzStore(  # EF-BIB-051 (6): SVG pages drawn to PNG, declared a derivative
                PassthroughStore(
                    comp.engine, w.fsops, w.loaded.locate, settings.data_dir,
                    tombstones=w.tombstones, hold_work=work_holder(comp),
                    min_page_ratio=settings.revision_min_page_ratio,
                ),
                KeepCopier(
                    comp.engine, w.fsops, w.loaded.locate, store_dir, settings.data_dir,
                    IntentJournal(comp.engine, w.fsops, clock=comp.clock),
                    hold_work=work_holder(comp),
                ),
                settings.data_dir,
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
            scan, outcomes = folder.run(declared_purchase=declared_purchase)
            return _report(scan, outcomes, dry_run=False)

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
