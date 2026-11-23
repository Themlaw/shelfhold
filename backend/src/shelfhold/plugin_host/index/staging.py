"""The staging file of a wheel fetched from a signed plugin index (M6 L07, EF-SRC-017, D14).

The one writer of the index fetch: a scratch file the fetch makes itself in the destination the
caller gave (the plugins volume), never a library path, removed the moment the index's word does
not hold. The service that fetches never touches the file system directly.
"""

import tempfile
from pathlib import Path

__all__ = ["discard_staged_wheel", "stage_wheel"]


def stage_wheel(destination: Path) -> tuple[int, Path]:
    """Make the destination and open one staging file in it, never the wheel's final name."""
    destination.mkdir(parents=True, exist_ok=True)
    handle, staged = tempfile.mkstemp(dir=destination, prefix=".index-wheel-", suffix=".whl")
    return handle, Path(staged)


def discard_staged_wheel(path: Path) -> None:
    """Remove a staged wheel the index did not vouch for; nothing of a refusal stays on disk."""
    path.unlink(missing_ok=True)
