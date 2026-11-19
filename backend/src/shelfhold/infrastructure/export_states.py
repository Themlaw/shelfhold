"""States of an export record, as plain values (no framework: domains may import it)."""

from typing import Final

__all__ = ["LIVE_STATES"]

# States of an export record that stand for a file on the destination (shared by the reconcile
# and by the reading side, which asks whether a profile reads the file elsewhere).
LIVE_STATES: Final = frozenset({"written", "scanned", "resolved", "adopted"})
