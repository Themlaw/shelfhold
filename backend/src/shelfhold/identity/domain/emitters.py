"""Every code site that deletes for good, and the class that makes it legitimate (N2).

A site is `<path under src>::<qualified function>`. Its class is one of:

- a name of `RESERVED_ACTIONS`: the site runs only for that reserved action, authorised upstream;
- a name of `SYSTEM_EMITTERS`: an internal emitter, limited by fsops to its perimeter;
- `PRIMITIVE`: the fsops layer itself; whoever calls it is classed on its own line;
- `REPLAY`: the crash-recovery replay of the intent journal, which only completes an `rmdir` of an
  empty folder that a classed emitter journaled before the crash;
- `RESTORE`: the rollback of the database file to its pre-migration copy;
- `DB_ROWS`: rows of the database, never a file: the ticket and cached handle columns of the
  connected service, reset under a changed instance fingerprint by `source reconnect` (EF-SRC-036
  (3)); no Unit, Variant or library file is reachable from it;
- `BACKUP_DIRECTORY`: the integrated backup's own files (EF-INT-015, M1b L23): in the backup
  directory, an archive `shelfhold-backup-<UTC>.tar.age` beyond the 14 kept (only one that the
  instance recorded and that starts like one of ours, never before the new one is written and
  verified) and a `.partial-*` file of a failed write (`ops/backup/store.py`, the one module that
  opens that directory, exact name patterns, regular files only); in `/data/tmp`, the
  `backup-*` scratch folder of the run's clear snapshot (`ops/backup/scratch.py`: removed
  *recursively* (its files, links and the staged sub-folders, never a link's target), only a
  folder of that exact name that carries the marker file the module writes first, and that nobody
  holds a lock on);
- `IMPORT_FOLDER`: a file of the import folder (EF-SRC-012), taken out only after the store accepted
  it and a copy holding the very same SHA-256 was read back from the disk, the source being hashed
  once more (`library/adapters/import_scan.py`, one function, one regular file by descriptor, never
  a link, never a directory);
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
DB_ROWS = "database_rows"
REPLAY = "intent_replay"
BACKUP_DIRECTORY = "backup_directory"
IMPORT_FOLDER = "import_folder"

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
                f"{_FSOPS}/facade.py::Fsops.relocate_inbox",  # the emptied legacy inbox folder only
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
                # EF-EXP-016: the staged new version of a same-path upgrade given up before its
                # swap, at the token's own path (`.shelfhold/upgrade/<intent>`); never served
                f"{_FSOPS}/facade.py::Fsops.same_path_discard",
                "shelfhold/infrastructure/migrate.py::MigrationStateFile.clear",
                "shelfhold/infrastructure/secrets/atomic.py::write_private",
                # the CLI socket this process bound (and its staging directory), in /data
                "shelfhold/infrastructure/local_socket/server.py::LocalSocketServer._remove_socket",
                "shelfhold/infrastructure/local_socket/server.py::bind_private",
                # the plugin staging directory and the plugin files `plugin install` unpacked
                # itself under /data/plugins (EF-SRC-019); never a library path
                "shelfhold/plugin_host/install.py::install_wheel",
                "shelfhold/plugin_host/install.py::remove_plugin",
                "shelfhold/plugin_host/install.py::_undo",
                # the probe folder the write check made itself (EF-UX-002)
                f"{_FSOPS}/probe.py::_remove_probe_dir",
            }
        ),
        # Database rows only, no file: the open tickets of the old installation go back to
        # `requested` and its cached `fetch_handle`s are forgotten. Reached only from
        # `reconnect`, which runs only when the service is paused for a changed fingerprint
        # (the admin's `source reconnect`); EF-SRC-036 (3) asks for exactly this purge.
        DB_ROWS: frozenset({"shelfhold/acquisition/application/instance.py::_purge_orphans"}),
        # Rolls the database file (and its sidecars) back to the copy taken before a migration.
        RESTORE: frozenset({"shelfhold/infrastructure/migrate.py::_restore_database"}),
        # The integrated backup's own directory: archives past the retention, partial files and
        # the work folder of the clear snapshot; exact name patterns, never a library path.
        BACKUP_DIRECTORY: frozenset(
            {
                "shelfhold/ops/backup/store.py::BackupDirectory._discard",
                "shelfhold/ops/backup/scratch.py::_remove_scratch",
            }
        ),
        # The import folder: the one removal of a source file, after a hash-verified storage.
        IMPORT_FOLDER: frozenset(
            {
                "shelfhold/library/adapters/import_scan.py::ImportFolder._put_back",
                "shelfhold/library/adapters/import_scan.py::ImportFolder._quarantine_and_drop",
            }
        ),
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
        # The erasure of a download in the connected service after a verified export (L16,
        # EF-SRC-009): a deed of the window job, reached only on a shelf where an admin turned the
        # option on with this reserved action (the sole writer of the column), and only when the
        # rule of EF-BIB-038 holds again at the moment of the deed (EF-EXP-064).
        "shelfhold/library/adapters/safeguard_job.py::UpstreamPurge.execute": (
            "shelf.purge_upstream"
        ),
        # `purge --now` (L17): the download of a purged Unit in the connected service, by the deed
        # of "Mettre à l'abri" (an intent that names the request, one erasure at most, never again
        # for a deed that was begun). Reached from the purge and from the M1b leftovers plan,
        # both behind the reserved action `purge.now`.
        "shelfhold/library/adapters/safeguard_job.py::UpstreamPurge.release_for_purge": (
            "purge.now"
        ),
        # The recycle bins past 30 days (ENF-OPS-009): the daily sweep, under an intention; fsops
        # reads the age of the entry from the file system and refuses a younger one.
        f"{_EXPORT_APP}/recycle_sweep.py::RecycleSweeper.sweep": "trash_retention_purge",
        # The probe file of the permission preflight (EF-EXP-054): a name the probe drew itself.
        f"{_FSOPS}/probe.py::_remove_probe_file": ("probe_file_cleanup"),
        # The files of the capability probe of a root (EF-EXP-059): names it drew, in the staging
        # folder of the root and of /store.
        f"{_FSOPS}/facade.py::Fsops.probe_capabilities.remove": "probe_file_cleanup",
    }
)

DELETION_SITES: Mapping[str, str] = MappingProxyType(
    {**{site: name for name, sites in CLOSED_SITES.items() for site in sites}, **_OTHER}
)


# Every code site that **replaces a file in place** (`replace=True` on an atomic write, a copy or a
# rename), the way a deletion is classed above: the old bytes are gone once it has run, so a new one
# is a security decision, reviewed as such. The value says what makes it legitimate. Each of them
# writes a staged file, reads it back against a hash and swaps it in; none unlinks first.
REPLACEMENT_SITES: Mapping[str, str] = MappingProxyType(
    {
        "shelfhold/library/adapters/storage.py::KeepCopier._declare": (
            "a declared derivative replaces the received file, the original having been kept first"
        ),
        "shelfhold/export/application/purge.py::PurgeService._write_index": (
            "the index of /store rewritten without the purged Units, under its lock, never a copy"
        ),
        "shelfhold/library/adapters/storage.py::KeepCopier._copy": (
            "the copy to /store of a file read back against its hash, the target of its own intent"
        ),
        "shelfhold/library/adapters/safeguard.py::Safeguard._verify_and_write": (
            "the safeguard writes into /store, never /library, once the payload verifies"
        ),
        "shelfhold/export/application/withdrawal.py::WithdrawalService._keep_copy": (
            "the copy to /store of an adopted original before it leaves the library, read back "
            "against its hash, a new path of its own (never /library, never over another file)"
        ),
        "shelfhold/library/adapters/store_index.py::StoreIndex.rewrite": (
            "store/INDEX.jsonl, a projection rewritten whole"
        ),
        "shelfhold/library/adapters/epub_derivative.py::UntrustedEpubStore.regenerate": (
            "a security update of the neutralizer replaces the derivative of an untrusted EPUB, "
            "verified first, from the original or the stored derivative (EF-BIB-051)"
        ),
    }
)
