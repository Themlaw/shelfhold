"""The path map: where Shelfhold writes, and where Kavita sees the same folders (EF-EXP-003).

Pure and lexical. Nothing here touches the file system, and a path is never normalised across a
`..`: a path that climbs is refused, not folded into another entry (ENF-SEC-012).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["PathEntry", "PathMap", "PathMapError", "PathMode", "clean_absolute", "within"]

DEFAULT_PAIRS = (
    ("/library/manga", "/manga"),
    ("/library/books", "/books"),
    ("/library/comics", "/comics"),
)


class PathMapError(ValueError):
    """The path is not absolute, climbs with `..`, or lies outside every entry."""


class PathMode(StrEnum):
    RW = "rw"
    # A dedicated read-only mount (EF-EXP-060): readable for adoption and repatriation, never a
    # write target.
    ADOPT_RO = "adopt_ro"


def clean_absolute(path: str) -> str:
    """Absolute path without `.`, empty parts or a trailing slash; `..` and NUL are refused."""
    if not path.startswith("/") or "\0" in path:
        raise PathMapError(f"not an absolute path: {path!r}")
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise PathMapError(f"path climbs with '..': {path!r}")
    return "/" + "/".join(parts)


def within(path: str, base: str) -> str | None:
    """The part of `path` below `base` ('' for `base` itself), or None when outside."""
    if base == "/":
        return path[1:]
    if path == base:
        return ""
    if path.startswith(base + "/"):
        return path[len(base) + 1 :]
    return None


@dataclass(frozen=True)
class PathEntry:
    local: str
    remote: str
    mode: PathMode = PathMode.RW

    def __post_init__(self) -> None:
        object.__setattr__(self, "local", clean_absolute(self.local))
        object.__setattr__(self, "remote", clean_absolute(self.remote))
        if self.local == "/" or self.remote == "/":
            raise PathMapError("an entry cannot be the file system root")


@dataclass(frozen=True)
class PathMap:
    entries: tuple[PathEntry, ...]

    def __post_init__(self) -> None:
        locals_ = [entry.local for entry in self.entries]
        remotes = [entry.remote for entry in self.entries]
        if len(set(locals_)) != len(locals_):
            raise PathMapError("two entries share a local path")
        if len(set(remotes)) != len(remotes):
            raise PathMapError("two entries share a remote path")

    @classmethod
    def default(cls) -> "PathMap":
        return cls(tuple(PathEntry(local, remote) for local, remote in DEFAULT_PAIRS))

    @classmethod
    def same_path(cls, locals_: Iterable[str]) -> "PathMap":
        """The option where Kavita mounts the folders at the very same paths."""
        return cls(tuple(PathEntry(local, local) for local in locals_))

    @classmethod
    def parse(cls, text: str) -> "PathMap":
        """`local=remote[@adopt_ro]` pairs separated by commas; empty means the default."""
        if not text.strip():
            return cls.default()
        entries: list[PathEntry] = []
        for item in text.split(","):
            pair, _, mode = item.strip().partition("@")
            local, sep, remote = pair.partition("=")
            if not sep:
                raise PathMapError(f"expected local=remote, got {item!r}")
            try:
                path_mode = PathMode(mode) if mode else PathMode.RW
            except ValueError:
                raise PathMapError(f"unknown mode {mode!r}") from None
            entries.append(PathEntry(local.strip(), remote.strip(), path_mode))
        return cls(tuple(entries))

    def dump(self) -> str:
        return ",".join(
            f"{e.local}={e.remote}" + ("" if e.mode is PathMode.RW else f"@{e.mode.value}")
            for e in self.entries
        )

    def writable(self) -> tuple[PathEntry, ...]:
        return tuple(e for e in self.entries if e.mode is PathMode.RW)

    def entry_for(self, local: str) -> PathEntry:
        entry, _ = self._match_local(clean_absolute(local))
        return entry

    def to_remote(self, local: str) -> str:
        """The path Kavita sees for a local one (the folder to ask it to scan, for instance)."""
        entry, below = self._match_local(clean_absolute(local))
        return entry.remote + ("/" + below if below else "")

    def to_local(self, remote: str) -> str:
        path = clean_absolute(remote)
        best: tuple[PathEntry, str] | None = None
        for entry in self.entries:
            below = within(path, entry.remote)
            if below is not None and (best is None or len(entry.remote) > len(best[0].remote)):
                best = (entry, below)
        if best is None:
            raise PathMapError(f"outside every entry: {remote!r}")
        entry, below = best
        return entry.local + ("/" + below if below else "")

    def _match_local(self, path: str) -> tuple[PathEntry, str]:
        best: tuple[PathEntry, str] | None = None
        for entry in self.entries:
            below = within(path, entry.local)
            if below is not None and (best is None or len(entry.local) > len(best[0].local)):
                best = (entry, below)
        if best is None:
            raise PathMapError(f"outside every entry: {path!r}")
        return best
