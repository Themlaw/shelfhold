"""Every code site that deletes for good, and the class that makes it legitimate (N2).

A site is `<path under src>::<qualified function>`. Its class is one of:

- a name of `RESERVED_ACTIONS`: the site runs only for that reserved action, authorised upstream;
- a name of `SYSTEM_EMITTERS`: an internal emitter, limited by fsops to its perimeter;
- `PRIMITIVE`: the fsops layer itself; whoever calls it is classed on its own line;
- `REPLAY`: the crash-recovery replay of the intent journal, which only completes an `rmdir` of an
  empty folder that a classed emitter journaled before the crash;
- `RESTORE`: the rollback of the database file to its pre-migration copy;
- `OWN_SCRATCH`: a file the process created for its own mechanism (temporary copy, progress
  marker, lease swap), never library data.

`tests/unit/test_deletion_emitters_architecture.py` scans the package and fails on a site missing
here or listed here but gone. Adding a line is a security decision, reviewed as such.
"""

from collections.abc import Mapping
from types import MappingProxyType

PRIMITIVE = "fsops_primitive"
OWN_SCRATCH = "own_scratch"
RESTORE = "restore"
REPLAY = "intent_replay"

_FSOPS = "shelfhold/infrastructure/fsops"

# Each closed class lists exactly the sites it admits; nothing is admitted by module.
CLOSED_SITES: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        # The fsops layer: every caller of these is a site of its own.
        PRIMITIVE: frozenset(
            {
                f"{_FSOPS}/confined.py::ConfinedRoot.unlink",
                f"{_FSOPS}/confined.py::ConfinedRoot.rmdir",
                f"{_FSOPS}/confined.py::ConfinedRoot.remove_tree",
                f"{_FSOPS}/confined.py::ConfinedRoot._empty",
                f"{_FSOPS}/confined.py::ConfinedRoot._rename_noreplace",
                f"{_FSOPS}/facade.py::Fsops.unlink",
                f"{_FSOPS}/facade.py::Fsops.rmdir",
                f"{_FSOPS}/facade.py::Fsops.purge_remove_tree",  # L34: bin entries only
                f"{_FSOPS}/facade.py::Fsops.purge_unlink",  # L34: one regular file, never reserved
                f"{_FSOPS}/facade.py::Fsops.recycle",  # empty folders it just made
                f"{_FSOPS}/facade.py::Fsops.system_unlink",  # perimeter and condition checked
                f"{_FSOPS}/facade.py::Fsops.system_remove_tree",
                f"{_FSOPS}/facade.py::Fsops._system_empty",
                "shelfhold/infrastructure/instance/lease.py::WriteSession.unlink",
                "shelfhold/infrastructure/instance/lease.py::WriteSession.rmdir",
            }
        ),
        # The process's own mechanism: temporary copy, progress marker, lease swap.
        OWN_SCRATCH: frozenset(
            {
                f"{_FSOPS}/facade.py::_claim_lease",
                "shelfhold/infrastructure/migrate.py::MigrationStateFile.clear",
                "shelfhold/infrastructure/secrets/atomic.py::write_private",
                # the CLI socket this process bound (and its staging directory), in /data
                "shelfhold/infrastructure/local_socket/server.py::LocalSocketServer._remove_socket",
                "shelfhold/infrastructure/local_socket/server.py::bind_private",
                # the probe folder the write check made itself (EF-UX-002)
                f"{_FSOPS}/probe.py::_remove_probe_dir",
            }
        ),
        # Rolls the database file (and its sidecars) back to the copy taken before a migration.
        RESTORE: frozenset({"shelfhold/infrastructure/migrate.py::_restore_database"}),
        # Completes an `rmdir` of an empty folder a classed emitter journaled before a crash.
        REPLAY: frozenset(
            {"shelfhold/export/application/intents.py::IntentJournal._resolve_rmdir"}
        ),
    }
)

_EXPORT_APP = "shelfhold/export/application"

_OTHER: Mapping[str, str] = MappingProxyType(
    {
        # System emitters (closed list): `system_*` calls name theirs as a literal.
        f"{_FSOPS}/facade.py::Fsops.boot": "staging_orphan_cleanup",
        f"{_FSOPS}/writer.py::_discard": "staging_orphan_cleanup",  # a failed write's staged file
        # `purge --now` (L34): the service, and the one helper that erases under /data
        "shelfhold/export/application/purge.py::PurgeService.purge_unit": "purge.now",
        "shelfhold/export/application/purge.py::PurgeService.purge_work": "purge.now",
        "shelfhold/infrastructure/volumes.py::erase_data_path": "purge.now",
        "shelfhold/infrastructure/volumes.py::_remove_tree_at": "purge.now",
        # `purge --now` doors (L41): the CLI command, the socket method, the two routes (a reserved
        # action: a fresh session, never a key) and the service-less glue they share
        "shelfhold/interfaces/cli/purge.py::_purge": "purge.now",
        "shelfhold/interfaces/local_cli/ops.py::_purge": "purge.now",
        "shelfhold/interfaces/http_api/library.py::build_library_router.purge_unit": "purge.now",
        "shelfhold/interfaces/http_api/library.py::build_library_router.purge_work": "purge.now",
        "shelfhold/ops/library.py::LibraryOps.purge": "purge.now",
        # `reconcile --adopt-after-restore --confirm-deletion` (L35): the port to the purge, reached
        # only after the reserved action was authorised; it deletes nothing by itself
        f"{_EXPORT_APP}/restore.py::DeletionExecutor.purge": "reconcile.confirm_deletion",
        f"{_EXPORT_APP}/restore_purge.py::RestoreDeletion._erase": "reconcile.confirm_deletion",
        f"{_EXPORT_APP}/restore_purge.py::RestoreDeletion.purge": "reconcile.confirm_deletion",
        # The probe file of the permission preflight (EF-EXP-054): a name the probe drew itself.
        f"{_EXPORT_APP}/preflight.py::FolderProbe._remove_probe_file": "probe_file_cleanup",
        f"{_FSOPS}/probe.py::_remove_probe_file": ("probe_file_cleanup"),
    }
)

DELETION_SITES: Mapping[str, str] = MappingProxyType(
    {**{site: name for name, sites in CLOSED_SITES.items() for site in sites}, **_OTHER}
)
