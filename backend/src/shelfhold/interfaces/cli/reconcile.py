"""``shelfhold reconcile --adopt-after-restore``: the only way out of ``restored_behind``.

After a restore the database is older than the disk. This command shows what it would do (the
files filed since the backup, the intents that came back stale, the tombstones it replays first),
prints a fingerprint, and acts only on ``--confirm <fingerprint>``. It re-adopts the files from
the provenance mirror, never deletes, and re-stamps the sentinels. A deletion that the tombstone
journal asks for is only carried out with ``--confirm-deletion`` (a reserved action) on top.

It takes ``/data/.lock``: stop Shelfhold first, run it, start Shelfhold again (the server starts
read only until this was done, and says so).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import sqlalchemy as sa

from shelfhold.config import ResolvedSettings
from shelfhold.events.bus import EventBus
from shelfhold.export.application.restore import (
    AdoptionBlockedError,
    AdoptionPreview,
    DeletionConfirmation,
    DeletionRefusedError,
    DeletionUnavailableError,
    PreviewStaleError,
    RestoreAdopter,
    RestoreGuard,
)
from shelfhold.export.application.roots import load_roots
from shelfhold.export.domain.path_map import PathMap
from shelfhold.i18n import t
from shelfhold.identity.public import Principal
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.instance import InstanceLock, InstanceLockHeldError
from shelfhold.infrastructure.local_socket import Peer
from shelfhold.infrastructure.migrate import current_revision, head_revision
from shelfhold.infrastructure.schema import platform
from shelfhold.infrastructure.secrets import diagnostics
from shelfhold.interfaces.local_cli.principal import principal_for_peer
from shelfhold.library.tombstones import TombstoneService
from shelfhold.library.tombstones.screen import TombstoneScreenAdapter

NEEDS_CONFIRMATION = 2
BLOCKED = 3
STALE = 4
REFUSED = 5


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    reconcile = commands.add_parser("reconcile", help=t("reconcile.help"), allow_abbrev=False)
    reconcile.add_argument(
        "--adopt-after-restore", action="store_true", help=t("reconcile.adopt_after_restore_help")
    )
    reconcile.add_argument("--dry-run", action="store_true", help=t("reconcile.dry_run_help"))
    reconcile.add_argument(
        "--confirm", metavar="FINGERPRINT", default=None, help=t("reconcile.confirm_help")
    )
    reconcile.add_argument(
        "--confirm-deletion", action="store_true", help=t("reconcile.confirm_deletion_help")
    )
    reconcile.set_defaults(handler=run)


@contextmanager
def open_restore(resolved: ResolvedSettings) -> Iterator[tuple[RestoreAdopter, RestoreGuard]]:
    """The restore machinery over the instance's own volumes, with the instance lock held."""
    settings = resolved.values
    lock = InstanceLock(settings.data_dir)
    lock.acquire()
    engine = create_db_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            paths = [r.path_entry for r in connection.execute(sa.select(platform.physical_root))]
            loaded = load_roots(connection, PathMap.same_path(paths))
        fsops = Fsops.open(loaded.path_map, loaded.expected, frozen=loaded.restore_pending)
        clock = SystemClock()
        bus = EventBus(clock)
        guard = RestoreGuard(engine, fsops, clock=clock, bus=bus)
        keyring = diagnostics.store_for(settings).load()
        service = TombstoneService(engine, keyring, fsops, bus)
        adopter = RestoreAdopter(
            engine, fsops, TombstoneScreenAdapter(service), clock=clock, bus=bus, guard=guard
        )
        yield adopter, guard
    finally:
        engine.dispose()
        lock.release()


def _show(preview: AdoptionPreview) -> None:
    report = preview.report
    print(t("reconcile.fingerprint_line", fingerprint=preview.fingerprint))
    print(
        t(
            "reconcile.header",
            roots=len(report.status.behind_roots),
            files=len(preview.readopt),
            stale=len(report.stale_intents),
        )
    )
    for line in preview.readopt:
        print(t("reconcile.readopt_line", path=line.rel_path))
    for path, reason in preview.unresolved:
        print(t("reconcile.unresolved_line", path=path, reason=reason))
    for path in report.modified_files:
        print(t("reconcile.modified_line", path=path))
    for path in report.unknown_files:
        print(t("reconcile.unknown_line", path=path))
    for path in report.missing_files:
        print(t("reconcile.missing_line", path=path))
    for series in report.upstream_drift:
        print(t("reconcile.drift_line", source=series.source_key, series=series.series_ref))
    if report.upstream_proposal:
        print(t("reconcile.proposal_remove_category"))
    for candidate in preview.deletions:
        print(t("reconcile.deletion_line", path=candidate.rel_path, scope=candidate.scope))
    if report.unverified_tombstone_lines:
        print(t("reconcile.unverified_line", count=report.unverified_tombstone_lines))
    for root_id, reason in sorted(preview.blocked_roots.items()):
        print(t("reconcile.blocked_line", root=root_id, reason=reason))


def _principal(data_dir: Path) -> Principal | None:
    """The operator at the host's console, vetted as the socket vets a peer (EF-USR-024): root or
    the owner of `/data`. The principal comes from the one place that builds it from a peer."""
    uid = os.getuid()
    if uid != 0 and uid != data_dir.stat().st_uid:
        return None
    return principal_for_peer(Peer(os.getpid(), uid, os.getgid()))


def run(args: argparse.Namespace, resolved: ResolvedSettings) -> int:
    if not args.adopt_after_restore:
        print(t("reconcile.needs_flag"), file=sys.stderr)
        return NEEDS_CONFIRMATION
    database = resolved.values.database_path
    if not database.exists() or current_revision(database) != head_revision():
        print(t("status.database_not_migrated"), file=sys.stderr)
        return NEEDS_CONFIRMATION
    try:
        with open_restore(resolved) as (adopter, guard):
            status = guard.enter_restored_behind()
            if not status.behind:
                print(t("reconcile.not_behind"))
                return 0
            preview = adopter.preview()
            _show(preview)
            if args.dry_run:
                return 0
            if not args.confirm:
                print(t("reconcile.needs_confirmation"), file=sys.stderr)
                return NEEDS_CONFIRMATION
            principal = _principal(resolved.values.data_dir) if args.confirm_deletion else None
            if args.confirm_deletion and principal is None:
                print(t("reconcile.deletion_refused"), file=sys.stderr)
                return REFUSED
            deletion = DeletionConfirmation(principal) if principal is not None else None
            result = adopter.adopt(confirm=args.confirm, deletion=deletion)
    except InstanceLockHeldError:
        print(t("reconcile.instance_running"), file=sys.stderr)
        return BLOCKED
    except AdoptionBlockedError as blocked:
        print(t("reconcile.blocked", root=blocked.root_id, reason=blocked.reason), file=sys.stderr)
        return BLOCKED
    except PreviewStaleError:
        print(t("reconcile.preview_stale"), file=sys.stderr)
        return STALE
    except (DeletionRefusedError, DeletionUnavailableError):
        print(t("reconcile.deletion_refused"), file=sys.stderr)
        return REFUSED
    print(
        t(
            "reconcile.done",
            files=len(result.readopted),
            pending=len(result.pending_deletions),
            deleted=len(result.deleted),
        )
    )
    return 0
