"""What the database says of one root now, in one read (I39, EF-INT-026)."""

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class RootReading:
    """`generation` and `restore_epoch` as committed, `frozen`: the database holds a freeze for
    the instance or for this root alone. Read in one connection, so they agree with each other."""

    generation: int
    restore_epoch: int
    frozen: bool


#: `(root path, root id) -> reading`; None for a root the database does not know under both.
GenerationSource = Callable[[str, str], RootReading | None]
