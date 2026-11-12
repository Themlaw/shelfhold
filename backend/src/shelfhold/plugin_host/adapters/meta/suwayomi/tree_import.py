"""Import an existing Suwayomi download tree without downloading it again (EF-INT-009).

The tree is `[mangas/]<source>/<series>/<chapter>`, a chapter being a CBZ or a folder of images. It
belongs to Suwayomi, not to us: it is only ever **read**. Nothing is removed from it, nothing is
written into it (not even the import folder's quarantine), and the same walk and the same checks
as the import folder (`library/adapters/import_scan.py`) keep it safe: no link followed, no special
file, no other disk, no name that could be read as a path, a settling time for what is still being
written, every file read by descriptor.

A CBZ goes through the import folder's road unchanged: staged and hashed, the allowlist and the
tombstones asked first, stored by the same store as any received file, as `untrusted`. A folder of
images is packaged into a CBZ first (`write_cbz`), from pages whose names are never used: each page
is renamed in reading order after the kind its own bytes show, so a name made to climb out of a
folder or to hide a script has nothing to climb out of, and the result passes the very same
allowlist (images by magic bytes, one ComicInfo) before anything is stored.

Chapters are attached to the Suwayomi bindings that already exist (see `TreeTargets`); a series
with no binding is left alone, a purged one is refused.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Final, cast

from shelfhold.acquisition.public import SeriesBinding, TreeTargets, chapter_number, series_key
from shelfhold.infrastructure.content_trust import sniff_image_kind
from shelfhold.infrastructure.packaging.cbz.packager import write_cbz
from shelfhold.infrastructure.packaging.comicinfo import ComicInfoData, render_comicinfo
from shelfhold.library.adapters.import_scan import (
    ARCHIVE,
    LINK,
    MAX_DEPTH,
    MAX_ENTRIES,
    NAME_COLLISION,
    NOT_READY,
    TOMBSTONED,
    TOO_LARGE,
    UNSUPPORTED,
    Found,
    ImportFolder,
    ImportItem,
    ImportOutcome,
    Verdict,
    check_name,
    collision_key,
    display_path,
)

ENTRY_PREFIX: Final = "suwayomi-tree/"
TREE_DIR: Final = "mangas"
NO_SERIES: Final = "import.tree_no_series"
AMBIGUOUS_SERIES: Final = "import.tree_ambiguous_series"
UNNUMBERED: Final = "import.tree_unnumbered"
DUPLICATE: Final = "import.tree_duplicate"
MAX_PAGES: Final = 5_000
MAX_PAGE_BYTES: Final = 64 * 1024 * 1024
_EXTENSION: Final = {"jpeg": "jpg", "png": "png", "webp": "webp", "gif": "gif", "avif": "avif",
                     "jxl": "jxl"}  # fmt: skip
_DIGITS = re.compile(r"(\d+)")


def _natural(name: str) -> list[Any]:
    return [int(p) if p.isdigit() else p.casefold() for p in _DIGITS.split(name)]


@dataclass(frozen=True)
class Page:
    name: str
    size: int
    mtime_ns: int
    ino: int


@dataclass(frozen=True)
class Chapter:
    found: Found  # a CBZ: the file. A folder: the folder (size: its pages', time: the newest)
    series: str
    name: str
    pages: tuple[Page, ...] | None = None  # None: a CBZ file


@dataclass(frozen=True)
class TreeReport:
    ready: tuple[str, ...]  # escaped paths of what a dry run says would be taken
    refused: tuple[Verdict, ...]
    waiting: tuple[Verdict, ...]
    outcomes: tuple[ImportOutcome, ...]


class _Changed(Exception):
    """A page changed or went away while the chapter was read."""


class _Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SuwayomiTree(ImportFolder):
    def __init__(self, root: Path, *, targets: TreeTargets, **options: Any) -> None:
        self._targets = targets
        self._plans: dict[str, tuple[SeriesBinding, str, str]] = {}
        inner = options.get("precheck")

        def precheck(staged: Path, kind: str, sha: str, trust: str) -> str | None:
            if kind != "cbz":
                return UNSUPPORTED  # a chapter is a CBZ, whatever the name of the file says
            return inner(staged, kind, sha, trust) if inner is not None else None

        options.update(keep_source=True, entry_prefix=ENTRY_PREFIX, precheck=precheck)
        super().__init__(root, target=self._target_of, **options)

    def _target_of(self, item: ImportItem) -> tuple[str, str]:
        series, number, title = self._plans[item.rel]
        return self._targets.target(series, item.rel, number, title)

    # -- looking ------------------------------------------------------------------------------

    def look(self) -> tuple[list[Chapter], list[Verdict], list[Verdict]]:
        """The chapters of the tree that are ready, what is refused and what is still waiting."""
        refused: list[Verdict] = []
        waiting: list[Verdict] = []
        found: list[Chapter] = []
        root_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            device = os.fstat(root_fd).st_dev
            counter = [0]
            self._levels(root_fd, (), device, found, refused, waiting, counter)
            self._sleep(self._recheck)
            ready = [c for c in found if self._same(root_fd, c, waiting)]
        finally:
            os.close(root_fd)
        return ready, refused, waiting

    def _same(self, root_fd: int, chapter: Chapter, waiting: list[Verdict]) -> bool:
        """Still what the first look saw, a pause later (not being written)."""
        folder = chapter.pages is not None
        again = self._signature(root_fd, chapter.found.rel, folder)
        if again is None or again != (chapter.pages if folder else chapter.found):
            waiting.append(Verdict(display_path(chapter.found.rel), NOT_READY))
            return False
        return True

    def _signature(self, root_fd: int, rel: str, folder: bool) -> Any:
        try:
            fd = self._open_under(root_fd, rel.split("/"), directory=folder)
        except OSError:
            return None
        try:
            info = os.fstat(fd)
            if not folder:
                return Found(rel, info.st_size, info.st_mtime_ns, info.st_ino)
            return self._pages_of(fd)
        except (OSError, _Refused):
            return None
        finally:
            os.close(fd)

    def _levels(
        self, directory_fd: int, parents: tuple[str, ...], device: int, found: list[Chapter],
        refused: list[Verdict], waiting: list[Verdict], counter: list[int],
    ) -> None:  # fmt: skip
        """`[mangas/]source/series/chapter`: the depth says what a folder is."""
        base = parents
        if not parents and self._is_dir(directory_fd, TREE_DIR):
            base = (TREE_DIR,)
        if base != parents:
            child = self._step(directory_fd, TREE_DIR, device)
            if child is None:
                refused.append(Verdict(TREE_DIR, LINK))
                return
            try:
                self._levels_in(child, base, device, 0, found, refused, waiting, counter)
            finally:
                os.close(child)
            return
        self._levels_in(directory_fd, parents, device, 0, found, refused, waiting, counter)

    @staticmethod
    def _is_dir(directory_fd: int, name: str) -> bool:
        try:
            return stat.S_ISDIR(os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode)
        except OSError:
            return False

    def _step(self, directory_fd: int, name: str, device: int) -> int | None:
        """One folder down, by descriptor, never through a link, never onto another disk or a
        root of ours."""
        try:
            fd = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory_fd,
            )  # fmt: skip
        except OSError:
            return None
        info = os.fstat(fd)
        if info.st_dev != device or (info.st_dev, info.st_ino) in self._apart:
            os.close(fd)
            return None
        return fd

    def _levels_in(
        self, directory_fd: int, parents: tuple[str, ...], device: int, depth: int,
        found: list[Chapter], refused: list[Verdict], waiting: list[Verdict], counter: list[int],
    ) -> None:  # fmt: skip
        """`depth` 0: sources, 1: series, 2: chapters (below `mangas/` when it is there)."""
        with os.scandir(directory_fd) as entries:
            listing = sorted(entries, key=lambda e: e.name)
        groups: dict[str, int] = {}
        for entry in listing:
            key = collision_key(entry.name)
            groups[key] = groups.get(key, 0) + 1
        for entry in listing:
            counter[0] += 1
            rel = "/".join((*parents, entry.name))
            shown = display_path(rel)
            if counter[0] > MAX_ENTRIES or len(parents) >= MAX_DEPTH:
                refused.append(Verdict(shown, ARCHIVE))
                continue
            code = check_name(entry.name)
            if code is None and groups[collision_key(entry.name)] > 1:
                code = NAME_COLLISION
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                waiting.append(Verdict(shown, NOT_READY))
                continue
            if code is not None:
                refused.append(Verdict(shown, code))
            elif stat.S_ISLNK(info.st_mode):
                refused.append(Verdict(shown, LINK))
            elif stat.S_ISDIR(info.st_mode):
                self._enter(
                    directory_fd,
                    entry.name,
                    parents,
                    device,
                    depth,
                    found,
                    refused,
                    waiting,
                    counter,
                    shown,
                )
            elif not stat.S_ISREG(info.st_mode):
                refused.append(Verdict(shown, UNSUPPORTED))  # a pipe, a socket, a device
            elif depth == 2 and entry.name.lower().endswith(".cbz"):
                self._file_chapter(entry.name, rel, parents, info, device, found, refused, waiting)
            # any other regular file (a cover, a note) is not a chapter and is left alone

    def _enter(
        self, directory_fd: int, name: str, parents: tuple[str, ...], device: int, depth: int,
        found: list[Chapter], refused: list[Verdict], waiting: list[Verdict], counter: list[int],
        shown: str,
    ) -> None:  # fmt: skip
        child = self._step(directory_fd, name, device)
        if child is None:
            refused.append(Verdict(shown, LINK))  # another disk, a root of ours, or swapped
            return
        try:
            here = (*parents, name)
            if depth < 2:
                self._levels_in(child, here, device, depth + 1, found, refused, waiting, counter)
            else:
                self._folder_chapter(child, here, found, refused, waiting)
        finally:
            os.close(child)

    def _file_chapter(
        self, name: str, rel: str, parents: tuple[str, ...], info: os.stat_result, device: int,
        found: list[Chapter], refused: list[Verdict], waiting: list[Verdict],
    ) -> None:  # fmt: skip
        shown = display_path(rel)
        if info.st_nlink > 1 or info.st_dev != device:
            refused.append(Verdict(shown, LINK))  # a hard link may be a file elsewhere
        elif self._now() - info.st_mtime_ns / 1e9 < self._settle:
            waiting.append(Verdict(shown, NOT_READY))  # Suwayomi may be writing it
        else:
            chapter = Found(rel, info.st_size, info.st_mtime_ns, info.st_ino)
            found.append(Chapter(chapter, parents[-1], name))

    def _folder_chapter(
        self, fd: int, here: tuple[str, ...], found: list[Chapter], refused: list[Verdict],
        waiting: list[Verdict],
    ) -> None:  # fmt: skip
        rel = "/".join(here)
        shown = display_path(rel)
        try:
            pages = self._pages_of(fd)
        except _Refused as error:
            refused.append(Verdict(shown, error.code))
            return
        if not pages:
            return  # an empty folder: nothing was downloaded
        newest = max(p.mtime_ns for p in pages)
        if self._now() - newest / 1e9 < self._settle:
            waiting.append(Verdict(shown, NOT_READY))
            return
        size = sum(p.size for p in pages)
        info = os.fstat(fd)
        found.append(Chapter(Found(rel, size, newest, info.st_ino), here[-2], here[-1], pages))

    def _pages_of(self, fd: int) -> tuple[Page, ...]:
        """The files of an image folder, in reading order, each checked: a regular file, alone on
        its inode, on this disk, with a name that is only a name. One that is not refuses the
        whole chapter: half of one is never stored."""
        device = os.fstat(fd).st_dev
        with os.scandir(fd) as entries:
            listing = sorted(entries, key=lambda e: _natural(e.name))
        pages: list[Page] = []
        seen: set[str] = set()
        for entry in listing:
            if len(listing) > MAX_PAGES:
                raise _Refused(ARCHIVE)
            code = check_name(entry.name)
            if code is not None:
                raise _Refused(code)  # asked first: a hidden name is a name like any other
            if entry.name.startswith("."):
                continue  # a hidden note of a file manager
            key = collision_key(entry.name)
            if key in seen:
                raise _Refused(NAME_COLLISION)
            seen.add(key)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                raise _Refused(NOT_READY) from None
            if stat.S_ISLNK(info.st_mode) or info.st_nlink > 1 or info.st_dev != device:
                raise _Refused(LINK)
            if not stat.S_ISREG(info.st_mode):
                raise _Refused(UNSUPPORTED)  # a pipe, a device, a folder inside a chapter
            if info.st_size > MAX_PAGE_BYTES:
                raise _Refused(TOO_LARGE)
            pages.append(Page(entry.name, info.st_size, info.st_mtime_ns, info.st_ino))
        return tuple(pages)

    # -- taking in ----------------------------------------------------------------------------

    def import_tree(self, *, dry_run: bool = False) -> TreeReport:
        ready, refused, waiting = self.look()
        for verdict in refused:
            self._record(verdict.rel, 0, 0, None, "refused", verdict.code)
        if dry_run:
            return TreeReport(
                tuple(display_path(c.found.rel) for c in ready), tuple(refused), tuple(waiting), ()
            )
        series_of = self._targets.bindings()
        outcomes: list[ImportOutcome] = []
        root_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for chapter in ready:
                outcomes.append(self._chapter(root_fd, chapter, series_of))
        finally:
            os.close(root_fd)
        return TreeReport((), tuple(refused), tuple(waiting), tuple(outcomes))

    def _chapter(
        self, root_fd: int, chapter: Chapter, series_of: dict[str, list[SeriesBinding]]
    ) -> ImportOutcome:
        found = chapter.found
        shown = display_path(found.rel)
        known = self._known(found)
        if known is not None:
            return ImportOutcome(shown, "refused", known)
        matches = series_of.get(series_key(chapter.series), [])
        if len(matches) != 1:
            code = NO_SERIES if not matches else AMBIGUOUS_SERIES
            return ImportOutcome(shown, "skipped", code)
        series = matches[0]
        if self._targets.blocked(series):
            return self._refuse(found, shown, TOMBSTONED)  # a purged series stays purged
        number = chapter_number(chapter.name)
        if number is None:
            return ImportOutcome(shown, "skipped", UNNUMBERED)
        if self._targets.duplicate(series, number):
            return ImportOutcome(shown, "skipped", DUPLICATE)
        title = chapter.name.removesuffix(".cbz")
        self._plans[found.rel] = (series, number, title)
        try:
            if chapter.pages is None:
                return self._import(root_fd, found, shown, False)
            return self._import_folder(root_fd, chapter, series, number, title, shown)
        finally:
            self._plans.pop(found.rel, None)

    def _import_folder(
        self, root_fd: int, chapter: Chapter, series: SeriesBinding, number: str, title: str,
        shown: str,
    ) -> ImportOutcome:  # fmt: skip
        found = chapter.found
        try:
            fd = self._open_under(root_fd, found.rel.split("/"), directory=True)
        except FileNotFoundError:
            return ImportOutcome(shown, "waiting", NOT_READY)
        except OSError:
            return self._refuse(found, shown, LINK)
        try:
            with tempfile.NamedTemporaryFile(
                dir=self._staging, prefix="import-", suffix=".bin"
            ) as sink:
                info = ComicInfoData(series=series.title, title=title, number=number)
                comicinfo = render_comicinfo(info).xml
                try:
                    write_cbz(self._read_pages(fd, chapter), comicinfo, cast(BinaryIO, sink))
                except _Refused as error:
                    return self._refuse(found, shown, error.code)
                except _Changed:
                    return ImportOutcome(shown, "waiting", "import.changed_while_reading")
                sink.flush()
                sha = self._hash_file(sink.name)
                return self._file(root_fd, Path(sink.name), sha, found, shown, False)
        except OSError as error:
            return self._failed(found, shown, error)
        finally:
            os.close(fd)

    def _read_pages(self, fd: int, chapter: Chapter) -> Iterator[tuple[str, bytes]]:
        """Each page read by descriptor from the folder, one at a time, checked against what the
        walk saw; named by its place and by what its bytes are (never by its own name)."""
        total = 0
        assert chapter.pages is not None  # noqa: S101 - a folder always has its pages
        for index, page in enumerate(chapter.pages, start=1):
            try:
                page_fd = os.open(
                    page.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                    dir_fd=fd,
                )  # fmt: skip
            except OSError:
                raise _Changed from None
            with os.fdopen(page_fd, "rb", closefd=True) as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                    raise _Refused(LINK)
                if (info.st_size, info.st_mtime_ns, info.st_ino) != (
                    page.size, page.mtime_ns, page.ino,
                ):  # fmt: skip
                    raise _Changed
                data = stream.read(MAX_PAGE_BYTES + 1)
            total += len(data)
            if len(data) > MAX_PAGE_BYTES or total > self._max:
                raise _Refused(TOO_LARGE)
            kind = sniff_image_kind(data[:16])
            extension = _EXTENSION.get(kind or "")
            if extension is None:
                raise _Refused(ARCHIVE)  # not an image, whatever its name says
            yield f"{index:05d}.{extension}", data

    @staticmethod
    def _hash_file(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            while chunk := stream.read(1 << 20):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()


__all__ = ["Chapter", "SuwayomiTree", "TreeReport", "build_tree"]


def build_tree(root: Path, *, targets: TreeTargets, **options: Any) -> SuwayomiTree:
    return SuwayomiTree(root, targets=targets, **options)
