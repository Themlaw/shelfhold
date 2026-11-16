"""`shelfhold library normalize-pages [--dry-run]` (EF-EXP-046, ADR-0054): the pages of the archives
already filed are brought to JPEG, PNG, GIF or WebP. The received file goes to /store first, the
library takes the derivative. Not a reserved action: it deletes nothing and keeps every original,
and a run that is cut off is finished by running it again."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.page_policy import page_policy_resolver
from shelfhold.export.application.page_rerender import stale_served_files
from shelfhold.export.application.rendering import cbz_renderer
from shelfhold.export.application.upgrade import SamePathUpgrade, UpgradeRequest
from shelfhold.export.targets.kavita.destination import KavitaDestination
from shelfhold.i18n import t
from shelfhold.infrastructure.packaging.cbz import PageTarget
from shelfhold.infrastructure.schema import export
from shelfhold.library.public import (
    KeepCopier,
    LibraryPageNormalizer,
    PageStoreNotConfiguredError,
)
from shelfhold.ops.errors import CodedRefusal
from shelfhold.ops.reclaim import require_space
from shelfhold.work_locks import work_holder

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["PageNormalizeOps"]


def _capabilities_of(engine: Engine) -> Callable[[str], Any]:
    """What a destination declares (`accepted_image_types`): the core's own destination, Kavita."""

    def capabilities(target_id: str) -> Any:
        with engine.connect() as connection:
            kind = connection.execute(
                sa.select(export.export_target.c.kind).where(export.export_target.c.id == target_id)
            ).scalar()
        return KavitaDestination.capabilities if kind == "kavita" else None

    return capabilities


class PageNormalizeOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def normalize(self, *, dry_run: bool = False) -> dict[str, Any]:
        comp = self._comp
        settings = comp.settings
        store_dir = str(settings.store_dir)
        with comp.write_context() as w:
            mounted = any(e.path == store_dir for e in w.loaded.expected)
            hold = work_holder(comp)
            keeper = KeepCopier(
                comp.engine, w.fsops, w.loaded.locate, store_dir, settings.data_dir,
                IntentJournal(comp.engine, w.fsops, clock=comp.clock), hold_work=hold,
            )  # fmt: skip
            workdir = settings.data_dir / "tmp"
            normalizer = LibraryPageNormalizer(
                comp.engine, w.fsops, w.loaded.locate, keeper, workdir, hold,
                target=PageTarget(settings.page_target_format, settings.page_target_quality),
                store_configured=mounted, store_dir=store_dir,
            )  # fmt: skip
            if not dry_run:
                require_space(comp)  # a copy of each original goes to /store
            try:
                summary = normalizer.run(dry_run=dry_run)
            except PageStoreNotConfiguredError as refused:
                raise CodedRefusal(refused.code, t(f"errors.{refused.code}.title")) from None
            target = PageTarget(settings.page_target_format, settings.page_target_quality)
            capabilities = _capabilities_of(comp.engine)
            stale = stale_served_files(
                comp.engine, w.fsops, page_policy_resolver(capabilities, target)
            )
            rewritten: list[dict[str, str]] = []
            if not dry_run:
                upgrade = SamePathUpgrade(
                    comp.engine, w.fsops, locate=w.loaded.locate, data_dir=settings.data_dir,
                    clock=comp.clock, bus=w.bus,
                    renderer=cbz_renderer(
                        w.fsops, staging_dir=workdir, target=target, capabilities=capabilities
                    ),
                )  # fmt: skip
                for served in stale:
                    result = upgrade.upgrade(
                        UpgradeRequest(
                            served.destination_id,
                            served.export_item_id,
                            served.content_id,
                            "better_version",
                            rerender=True,
                        )
                    )
                    rewritten.append(
                        {"export_item_id": served.export_item_id, "outcome": result.outcome.value}
                    )
        return {
            "dry_run": dry_run,
            "pending": [u.unit_id for u in summary.pending],
            "outcomes": [
                {"unit_id": r.unit_id, "status": r.status, "code": r.code,
                 "issues": [i.code for i in r.issues]}
                for r in summary.reports
            ],
            "normalized": summary.normalized,
            "kept_as_received": summary.kept_as_received,
            "failed": summary.failed,
            "destination_files": [s.export_item_id for s in stale],
            "destination_outcomes": rewritten,
        }  # fmt: skip
