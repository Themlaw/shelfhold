"""Working data kept in /data while a repair waits (EF-BIB-012).

The fresh bytes of a Unit whose shelf refuses the Edition wait in `/data/integrity/held/`; they
are never a library or /store path. Removing one is the only deletion here: a regular file under
a folder the caller names, once it has been installed or refused.
"""

from pathlib import Path

__all__ = ["remove_held_file"]


def remove_held_file(path: Path) -> None:
    """Remove one regular working file; a link or a missing file is left alone."""
    if path.is_file() and not path.is_symlink():
        path.unlink()
