"""Every code site that deletes for good, and the class that makes it legitimate (N2).

A site is `<path under src>::<qualified function>`. Its class is one of:

- a name of `RESERVED_ACTIONS`: the site runs only for that reserved action, authorised upstream;
- a name of `SYSTEM_EMITTERS`: an internal emitter, limited by fsops to its perimeter;
- `PRIMITIVE`: the fsops layer itself; whoever calls it is classed on its own line;
- `REPLAY`: the crash-recovery replay of the intent journal, which only completes an `rmdir` of an
  empty folder that a classed emitter journaled before the crash;
- `OWN_SCRATCH`: a file the process created for its own mechanism (temporary copy, progress
  marker, lease swap), never library data.

`tests/unit/test_deletion_emitters_architecture.py` scans the package and fails on a site missing
here or listed here but gone. Adding a line is a security decision, reviewed as such.
"""

from collections.abc import Mapping
from types import MappingProxyType

PRIMITIVE = "fsops_primitive"
OWN_SCRATCH = "own_scratch"
REPLAY = "intent_replay"

_FSOPS = "shelfhold/infrastructure/fsops"

DELETION_SITES: Mapping[str, str] = MappingProxyType(
    {
        # The fsops layer: every caller of these is a site of its own below or elsewhere.
        f"{_FSOPS}/confined.py::ConfinedRoot.unlink": PRIMITIVE,
        f"{_FSOPS}/confined.py::ConfinedRoot.rmdir": PRIMITIVE,
        f"{_FSOPS}/confined.py::ConfinedRoot.remove_tree": PRIMITIVE,
        f"{_FSOPS}/confined.py::ConfinedRoot._empty": PRIMITIVE,
        f"{_FSOPS}/confined.py::ConfinedRoot._rename_noreplace": PRIMITIVE,  # a moved source
        f"{_FSOPS}/facade.py::Fsops.unlink": PRIMITIVE,
        f"{_FSOPS}/facade.py::Fsops.rmdir": PRIMITIVE,
        f"{_FSOPS}/facade.py::Fsops.recycle": PRIMITIVE,  # empty folders it just made
        f"{_FSOPS}/facade.py::Fsops.system_unlink": PRIMITIVE,  # perimeter-checked, N2
        f"{_FSOPS}/facade.py::Fsops.system_remove_tree": PRIMITIVE,
        "shelfhold/infrastructure/instance/lease.py::WriteSession.unlink": PRIMITIVE,
        "shelfhold/infrastructure/instance/lease.py::WriteSession.rmdir": PRIMITIVE,
        # System emitters (closed list)
        f"{_FSOPS}/facade.py::Fsops._system_empty": PRIMITIVE,  # checks the emitter's perimeter
        f"{_FSOPS}/writer.py::_discard": "staging_orphan_cleanup",  # a failed write's staged file
        # The process's own mechanism
        f"{_FSOPS}/facade.py::_claim_lease": OWN_SCRATCH,
        "shelfhold/infrastructure/migrate.py::MigrationStateFile.clear": OWN_SCRATCH,
        "shelfhold/infrastructure/migrate.py::_restore_database": OWN_SCRATCH,
        "shelfhold/infrastructure/secrets/atomic.py::write_private": OWN_SCRATCH,
        "shelfhold/export/application/intents.py::IntentJournal._resolve_rmdir": REPLAY,
    }
)
