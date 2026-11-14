"""A physical root as the removal and the detachment see it (M3 L30)."""

from collections.abc import Callable
from dataclasses import dataclass

from shelfhold.export.application.snapshot import DestinationContext
from shelfhold.export.domain.path_map import within

__all__ = ["CopyScope", "RootDirectory", "RootRef", "copy_scope"]


@dataclass(frozen=True)
class RootRef:
    """A physical root: its id (the database's) and its path on this machine."""

    root_id: str
    path: str


RootDirectory = Callable[[str], RootRef | None]  # a local path -> the physical root holding it


@dataclass(frozen=True)
class CopyScope:
    """Where the `destination` copies of one destination live: a root and a folder under it
    (`""` for the whole root). Nothing outside it is the destination's to forget."""

    root_id: str
    under: str


def copy_scope(ctx: DestinationContext, roots: RootDirectory) -> CopyScope | None:
    """None when the destination's folder is on no known root: no copy row is then touched."""
    if ctx.root_local is None:
        return None
    ref = roots(ctx.root_local)
    if ref is None:
        return None
    rel = within(ctx.root_local, ref.path)
    return None if rel is None else CopyScope(ref.root_id, rel.strip("/"))
