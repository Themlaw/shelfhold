"""The scheduled scan of the import folder (EF-SRC-012).

Every few minutes, when `import_dir` is set: the same scan as `shelfhold import scan`, by the same
service. A file only just dropped waits for the next one (its size and modification time must stop
moving). What is stored goes out: the export is asked for, as after any received file.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

import structlog

from shelfhold.composition import Composition, KeyringUnavailableError, NoLibraryError
from shelfhold.jobs.public import JobContext, JobPools
from shelfhold.ops.errors import CodedRefusal, ExitUnavailableError
from shelfhold.ops.import_folder import ImportOps

__all__ = ["IMPORT_KIND", "import_handler", "register_import_job"]

log = structlog.get_logger(__name__)

IMPORT_KIND: Final = "import.scan"
IMPORT_CRON: Final = "*/10 * * * *"


def _skipped(refused: CodedRefusal) -> str:
    """Off, or refused for lack of space (M3 L26): a scan that waits for room, not a failure."""
    return (
        "space_insufficient" if refused.code == "library.space_insufficient" else "not_configured"
    )


def import_handler(
    comp: Composition, on_stored: Callable[[], None] | None = None
) -> Callable[[JobContext], None]:
    def run(context: JobContext) -> None:
        try:
            report = ImportOps(comp).scan()
        except (NoLibraryError, KeyringUnavailableError, ExitUnavailableError) as error:
            log.info("import.skipped", reason=type(error).__name__)
            context.record(skipped=type(error).__name__)
            return
        except CodedRefusal as refused:
            context.record(skipped=_skipped(refused))
            return
        done = report["outcomes"]
        stored = sum(1 for o in done if o["state"] == "stored")
        context.record(
            stored=stored, removed=sum(1 for o in done if o["removed"]),
            refused=len(report["refused"]) + sum(1 for o in done if o["state"] == "refused"),
            waiting=len(report["waiting"]),
        )  # fmt: skip
        if stored and on_stored is not None:
            on_stored()  # what arrived goes out

    return run


def register_import_job(
    pools: JobPools, comp: Composition, on_stored: Callable[[], None] | None = None
) -> None:
    if comp.settings.import_dir is None:
        return  # off: nothing is scheduled, nothing is read
    pools.register(IMPORT_KIND, pool="io", handler=import_handler(comp, on_stored))
    pools.scheduler.add(IMPORT_KIND, IMPORT_CRON, jitter_s=60)
