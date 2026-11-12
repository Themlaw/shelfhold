"""The import folder (EF-SRC-012, EF-BIB-041, ENF-LEG-009): the one door by which a person's own
files (CBZ, EPUB, PDF) enter, and the one place that removes a file from the folder.

Everything a dropped file can do is assumed hostile, and each step answers with a code and a
message, never with silence:

- **What it is** is read in its bytes (`import_kind`), never in its name. Audio is refused until an
  audio edition exists (EF-SRC-012, passe 10); anything unknown is refused.
- **A protected file is refused** (`detect_protection`), with a message of its own for a loan or a
  purchase through a library, and **left in place**. Nothing in Shelfhold tries to lift it.
- **Where it is**: a link (symbolic or hard) is refused, so is another device; every step down the
  tree opens with `O_NOFOLLOW` from a directory descriptor, so a swap between the scan and the
  read cannot lead out of the folder. A name with `..`, a control or direction character, a
  trailing space or dot, or that collides after Unicode normalisation with another is refused.
  A name never reaches a destination path: the file is filed under the ids of its Variant.
- **Whether it is finished**: a file is read only when its size and modification time did not move
  across two looks and its age passes the settling time, and again when what was read is compared
  with what was announced. A file still being written waits.
- **Untrusted by default.** An import not declared as a purchase is `untrusted`: an EPUB goes
  through the neutralizer (`UntrustedEpubStore`), a PDF is kept and not exported (D9). A declared
  purchase is `publisher` and is flagged `watermarked`. The heuristic only suggests the flag; it
  never exempts anything.
- **Removal comes last** (`_remove_verified`): after the store accepted the file, a copy that holds
  the very bytes (same SHA-256 as the source) is read back from the disk, and the source is hashed
  once more, still the same file. Any doubt (a crash before, a different hash, a disk that is full,
  a file replaced meanwhile, an untrusted EPUB whose original was not kept) leaves the source where
  it is.
"""

import contextlib
import ctypes
import errno
import fcntl
import hashlib
import os
import stat
import tempfile
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import quote

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.infrastructure.archives import (
    DEFAULT_LIMITS,
    ArchiveLimits,
    UnsafeArchiveError,
    safe_open_archive,
)
from shelfhold.infrastructure.content_trust import (
    Destination,
    NeutralizationError,
    UntrustedEntryError,
    check_cbz_entries,
    neutralize_epub,
    sanitize_upstream_text,
)
from shelfhold.infrastructure.content_trust.protection import (
    PROTECTED,
    detect_protection,
    watermark_hint,
)
from shelfhold.infrastructure.fsops import Fsops, FsopsError
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import storage
from shelfhold.library.adapters.pdf_store import NotAPdfError, check_pdf_shape
from shelfhold.library.adapters.receive import sniff_kind
from shelfhold.library.adapters.storage import (
    DEFAULT_MAX_RECEIVED_BYTES,
    UnitGoneError,
    WorkBusyError,
)
from shelfhold.library.adapters.svg_derivative import draw_svg, svg_derivative_of
from shelfhold.library.application.anti_echo import absorb_import_echo
from shelfhold.library.domain.content_hash import FILE_HASH_BY
from shelfhold.library.domain.failures import (
    EpubRefusedError,
    ReceivedTooLargeError,
    TombstonedContentError,
    VariantAlreadyStoredError,
)
from shelfhold.library.replica import LibraryCopy, StoreCopy, library_copies, store_copies
from shelfhold.library.tombstones.values import ContentHash

log = structlog.get_logger(__name__)

# The codes the folder answers with; each has its text in `locales/fr.toml` (`errors.<code>`).
PROTECTED_FILE: Final = "import.protected"
PROTECTED_LICENCE: Final = "import.protected_licence"
PDF_REFUSED: Final = "import.pdf_refused"
AUDIO: Final = "import.audio_unsupported"
UNSUPPORTED: Final = "import.unsupported_file"
TOO_LARGE: Final = "import.too_large"
ARCHIVE: Final = "import.archive_refused"
LINK: Final = "import.link_refused"
UNSAFE_NAME: Final = "import.unsafe_name"
NAME_COLLISION: Final = "import.name_collision"
NOT_READY: Final = "import.not_ready"
PURCHASE_PENDING: Final = "import.purchase_pending"
LEFT_IN_QUARANTINE: Final = "import.left_in_quarantine"
PURCHASE_DIR: Final = "achats"  # what the admin declares a purchase, by the command only
COMPANION: Final = ".from"  # beside a held entry: the path it came from
QUARANTINE: Final = ".shelfhold-removing"  # the step between "verified" and "gone"
CHANGED: Final = "import.changed_while_reading"
HASH_MISMATCH: Final = "import.hash_mismatch"
DISK_FULL: Final = "import.disk_full"
NOT_STORED: Final = "import.not_stored"
TOMBSTONED: Final = "import.tombstoned"
OWN_EXPORT: Final = "import.own_export"  # Shelfhold's own export coming back: a replica
ORIGINAL_NOT_KEPT: Final = "import.original_not_kept"
UNTRUSTED_PDF: Final = "content.untrusted_pdf"  # a note on a stored PDF, not a refusal (D9)
REFUSED_CODES: Final = (
    PROTECTED_FILE, PROTECTED_LICENCE, PDF_REFUSED, AUDIO, UNSUPPORTED, TOO_LARGE, ARCHIVE, LINK,
    UNSAFE_NAME,
    NAME_COLLISION, NOT_READY, CHANGED, HASH_MISMATCH, DISK_FULL, NOT_STORED, TOMBSTONED,
    ORIGINAL_NOT_KEPT, LEFT_IN_QUARANTINE, OWN_EXPORT,
)  # fmt: skip

DEFAULT_SETTLE_SECONDS: Final = 30.0
DEFAULT_RECHECK_SECONDS: Final = 2.0
MAX_DEPTH: Final = 8
MAX_ENTRIES: Final = 20_000
MAX_NAME_BYTES: Final = 255
_IN_PROGRESS_SUFFIXES: Final = (".part", ".partial", ".tmp", ".crdownload", ".download", ".swp")
_AUDIO_SUFFIXES: Final = (
    ".mp3", ".m4a", ".m4b", ".m4p", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wav", ".wma",
    ".aiff", ".aif",
)  # fmt: skip
_AUDIO_BRANDS: Final = (b"M4A ", b"M4B ", b"M4P ", b"F4A ", b"F4B ")


# -- names -------------------------------------------------------------------------------------


def check_name(name: str) -> str | None:
    """The code that refuses a file or folder name, or None. The name is only ever read, shown
    (escaped) and turned into a title: never joined to a path."""
    if name in ("", ".", "..") or "/" in name or "\\" in name:
        return UNSAFE_NAME
    if name != name.rstrip(" .") or name != name.lstrip(" "):
        return UNSAFE_NAME  # a trailing dot or space is another name on some file systems
    for char in name:
        if unicodedata.category(char) in {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}:
            return UNSAFE_NAME  # controls, direction marks, lone surrogates, line separators
    if len(name.encode("utf-8", "surrogateescape")) > MAX_NAME_BYTES:
        return UNSAFE_NAME
    return None


def collision_key(name: str) -> str:
    """Two names with the same key are one name to a person, to a mac and to a search."""
    return unicodedata.normalize("NFKC", name).casefold()


def display_path(rel: str) -> str:
    """A relative path that can be printed and stored: printable ASCII only, every other character
    (a control, an escape sequence, a direction mark, a lone surrogate) as a visible escape."""
    out: list[str] = []
    for char in rel:
        code = ord(char)
        if char == "\\":
            out.append("\\\\")
        elif 0x20 <= code <= 0x7E:
            out.append(char)
        elif code < 0x100:
            out.append(f"\\x{code:02x}")
        elif code < 0x10000:
            out.append(f"\\u{code:04x}")
        else:
            out.append(f"\\U{code:08x}")
    return "".join(out)


def _title_of(name: str) -> str:
    stem = name.rsplit(".", 1)[0] if "." in name else name
    clean = sanitize_upstream_text(stem, destination=Destination.PLAIN, max_length=200).strip()
    return clean or "import"


# -- what a file is ----------------------------------------------------------------------------

_HEAD: Final = 4096


def _head(path: Path) -> bytes:
    with path.open("rb") as stream:
        return stream.read(_HEAD)


def is_audio_head(head: bytes) -> bool:
    if head[:3] == b"ID3" or head[:4] in (b"OggS", b"fLaC", b"MThd"):
        return True
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        return True  # an MPEG audio frame header
    if head[:4] in (b"RIFF", b"FORM") and head[8:12] in (b"WAVE", b"AIFF", b"AIFC"):
        return True
    return head[4:8] == b"ftyp" and head[8:12] in _AUDIO_BRANDS


def import_kind(path: Path, name: str = "") -> str:
    """`epub`, `cbz`, `pdf`, `audio` or `unknown`, from the bytes (the name only breaks a tie for
    audio: a file called `.m4b` that is nothing else is still refused as audio)."""
    head = _head(path)
    if head[: len(b"%PDF-")] == b"%PDF-":
        return "pdf"
    if is_audio_head(head):
        return "audio"
    if head[:4] == b"PK\x03\x04":
        return sniff_kind(path).removeprefix(".")
    return "audio" if name.lower().endswith(_AUDIO_SUFFIXES) else "unknown"


# -- the shapes --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Found:
    rel: str  # relative to the folder, `/`-separated, raw (never printed: see `display_path`)
    size: int
    mtime_ns: int
    ino: int


@dataclass(frozen=True)
class Verdict:
    rel: str  # escaped, safe to print
    code: str


@dataclass(frozen=True)
class Scan:
    ready: tuple[Found, ...]
    waiting: tuple[Verdict, ...]
    refused: tuple[Verdict, ...]


@dataclass(frozen=True)
class ImportItem:
    """What the caller needs to create the Unit and its Variant for a file."""

    kind: str  # `cbz`, `epub`, `pdf`
    title: str  # a clean title taken from the name
    trust: str  # `publisher` (a declared purchase) or `untrusted`
    sha256: str
    rel: str = ""  # where it lay, raw and relative to the folder (never printed)


@dataclass(frozen=True)
class ImportOutcome:
    rel: str  # escaped
    state: str  # `stored`, `refused`, `waiting`, `failed`
    code: str | None = None  # why not, or a note on a stored file
    removed: bool = False  # the source is gone from the folder
    unit_id: str | None = None
    kind: str | None = None
    watermark: str | None = None  # `user` (declared), `heuristic` (suggested) or None


class Filer(Protocol):
    def ingest(self, **arguments: Any) -> Any: ...

    def finish_derivative(self, *, unit_id: str, staged: Path) -> None: ...

    def derivative_missing(self, unit_id: str, staged: Path) -> bool: ...


Target = Callable[[ImportItem], tuple[str, str]]  # item -> (unit_id, variant_id)
# (staged file, kind, sha256, trust) -> the code that refuses it, or None: asked before any Unit
Precheck = Callable[[Path, str, str, str], str | None]
Place = Callable[[str, str, str], str]  # (unit id, variant id, `.cbz`/`.epub`/`.pdf`) -> local path


def make_precheck(tombstones: Any, limits: ArchiveLimits = DEFAULT_LIMITS) -> Precheck:
    """What the stores would refuse, asked first: a purged content, a CBZ entry off the allowlist,
    an EPUB the neutralizer cannot take (an `untrusted` one only: a publisher file is not
    rewritten). Nothing is stored and nothing is kept."""

    def check(staged: Path, kind: str, sha: str, trust: str) -> str | None:
        if tombstones.check_content([ContentHash(FILE_HASH_BY, sha)]):
            return TOMBSTONED
        try:
            if kind == "cbz":
                with safe_open_archive(staged, name="import.cbz", limits=limits) as archive:
                    svgs = check_cbz_entries(archive, allow_svg=True).svgs
                if svgs:  # EF-BIB-051 (6): drawn once here, so a hostile page leaves no Unit
                    with svg_derivative_of(staged, staged.parent, draw_svg):
                        pass
            elif kind == "epub" and trust != "publisher":
                with tempfile.TemporaryFile(dir=staged.parent) as sink:
                    neutralize_epub(staged, sink, name="import.epub", limits=limits)
        except (UnsafeArchiveError, UntrustedEntryError, NeutralizationError):
            return ARCHIVE
        return None

    return check


class ImportFolderOverlapError(ValueError):
    """The import folder is, holds or sits inside a root Shelfhold writes: it would re-import what
    it exported (the echo)."""


_RENAME_NOREPLACE: Final = 1


def _rename_noreplace(old_dir: int, old: str, new_dir: int, new: str) -> None:
    """`renameat2(old_dir, old, new_dir, new, RENAME_NOREPLACE)` through the C library. Raises
    `FileExistsError` when `new` exists, `OSError` (ENOSYS, EINVAL...) where it is unsupported."""
    libc = ctypes.CDLL(None, use_errno=True)
    call = getattr(libc, "renameat2", None)
    if call is None:
        raise OSError(errno.ENOSYS, "renameat2 is not available")
    call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    call.restype = ctypes.c_int
    if call(old_dir, os.fsencode(old), new_dir, os.fsencode(new), _RENAME_NOREPLACE) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _escaped_key(rel: str) -> str:
    return quote(rel, safe="/")


# -- the folder --------------------------------------------------------------------------------


class ImportFolder:
    def __init__(
        self,
        root: Path,
        *,
        engine: Engine,
        fsops: Fsops,
        store: Filer,
        staging_dir: Path,
        target: Target,
        place: Place,
        keep_apart: tuple[Path, ...] = (),
        precheck: Precheck | None = None,
        default_trust: str = "untrusted",
        keep_source: bool = False,
        entry_prefix: str = "",
        max_bytes: int = DEFAULT_MAX_RECEIVED_BYTES,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        recheck_seconds: float = DEFAULT_RECHECK_SECONDS,
        limits: ArchiveLimits = DEFAULT_LIMITS,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._root = root.resolve()
        self._apart: set[tuple[int, int]] = set()
        for other in keep_apart:
            resolved = other.resolve()
            if self._root.is_relative_to(resolved) or resolved.is_relative_to(self._root):
                raise ImportFolderOverlapError("the import folder overlaps a root Shelfhold writes")
            try:
                info = resolved.stat()
            except OSError:
                continue
            self._apart.add((info.st_dev, info.st_ino))  # a bind mount shows the same pair
        try:
            mine = self._root.stat()
        except OSError:
            mine = None
        if mine is not None and (mine.st_dev, mine.st_ino) in self._apart:
            raise ImportFolderOverlapError("the import folder is a root Shelfhold writes")
        self._engine = engine
        self._fsops = fsops
        self._store = store
        self._staging = staging_dir
        self._target = target
        self._place = place
        self._precheck = precheck
        self._default_trust = default_trust
        self._keep_source = keep_source  # a tree that is someone else's: read, never removed
        self._prefix = entry_prefix  # its own namespace in the table of decisions
        self._max = max_bytes
        self._settle = settle_seconds
        self._recheck = recheck_seconds
        self._limits = limits
        self._now = now
        self._sleep = sleep

    # -- looking ------------------------------------------------------------------------------

    def scan(self) -> Scan:
        """Walk the folder without following anything, then look a second time after a pause."""
        found: list[Found] = []
        refused: list[Verdict] = []
        waiting: list[Verdict] = []
        counter = [0]
        root_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            device = os.fstat(root_fd).st_dev
            self._walk(root_fd, (), device, found, refused, waiting, counter)
            self._sleep(self._recheck)
            ready: list[Found] = []
            for item in found:
                again = self._stat(root_fd, item.rel)
                if again is None or (again.st_size, again.st_mtime_ns, again.st_ino) != (
                    item.size, item.mtime_ns, item.ino,
                ):  # fmt: skip
                    waiting.append(Verdict(display_path(item.rel), NOT_READY))
                    continue
                ready.append(item)
        finally:
            os.close(root_fd)
        return Scan(tuple(ready), tuple(waiting), tuple(refused))

    def _stat(self, root_fd: int, rel: str) -> os.stat_result | None:
        try:
            fd = self._open_under(root_fd, rel.split("/"), directory=False)
        except OSError:
            return None
        try:
            return os.fstat(fd)
        finally:
            os.close(fd)

    def _walk(
        self,
        directory_fd: int,
        parents: tuple[str, ...],
        device: int,
        found: list[Found],
        refused: list[Verdict],
        waiting: list[Verdict],
        counter: list[int],
    ) -> None:
        with os.scandir(directory_fd) as entries:
            listing = sorted(entries, key=lambda e: e.name)
        groups: dict[str, list[str]] = {}
        for entry in listing:
            groups.setdefault(collision_key(entry.name), []).append(entry.name)
        for entry in listing:
            if not parents and entry.name == QUARANTINE:
                continue  # ours: what is on its way out is not a file to take in
            counter[0] += 1
            rel = "/".join((*parents, entry.name))
            shown = display_path(rel)
            if counter[0] > MAX_ENTRIES or len(parents) >= MAX_DEPTH:
                refused.append(Verdict(shown, ARCHIVE))
                continue
            code = check_name(entry.name)
            if code is None and len(groups[collision_key(entry.name)]) > 1:
                code = NAME_COLLISION
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                waiting.append(Verdict(shown, NOT_READY))
                continue
            if code is not None:
                refused.append(Verdict(shown, code))
                continue
            if stat.S_ISLNK(info.st_mode):
                refused.append(Verdict(shown, LINK))
            elif stat.S_ISDIR(info.st_mode):
                if info.st_dev != device or (info.st_dev, info.st_ino) in self._apart:
                    refused.append(Verdict(shown, LINK))  # another disk, or a root of ours
                    continue
                try:
                    child = os.open(
                        entry.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=directory_fd,
                    )
                except OSError:
                    refused.append(Verdict(shown, LINK))
                    continue
                try:
                    self._walk(
                        child, (*parents, entry.name), device, found, refused, waiting, counter
                    )
                finally:
                    os.close(child)
            elif not stat.S_ISREG(info.st_mode):
                refused.append(Verdict(shown, UNSUPPORTED))  # a pipe, a socket, a device
            elif info.st_nlink > 1 or info.st_dev != device:
                refused.append(Verdict(shown, LINK))  # a hard link: it may be a file elsewhere
            elif entry.name.startswith(".") or entry.name.lower().endswith(_IN_PROGRESS_SUFFIXES):
                waiting.append(Verdict(shown, NOT_READY))
            elif self._now() - info.st_mtime_ns / 1e9 < self._settle:
                waiting.append(Verdict(shown, NOT_READY))  # written a moment ago, or in the future
            else:
                found.append(Found(rel, info.st_size, info.st_mtime_ns, info.st_ino))

    @staticmethod
    def _open_under(root_fd: int, parts: list[str], *, directory: bool) -> int:
        """Open a path one step at a time from `root_fd`, refusing a link at every step."""
        current = os.dup(root_fd)
        try:
            for part in parts[:-1]:
                step = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=current,
                )  # fmt: skip
                os.close(current)
                current = step
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            return os.open(parts[-1], flags | (os.O_DIRECTORY if directory else 0), dir_fd=current)
        finally:
            os.close(current)

    # -- importing ----------------------------------------------------------------------------

    def run(self, *, declared_purchase: bool = False) -> tuple[Scan, list[ImportOutcome]]:
        """Scan, then take in each file that is ready. A file already refused and unchanged is not
        read again. Only what lies under `achats/` can be a declared purchase, and only when the
        command says so; everything else is untrusted."""
        try:
            lock = self._lock()
        except BlockingIOError:
            return Scan((), (), ()), []  # another scan is running: two never cross
        except OSError:  # the quarantine is not a plain folder of ours: nothing is removed then
            log.warning("import.quarantine_unusable")
            return Scan((), (), ()), []
        try:
            return self._run(declared_purchase)
        finally:
            os.close(lock)

    def _lock(self) -> int:
        root_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            fd = self._quarantine_dir(root_fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                raise
            return fd
        finally:
            os.close(root_fd)

    def _run(self, declared_purchase: bool) -> tuple[Scan, list[ImportOutcome]]:
        root_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            self._restore_quarantine(root_fd)
        finally:
            os.close(root_fd)
        scan = self.scan()
        for verdict in scan.refused:
            self._record(verdict.rel, 0, 0, None, "refused", verdict.code)
        outcomes: list[ImportOutcome] = []
        for item in scan.ready:
            if item.rel.split("/")[0] != PURCHASE_DIR:
                outcomes.append(self.import_one(item))
            elif declared_purchase:  # only the command declares: the schedule never does
                outcomes.append(self.import_one(item, declared_purchase=True))
            else:
                outcomes.append(ImportOutcome(display_path(item.rel), "waiting", PURCHASE_PENDING))
        return scan, outcomes

    def import_one(self, item: Found, *, declared_purchase: bool = False) -> ImportOutcome:
        shown = display_path(item.rel)
        known = self._known(item)
        if known is not None:
            return ImportOutcome(shown, "refused", known)
        root_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            return self._import(root_fd, item, shown, declared_purchase)
        finally:
            os.close(root_fd)

    def _import(self, root_fd: int, item: Found, shown: str, declared: bool) -> ImportOutcome:
        try:
            fd = self._open_under(root_fd, item.rel.split("/"), directory=False)
        except FileNotFoundError:
            return ImportOutcome(shown, "waiting", NOT_READY)  # taken away meanwhile: nothing owed
        except OSError:
            return self._refuse(item, shown, LINK)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink > 1:
                return self._refuse(item, shown, LINK)
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                item.size, item.mtime_ns, item.ino,
            ):  # fmt: skip
                return ImportOutcome(shown, "waiting", CHANGED)
            return self._stage_and_file(root_fd, fd, before, item, shown, declared)
        finally:
            os.close(fd)

    def _stage_and_file(
        self, root_fd: int, fd: int, before: os.stat_result, item: Found, shown: str, declared: bool
    ) -> ImportOutcome:
        if before.st_size > self._max:
            return self._refuse(item, shown, TOO_LARGE)
        try:
            with tempfile.NamedTemporaryFile(
                dir=self._staging, prefix="import-", suffix=".bin"
            ) as sink:
                sha, copied = self._copy(fd, sink)
                after = os.fstat(fd)
                if copied != before.st_size or (after.st_size, after.st_mtime_ns) != (
                    before.st_size, before.st_mtime_ns,
                ):  # fmt: skip
                    return ImportOutcome(shown, "waiting", CHANGED)  # written while it was read
                return self._file(root_fd, Path(sink.name), sha, item, shown, declared)
        except OSError as error:
            return self._failed(item, shown, error)

    def _copy(self, fd: int, sink: Any) -> tuple[str, int]:
        """The file into the staging folder, counted and hashed on the way; a disk that fills
        raises `OSError` and the staged file goes with the `with` block."""
        digest, total = hashlib.sha256(), 0
        with os.fdopen(os.dup(fd), "rb", closefd=True) as source:
            source.seek(0)
            while chunk := source.read(1 << 20):
                total += len(chunk)
                if total > self._max:
                    raise ReceivedTooLargeError(f"above {self._max}")
                digest.update(chunk)
                sink.write(chunk)
        sink.flush()
        return "sha256:" + digest.hexdigest(), total

    def _file(
        self, root_fd: int, staged: Path, sha: str, item: Found, shown: str, declared: bool
    ) -> ImportOutcome:
        name = item.rel.rsplit("/", 1)[-1]
        kind = import_kind(staged, name)
        if kind == "audio":
            return self._refuse(item, shown, AUDIO, sha)
        if kind == "unknown":
            return self._refuse(item, shown, UNSUPPORTED, sha)
        try:
            if kind == "pdf":
                check_pdf_shape(staged)
            protection = detect_protection(staged, kind, self._limits)
            hint = None if protection else watermark_hint(staged, kind, self._limits)
        except NotAPdfError:
            return self._refuse(item, shown, UNSUPPORTED, sha)
        except UnsafeArchiveError as error:
            code = PROTECTED_FILE if error.reason == "encrypted" else ARCHIVE
            return self._refuse(item, shown, code, sha)
        if protection is not None:
            log.info("import.refused", code=protection.code, reason=protection.reason, kind=kind)
            return self._refuse(item, shown, protection.code, sha, kind=kind)
        # I50, for every source: what Shelfhold exported and comes back is a replica, no Unit
        echo = absorb_import_echo(
            self._engine, staged=staged, kind=f".{kind}", file_hash=sha, size=staged.stat().st_size
        )
        if echo is not None:
            return self._refuse(item, shown, OWN_EXPORT, sha, kind=kind)
        mark = "user" if declared else ("heuristic" if hint else None)
        present = self._stored_unit(sha)
        if present is not None:  # the same bytes were filed before (a replay, or a second copy)
            unit_id, content_id = present
            ingested_id = content_id
        else:
            filed = self._ingest(staged, sha, kind, name, declared, item, shown)
            if isinstance(filed, ImportOutcome):
                return filed
            unit_id, ingested_id = filed
        if kind == "cbz":  # EF-BIB-051 (6): a source with SVG pages goes only with its derivative
            waiting = self._derivative_gate(unit_id, staged, shown)
            if waiting is not None:
                return waiting
        if mark is not None:
            self._mark(ingested_id, mark)
        return self._settle_and_remove(
            root_fd, item, shown, sha, kind, unit_id, mark, "publisher" if declared else "untrusted"
        )

    def _derivative_gate(self, unit_id: str, staged: Path, shown: str) -> ImportOutcome | None:
        """Never let the source go while its Unit has SVG pages and no `derived_hash`: finish the
        derivative (a replay after a failed declaration), and if it cannot be finished yet the
        file stays and is tried again. A store with no derivatives has nothing to finish."""
        missing = getattr(self._store, "derivative_missing", None)
        if missing is None or not missing(unit_id, staged):
            return None
        try:
            self._store.finish_derivative(unit_id=unit_id, staged=staged)
            still = missing(unit_id, staged)
        except (OSError, UntrustedEntryError, UnsafeArchiveError, WorkBusyError):
            still = True
        return ImportOutcome(shown, "waiting", NOT_READY) if still else None

    def _ingest(
        self, staged: Path, sha: str, kind: str, name: str, declared: bool, item: Found,
        shown: str,
    ) -> tuple[str, str] | ImportOutcome:  # fmt: skip
        """Everything that can refuse the file is asked **before** the Unit is made, so a refused
        file leaves no empty Unit behind; then the Unit, then the bytes."""
        trust = "publisher" if declared else self._default_trust
        if self._precheck is not None:
            code = self._precheck(staged, kind, sha, trust)
            if code is not None:
                return self._refuse(item, shown, code, sha, kind=kind)
        unit_id, variant_id = self._target(ImportItem(kind, _title_of(name), trust, sha, item.rel))
        return self._ingest_into(staged, sha, kind, unit_id, variant_id, item, shown)

    def _ingest_into(
        self, staged: Path, sha: str, kind: str, unit_id: str, variant_id: str, item: Found,
        shown: str,
    ) -> tuple[str, str] | ImportOutcome:  # fmt: skip
        try:
            ingested = self._store.ingest(
                unit_id=unit_id, variant_id=variant_id, staged=staged, expected_sha256=sha,
                dest=self._place(unit_id, variant_id, f".{kind}"), upstream_ref=None,
            )  # fmt: skip
        except VariantAlreadyStoredError:  # a replay after a crash: it is there
            found = self._stored_unit(sha)
            if found is None:
                return self._failed(item, shown, OSError(errno.EEXIST, "stored, not recorded"))
            return found
        except TombstonedContentError:
            return self._refuse(item, shown, TOMBSTONED, sha, kind=kind)
        except (EpubRefusedError, UntrustedEntryError, UnsafeArchiveError, NotAPdfError):
            return self._refuse(item, shown, ARCHIVE, sha, kind=kind)
        except ReceivedTooLargeError:
            return self._refuse(item, shown, TOO_LARGE, sha, kind=kind)
        except (UnitGoneError, WorkBusyError):
            return ImportOutcome(shown, "waiting", NOT_READY)
        except ValueError:  # `received_hash_mismatch`: not the bytes that were counted
            return ImportOutcome(shown, "failed", HASH_MISMATCH)
        except (OSError, FsopsError) as error:
            return self._failed(item, shown, error)
        return unit_id, str(ingested.content_id)

    # -- the end of it ------------------------------------------------------------------------

    def _settle_and_remove(
        self, root_fd: int, item: Found, shown: str, sha: str, kind: str, unit_id: str,
        mark: str | None, trust: str,
    ) -> ImportOutcome:  # fmt: skip
        self._record(shown, item.size, item.mtime_ns, sha, "stored", None)
        if not self._stored_verified(unit_id, sha):
            code = HASH_MISMATCH if self._claims(unit_id, sha) else ORIGINAL_NOT_KEPT
            return ImportOutcome(shown, "stored", code, False, unit_id, kind, mark)
        removed = False if self._keep_source else self._remove_verified(root_fd, item, sha)
        if removed:
            self._record(shown, item.size, item.mtime_ns, sha, "removed", None)
            log.info("import.removed", kind=kind)
        note = UNTRUSTED_PDF if kind == "pdf" and trust != "publisher" else None
        return ImportOutcome(shown, "stored", note, removed, unit_id, kind, mark)

    def _stored_verified(self, unit_id: str, sha: str) -> bool:
        """A copy that holds these very bytes, read back from the disk now."""
        with self._engine.connect() as connection:
            copies: list[LibraryCopy | StoreCopy] = [
                *library_copies(connection, unit_id),
                *store_copies(connection, unit_id, include_untrusted=True),
            ]
        for copy in copies:
            if copy.file_hash != sha:
                continue
            try:
                if self._fsops.sha256_of(copy.local) == sha:
                    return True
            except (OSError, FsopsError):
                continue
        return False

    def _claims(self, unit_id: str, sha: str) -> bool:
        """Some replica row says it holds these bytes (so a failed read-back is a mismatch, and
        no row at all is an original that was never kept)."""
        with self._engine.connect() as connection:
            copies: list[LibraryCopy | StoreCopy] = [
                *library_copies(connection, unit_id),
                *store_copies(connection, unit_id, include_untrusted=True),
            ]
        return any(copy.file_hash == sha for copy in copies)

    def _remove_verified(self, root_fd: int, item: Found, sha: str) -> bool:
        """Take the file out of the folder: only if it is still the file that was read (same
        inode, same size, same hash). Called only once a stored copy was verified by hash.

        A name can be re-pointed at another file at any moment, so the name is never unlinked
        directly: it is moved into the quarantine, the moved entry is compared with the file that
        was read, and only an identical one is deleted. Anything else is put back."""
        parts = item.rel.split("/")
        try:
            parent = self._parent_fd(root_fd, parts)  # one parent: to open, to move, to put back
        except OSError:
            return False
        try:
            fd = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=parent,
            )  # fmt: skip
        except OSError:
            os.close(parent)
            return False  # gone, or swapped for a link: nothing to remove
        try:
            now = os.fstat(fd)
            if (now.st_ino, now.st_size) != (item.ino, item.size) or now.st_nlink > 1:
                return False
            if self._rehash(fd) != sha:
                return False  # replaced since it was read: it is somebody's other file
            return self._quarantine_and_drop(root_fd, parent, item.rel, now)
        except OSError:
            return False
        finally:
            os.close(fd)
            os.close(parent)

    @staticmethod
    def _rehash(fd: int) -> str:
        digest = hashlib.sha256()
        with os.fdopen(os.dup(fd), "rb", closefd=True) as stream:
            while chunk := stream.read(1 << 20):
                digest.update(chunk)
        return "sha256:" + digest.hexdigest()

    @staticmethod
    def _quarantine_dir(root_fd: int) -> int:
        with contextlib.suppress(FileExistsError):
            os.mkdir(QUARANTINE, 0o700, dir_fd=root_fd)
        return os.open(
            QUARANTINE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=root_fd
        )

    def _quarantine_and_drop(
        self, root_fd: int, parent: int, rel: str, verified: os.stat_result
    ) -> bool:
        name = rel.rsplit("/", 1)[-1]
        quarantine = self._quarantine_dir(root_fd)
        try:
            held = new_ulid()
            self._write_companion(quarantine, held, rel)
            os.rename(name, held, src_dir_fd=parent, dst_dir_fd=quarantine)
            moved = os.stat(held, dir_fd=quarantine, follow_symlinks=False)
            if stat.S_ISREG(moved.st_mode) and (moved.st_dev, moved.st_ino) == (
                verified.st_dev, verified.st_ino,
            ):  # fmt: skip
                os.unlink(held, dir_fd=quarantine)
                os.unlink(held + COMPANION, dir_fd=quarantine)
                return True
            self._put_back(quarantine, held, parent, name, rel)  # another file took the name
            return False
        finally:
            os.close(quarantine)

    @staticmethod
    def _write_companion(quarantine: int, held: str, rel: str) -> None:
        """Where the entry came from, kept beside it: a restore goes back to the same folder."""
        fd = os.open(
            held + COMPANION, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600, dir_fd=quarantine,
        )  # fmt: skip
        with os.fdopen(fd, "wb") as stream:
            stream.write(rel.encode())

    def _put_back(self, quarantine: int, held: str, parent: int, name: str, rel: str) -> bool:
        """Return what is held to `name` by a rename that refuses to replace anything
        (`renameat2` with RENAME_NOREPLACE). A taken name leaves it held. Where the kernel or the
        file system cannot do that, it stays in the quarantine and the status says so: nothing is
        ever put back by overwriting."""
        try:
            _rename_noreplace(quarantine, held, parent, name)
        except FileExistsError:
            log.warning("import.removal_conflict")  # the name is taken: it stays held
            return False
        except OSError:
            log.warning("import.left_in_quarantine")
            self._record(rel, 0, 0, None, "refused", LEFT_IN_QUARANTINE)  # a message that lasts
            return False
        with contextlib.suppress(OSError):
            os.unlink(held + COMPANION, dir_fd=quarantine)
        return True

    def _restore_quarantine(self, root_fd: int) -> None:
        """What a crash left in the quarantine was not verified (a verified file is deleted at
        once): it goes back to the folder it came from, where the next scan reads it again."""
        try:
            quarantine = self._quarantine_dir(root_fd)
        except OSError:
            return
        try:
            with os.scandir(quarantine) as entries:
                held = sorted(e.name for e in entries if not e.name.endswith(COMPANION))
            for entry in held:
                parts = self._origin(quarantine, entry)
                try:
                    parent = self._ensure_parent(root_fd, parts)
                except OSError:
                    continue
                try:
                    rel = "/".join(parts)
                    if not self._put_back(quarantine, entry, parent, parts[-1], rel):
                        # the name is taken: keep both, the held one under a name of its own
                        self._put_back(quarantine, entry, parent, f"{entry}~{parts[-1]}"[:200], rel)
                finally:
                    os.close(parent)
                log.warning("import.quarantine_restored")
        finally:
            os.close(quarantine)

    @staticmethod
    def _origin(quarantine: int, entry: str) -> list[str]:
        try:
            fd = os.open(
                entry + COMPANION, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=quarantine
            )
            with os.fdopen(fd, "rb") as stream:
                parts = stream.read(4096).decode().split("/")
        except (OSError, UnicodeDecodeError):
            return [entry]
        if not parts or any(p in ("", ".", "..") or "\0" in p for p in parts):
            return [entry]
        return parts

    @staticmethod
    def _ensure_parent(root_fd: int, parts: list[str]) -> int:
        current = os.dup(root_fd)
        try:
            for part in parts[:-1]:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o755, dir_fd=current)
                step = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=current,
                )  # fmt: skip
                os.close(current)
                current = step
        except OSError:
            os.close(current)
            raise
        return current

    @staticmethod
    def _parent_fd(root_fd: int, parts: list[str]) -> int:
        current = os.dup(root_fd)
        for part in parts[:-1]:
            step = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current
            )
            os.close(current)
            current = step
        return current

    # -- rows ---------------------------------------------------------------------------------

    def _stored_unit(self, sha: str) -> tuple[str, str] | None:
        content = storage.content
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(content.c.unit_id, content.c.id)
                .where(content.c.original_sha256 == sha, content.c.unit_id.is_not(None))
                .order_by(content.c.id)
                .limit(1)
            ).first()
        return None if row is None else (str(row.unit_id), str(row.id))

    def _mark(self, content_id: str, origin: str) -> None:
        """Flag the content. A user's flag stays; the heuristic only fills a blank."""
        content = storage.content
        allowed = ("none", "heuristic") if origin == "user" else ("none",)
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(content)
                .where(content.c.id == content_id, content.c.watermark_origin.in_(allowed))
                .values(watermarked=True, watermark_origin=origin)
            )

    def _known(self, item: Found) -> str | None:
        """The code of a refusal already recorded for this very file (same size and time)."""
        table = storage.import_entry
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(table.c.state, table.c.reason, table.c.size, table.c.mtime).where(
                    table.c.rel_path == self._prefix + _escaped_key(display_path(item.rel))
                )
            ).first()
        if row is None or row.state != "refused" or row.size != item.size or not row.reason:
            return None
        if row.mtime != datetime.fromtimestamp(item.mtime_ns / 1e9, UTC):
            return None  # another file under the same name
        return str(row.reason)

    def _record(
        self, rel: str, size: int, mtime_ns: int, sha: str | None, state: str, reason: str | None
    ) -> None:
        table = storage.import_entry
        key, now = self._prefix + _escaped_key(rel), datetime.now(UTC)
        values = {
            "size": max(size, 1), "mtime": datetime.fromtimestamp(mtime_ns / 1e9, UTC),
            "sha256": sha, "state": state, "reason": reason, "decided_at": now,
        }  # fmt: skip
        try:
            with self._engine.begin() as connection:
                exists = connection.execute(
                    sa.select(table.c.rel_path).where(table.c.rel_path == key)
                ).first()
                if exists:
                    connection.execute(
                        sa.update(table).where(table.c.rel_path == key).values(**values)
                    )
                else:
                    connection.execute(
                        sa.insert(table).values(rel_path=key, first_seen_at=now, **values)
                    )
        except sa.exc.IntegrityError:  # a name the table refuses: the answer is still given
            log.warning("import.entry_not_recorded")

    # -- answers ------------------------------------------------------------------------------

    def _refuse(
        self, item: Found, shown: str, code: str, sha: str | None = None, *, kind: str | None = None
    ) -> ImportOutcome:
        """Refused: said by its code, recorded, and the file stays where it is."""
        self._record(shown, item.size, item.mtime_ns, sha, "refused", code)
        log.info("import.refused", code=code)
        return ImportOutcome(shown, "refused", code, False, None, kind)

    def _failed(self, item: Found, shown: str, error: BaseException) -> ImportOutcome:
        full = isinstance(error, OSError) and error.errno in (errno.ENOSPC, errno.EDQUOT)
        code = DISK_FULL if full else NOT_STORED
        log.warning("import.failed", code=code, error=type(error).__name__)
        return ImportOutcome(shown, "failed", code)


__all__ = [
    "PROTECTED",
    "REFUSED_CODES",
    "Found",
    "ImportFolder",
    "ImportFolderOverlapError",
    "ImportItem",
    "ImportOutcome",
    "Scan",
    "Verdict",
    "check_name",
    "collision_key",
    "display_path",
    "import_kind",
]
