"""What each kind of job is, said once where the kinds are declared (EF-INT-017, EF-INT-018).

Two traits matter to the control of the queue, and neither is a list of names kept somewhere else:

- `security_repair`: the job repairs what protects the library (a copy that keeps a file safe, the
  coherence of the disks against the database, a child account that sees an adult library). It
  runs above every class a user can pick, a pause does not park it, and only a fresh session may
  cancel it. A new kind that protects something is declared here, with its trait, or it is a
  user job: `test_every_declared_kind_is_a_registered_kind` fails for a name nobody registers.
- `cancellable`: the handler reads `JobContext.stop` between two steps, so that a cancel asked
  while it runs really stops it. For any other kind a cancel of a running job is answered `too_late`
  (it finishes, and the ledger says what happened), never recorded as a cancellation that stopped
  nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = ["KindTraits", "security_repair_kinds", "traits_of"]


@dataclass(frozen=True)
class KindTraits:
    security_repair: bool = False
    cancellable: bool = False


_REPAIR: Final = KindTraits(security_repair=True, cancellable=True)
_STEPS: Final = KindTraits(cancellable=True)

DECLARED: Final[dict[str, KindTraits]] = {
    "library.safeguard": _REPAIR,  # "Mettre à l'abri": the copy of /store that keeps a file safe
    "store.keep_catchup": _REPAIR,
    "store.index": KindTraits(security_repair=True),  # one `rewrite()`: no step to stop between
    "reconcile.cycle": _REPAIR,  # the coherence of the disks against the database
    "reconcile.hash": _REPAIR,
    "integrity_rolling": _REPAIR,  # replaces the damaged copies (L23)
    "target.access_audit": KindTraits(security_repair=True),  # EF-EXP-055; registered by L26/L37
    "export.run": _STEPS,
    "vacuum_monthly": _STEPS,
    "thumbnail_gc": _STEPS,  # stops between two files when the window closes (L24)
    "thumbnail_catchup": _STEPS,
}


def traits_of(kind: str) -> KindTraits:
    return DECLARED.get(kind, KindTraits())


def security_repair_kinds() -> frozenset[str]:
    return frozenset(kind for kind, traits in DECLARED.items() if traits.security_repair)
