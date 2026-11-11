"""`content reneutralize` (EF-BIB-051, in the manner of ENF-OPS-021): after a security update of the
neutralizer, every derivative of an `untrusted` EPUB is made again, after a plan and a
confirmation.

Without a confirmation it only reads: which derivatives are under another release, where each
regeneration restarts from (the original in /store, else the stored derivative) and which cannot be
regenerated. With the fingerprint of that plan it replaces each derivative by an atomic
replacement, after verifying the new file. It deletes nothing and keeps the generation replaced in
`derived_chain`. It writes to /library, so it runs in the instance under the write lease.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING, Any

from shelfhold.export.application.locks import work_lock_for
from shelfhold.infrastructure.content_trust import DERIVED_BY
from shelfhold.library.public import (
    ReneutralizePlan,
    Reneutralizer,
    StalePlanError,
    UntrustedEpubStore,
)
from shelfhold.ops.errors import StalePreviewError
from shelfhold.ops.library import refusal_of

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["ContentOps"]


def _shown(plan: ReneutralizePlan) -> dict[str, Any]:
    return {
        "target": DERIVED_BY,
        "fingerprint": plan.fingerprint,
        "items": [
            {"unit": i.unit_id, "from": i.from_by, "to": i.to_by, "source": i.source,
             "reason": i.reason}
            for i in plan.items
        ],
    }  # fmt: skip


class ContentOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def reneutralize(self, *, confirm: str | None) -> dict[str, Any]:
        try:
            return self._reneutralize(confirm)
        except StalePlanError as error:
            raise StalePreviewError(str(error)) from None
        except Exception as error:
            refusal = refusal_of(error)
            if refusal is None:
                raise
            raise refusal from None

    def _reneutralize(self, confirm: str | None) -> dict[str, Any]:
        comp = self._comp
        if confirm is None:  # the plan reads the library; it holds no lease and writes nothing
            fsops, loaded, tombstones = comp.read_context()
            plan = self._worker(fsops, loaded, tombstones).plan()
            return {**_shown(plan), "applied": None}
        with comp.write_context() as w:
            worker = self._worker(w.fsops, w.loaded, w.tombstones)
            plan = worker.plan()
            if not hmac.compare_digest(confirm.strip().encode(), plan.fingerprint.encode()):
                raise StalePreviewError("the plan is not the one that was previewed: look again")
            report = worker.apply(plan.fingerprint)
        return {
            **_shown(plan),
            "applied": {
                "regenerated": report.regenerated, "blocked": report.blocked,
                "failed": report.failed,
            },
        }  # fmt: skip

    def _worker(self, fsops: Any, loaded: Any, tombstones: Any) -> Reneutralizer:
        comp = self._comp
        store = str(comp.settings.store_dir)
        mounted = any(e.path == store for e in loaded.expected)
        epubs = UntrustedEpubStore(
            comp.engine, fsops, loaded.locate, comp.settings.data_dir,
            store_dir=store if mounted else None, tombstones=tombstones,
            hold_work=work_lock_for(comp.engine, clock=comp.clock),
        )  # fmt: skip
        return Reneutralizer(epubs, engine=comp.engine)
