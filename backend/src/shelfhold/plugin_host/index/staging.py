"""The staging file of a wheel fetched from a signed plugin index (M6 L07, EF-SRC-017, D14).

The one writer of the index fetch: a scratch file the fetch makes itself in the destination the
caller gave (the plugins volume), never a library path, removed the moment the index's word does
not hold. The service that fetches never touches the file system directly.

Since M6 L08 a staged wheel can carry the name the index vouches for: the installer reads the
tags of the *filename*, so a wheel staged for an install keeps its own name, alone in a scratch
folder of the destination.
"""

import tempfile
from pathlib import Path

__all__ = ["discard_staged_wheel", "stage_wheel"]

_PREFIX = ".index-wheel-"


def stage_wheel(destination: Path, name: str | None = None) -> tuple[int, Path]:
    """Make the destination and open one staging file in it, never the wheel's final name —
    unless the caller names the wheel: the file then carries the name the index vouches for
    (the installer reads the tags of the filename), alone in its own scratch folder."""
    destination.mkdir(parents=True, exist_ok=True)
    if name is None:
        handle, staged = tempfile.mkstemp(dir=destination, prefix=_PREFIX, suffix=".whl")
        return handle, Path(staged)
    folder = Path(tempfile.mkdtemp(dir=destination, prefix=_PREFIX))
    handle, staged = tempfile.mkstemp(dir=folder, prefix=".staged-", suffix=".whl")
    named = folder / name
    Path(staged).rename(named)  # the open handle follows: the file is the wheel's own name now
    return handle, named


def discard_staged_wheel(path: Path) -> None:
    """Remove a staged wheel the index did not vouch for; nothing of a refusal stays on disk."""
    path.unlink(missing_ok=True)
    if path.parent.name.startswith(_PREFIX):
        path.parent.rmdir()  # the wheel was the folder's only file
