"""An existing Kavita library on disk, as the simulated Kavita indexes it, and the adopter over it.

Real files (synthetic CBZ with a ComicInfo), a real database, the simulated Kavita. The tree
under the destination root is what the admin already had: Shelfhold wrote none of it.
"""

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from shelfhold.export.application.adopt import Adopter
from shelfhold.export.targets.kavita.audit import AccessAuditor
from tests.export_env import ExportEnv
from tests.kavita_export_env import LIBRARY_ID, REMOTE_ROOT, Wired, wire
from tests.library_env import cbz_bytes


def comicinfo(
    series: str,
    *,
    number: str = "",
    language: str = "fr",
    age: str | None = None,
    localized: str | None = None,
    sort: str | None = None,
) -> bytes:
    parts = [f"<Series>{escape(series)}</Series>", f"<LanguageISO>{language}</LanguageISO>"]
    if number:
        parts.append(f"<Number>{number}</Number>")
    if localized:
        parts.append(f"<LocalizedSeries>{escape(localized)}</LocalizedSeries>")
    if sort:
        parts.append(f"<SeriesSort>{escape(sort)}</SeriesSort>")
    if age:
        parts.append(f"<AgeRating>{age}</AgeRating>")
    body = "".join(parts)
    return f'<?xml version="1.0" encoding="utf-8"?><ComicInfo>{body}</ComicInfo>'.encode()


def tree_state(root: Path) -> dict[str, tuple[str, int, int, int]]:
    """Every file under `root`: digest, mtime, inode and size. Adoption must leave it as is."""
    found: dict[str, tuple[str, int, int, int]] = {}
    for base, _, names in os.walk(root):
        for name in names:
            path = Path(base) / name
            stat = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            found[str(path.relative_to(root))] = (digest, stat.st_mtime_ns, stat.st_ino, stat.st_size)
    return found


@dataclass
class Adoption:
    wired: Wired
    adopter: Adopter
    series: dict[str, int] = field(default_factory=dict)

    @property
    def env(self) -> ExportEnv:
        return self.wired.env

    def add_series(
        self,
        folder: str,
        files: Mapping[str, bytes | None] | list[str],
        *,
        name: str | None = None,
        info: bytes | None = None,
    ) -> int:
        """The folder with its files (a name alone gets a synthetic CBZ) and the series Kavita
        indexed for it. `info` is the ComicInfo written in every file."""
        directory = self.env.root / folder
        directory.mkdir(parents=True, exist_ok=True)
        remote: list[str] = []
        items = dict.fromkeys(files) if isinstance(files, list) else dict(files)
        for index, (filename, data) in enumerate(items.items()):
            pages = ((f"{filename}-{index}.png", b"\x89PNG\r\n\x1a\n" + filename.encode()),)
            (directory / filename).write_bytes(data or cbz_bytes(pages, comicinfo=info))
            remote.append(f"{REMOTE_ROOT}/{folder}/{filename}")
        sid = self.wired.fake.add_series(
            name or folder, f"{REMOTE_ROOT}/{folder}", remote, library_id=LIBRARY_ID
        )
        self.series[folder] = sid
        return sid

    def state(self) -> dict[str, tuple[str, int, int, int]]:
        return tree_state(self.env.lib.tmp)


def adoption(env: ExportEnv, **fake_options: object) -> Adoption:
    wired = wire(env, **fake_options)
    auditor = AccessAuditor(env.engine, env.clock, wired.events)
    adopter = Adopter(
        env.engine, env.lib.fsops, locate=env.lib.locate, clock=env.clock, auditor=auditor
    )
    return Adoption(wired, adopter)
