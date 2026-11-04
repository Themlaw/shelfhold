"""A synthetic /library for the fsops tests: real directories, real sentinels, no mocks of the OS.

`books` stands for a sub-disk mounted under /library: it carries its own sentinel and its own
`.shelfhold/` (EF-EXP-059). `upstream` is the read-only adoption volume (EF-EXP-060):
mounted `:ro`, so it has no sentinel.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from shelfhold.export.domain.path_map import PathEntry, PathMap, PathMode
from shelfhold.export.domain.physical_roots import ExpectedRoot, SentinelDocument
from ulid import ULID

from shelfhold.infrastructure.fsops import Fsops, Strategy, create_sentinel

INSTANCE_ID = str(ULID())
STRATEGIES = [Strategy.OPENAT2, Strategy.FALLBACK]


@dataclass
class Env:
    tmp: Path
    library: Path
    manga: Path
    comics: Path
    books: Path
    upstream: Path
    path_map: PathMap
    expected: list[ExpectedRoot]
    fsops: Fsops

    def sentinel_of(self, root: Path) -> dict[str, object]:
        return json.loads((root / ".shelfhold-mount-id").read_text(encoding="utf-8"))


def make_env(
    tmp: Path,
    *,
    strategy: Strategy = Strategy.AUTO,
    umask: int | None = None,
    generation: int = 3,
    sentinels: tuple[str, ...] = ("library", "books"),
) -> Env:
    library = tmp / "library"
    manga, comics, books = library / "manga", library / "comics", library / "books"
    upstream = tmp / "upstream" / "suwayomi"
    for directory in (manga, comics, books, upstream):
        directory.mkdir(parents=True)
    path_map = PathMap(
        (
            PathEntry(str(manga), "/manga"),
            PathEntry(str(comics), "/comics"),
            PathEntry(str(books), "/books"),
            PathEntry(str(upstream), "/suwayomi", PathMode.ADOPT_RO),
        )
    )
    expected = [
        ExpectedRoot(str(ULID()), str(library), INSTANCE_ID, generation),
        ExpectedRoot(str(ULID()), str(books), INSTANCE_ID, generation),
    ]
    names = {"library": library, "books": books}
    for root in expected:
        for name, path in names.items():
            if root.path == str(path) and name in sentinels:
                create_sentinel(
                    path,
                    SentinelDocument(
                        root_id=root.root_id, instance_id=root.instance_id, generation=generation
                    ),
                )
    fs = Fsops.open(path_map, expected, umask=umask, strategy=strategy)
    return Env(tmp, library, manga, comics, books, upstream, path_map, expected, fs)
