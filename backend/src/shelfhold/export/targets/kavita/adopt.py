"""Adopting an existing adult Kavita library (EF-EXP-028, EF-EXP-045, EF-EXP-052, EF-EXP-055).

Untouchable: adoption reads and records, it never rewrites, moves or deletes an existing file.
Adopted files are *referenced* where they are (a `content` of retention `adopted_ref`, a replica
in the destination, a manifest line in state `adopted`), never copied. Renames the naming rules
would suggest stay a preview. Nothing is entered, and nothing is changed in Kavita, before the
admin confirms; a library that children can read is refused (M1b), with the list of accounts.

Every name is read through `plugin/parse` in bulk (`parse-bulk`, M9): the same answer decides the
number of an adopted file here and the collision checks of the planner afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import posixpath
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.catalog.public import LANG_NORMALIZER_ID, LanguageError, edition_key_lang
from shelfhold.export.application.export_items import add_coverage
from shelfhold.export.application.snapshot import DestinationContext, load_context
from shelfhold.export.domain.naming import (
    DEFAULT_TEMPLATE_REF,
    FrozenAttrs,
    export_path,
    normalize_series_name,
)
from shelfhold.export.domain.path_map import PathMapError, within
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.dto import HIDDEN_DIR_PATTERNS, LibraryType, ParseResult
from shelfhold.export.targets.kavita.errors import ConfirmationRequired
from shelfhold.infrastructure.archives.safe_open import UnsafeArchiveError, safe_open_archive
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export, storage
from shelfhold.infrastructure.xml.safe_parse import UnsafeXmlError, safe_parse_xml
from shelfhold.library.public import (
    FILE_HASH_BY,
    ContentHash,
    ContentTombstones,
    record_destination_copy,
)
from shelfhold.sdk.age import from_comicinfo

if TYPE_CHECKING:
    from shelfhold.export.application.pipeline import ExportPipeline

__all__ = [
    "AdoptedFile",
    "AdoptedSeries",
    "Adopter",
    "AdoptionRefused",
    "AdoptionReport",
    "AdoptionSession",
    "BulkParse",
    "Duplicate",
    "PreviewStale",
    "Rename",
]

UNKNOWN_LANGUAGE: Final = "x-unknown"  # `edition set-lang` leaves it (language_unknown)
ARCHIVES: Final = (".cbz", ".zip")
Number = tuple[str, str]  # (kind, number): ("chapter", "3") or ("volume", "1")


class AdoptionRefused(Exception):
    """A library the children can read is not adopted before M1b (EF-EXP-028, EF-EXP-055)."""

    code = "target.child_exposure"

    def __init__(self, accounts: Sequence[str]) -> None:
        super().__init__("library exposed to children: " + ", ".join(accounts))
        self.accounts = tuple(accounts)


class PreviewStale(Exception):
    """What the admin confirmed is not what is there now: look again (`adopt --dry-run`)."""

    code = "adopt.preview_stale"


class NoFolderError(Exception):
    """The destination has no folder on this machine to read an existing library from."""


# -- what the survey says -----------------------------------------------------------------------


@dataclass(frozen=True)
class AdoptedFile:
    name: str
    rel_path: str  # below the destination root
    local_path: str
    kind: str  # "chapter" | "volume" | "unknown"
    number: str  # chapter or volume number, "" when unknown
    origin: str  # "filename" | "comicinfo"
    size: int
    mtime_ns: int
    ino: int
    tombstoned: bool = False  # purged for good: never adopted again (EF-BIB-052 (2))


@dataclass(frozen=True)
class Duplicate:
    """Several files that read as the same number in one series: reported, never deleted."""

    series: str
    number: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class Rename:
    """What the naming rules would call a file: only ever a proposal (EF-EXP-028)."""

    from_path: str
    to_path: str


@dataclass(frozen=True)
class AdoptedSeries:
    name: str  # the name Kavita shows
    kavita_series_id: int
    series_dir: str
    frozen_series: str
    frozen_localized: str | None
    frozen_sort: str | None
    frozen_language: str | None
    has_volume: bool
    files: tuple[AdoptedFile, ...]
    volume_only: bool
    age: tuple[int, bool] | None
    edition_id: str | None  # an Edition of the shelf already carries this series


@dataclass(frozen=True)
class AdoptionReport:
    destination_id: str
    library_ref: str
    series: tuple[AdoptedSeries, ...]
    renames: tuple[Rename, ...]
    duplicates: tuple[Duplicate, ...]
    pre_adoption_settings: dict[str, Any]
    fingerprint: str = ""  # of what was read: the confirmation names it (no check-then-act gap)
    adopted_files: int = 0  # filled once applied
    already_adopted: int = 0
    warnings: tuple[str, ...] = field(default=())

    @property
    def tombstoned(self) -> tuple[str, ...]:
        """Files purged for good: the preview says so, the adoption leaves them out."""
        return tuple(f.rel_path for s in self.series for f in s.files if f.tombstoned)

    @property
    def files(self) -> int:
        return sum(len(s.files) for s in self.series)


# -- plugin/parse in bulk ------------------------------------------------------------------------


class BulkParse:
    """The answers of `parse-bulk`, read before planning: a sync `parse` for the planner.

    A name that was not fetched is an error, never a guess: a name that `plugin/parse` has not
    read is not allowed to place a file (EF-EXP-045).
    """

    def __init__(self, answers: Mapping[str, ParseResult]) -> None:
        self._answers = dict(answers)

    def __call__(self, name: str) -> ParseResult:
        try:
            return self._answers[name]
        except KeyError:
            raise KeyError(f"{name!r} was not read by plugin/parse") from None

    @classmethod
    async def fetch(
        cls, client: KavitaClient, names: Sequence[str], library_type: LibraryType
    ) -> BulkParse:
        unique = sorted(set(names))
        answers = await client.parse_bulk(unique, library_type) if unique else {}
        return cls(answers)

    @classmethod
    async def for_plan(
        cls, client: KavitaClient, pipeline: ExportPipeline, destination_id: str
    ) -> BulkParse:
        """The names the plan of a destination asks `plugin/parse` about, fetched in bulk."""
        names, kind = pipeline.wanted_names(destination_id)
        return await cls.fetch(client, names, library_type_named(kind))


def library_type_named(name: str | None) -> LibraryType:
    for candidate in (name or "manga", (name or "manga").upper()):
        try:
            return LibraryType.from_name(candidate)
        except ValueError:
            continue
    for member in LibraryType:
        if member.kavita_name.casefold() == (name or "manga").casefold():
            return member
    return LibraryType.MANGA


# -- numbers and ComicInfo -----------------------------------------------------------------------


MAX_NUMBER: Final = Decimal(10_000_000)  # no chapter or volume is numbered past this
MAX_NUMBER_TEXT: Final = 32


def _plain(value: float) -> str:
    """The number as plain decimal text; `""` for NaN, infinity or an absurd size."""
    try:
        wanted = Decimal(repr(value))
    except InvalidOperation:
        return ""
    if not wanted.is_finite() or not 0 < wanted < MAX_NUMBER:
        return ""
    return format(wanted.normalize(), "f")


def number_of(parsed: ParseResult, info: Mapping[str, str]) -> tuple[str, str, str]:
    """`(kind, number, origin)`: the name read by `plugin/parse` first, ComicInfo second.

    A number that is not finite or beyond `MAX_NUMBER` is ignored, never an exception."""
    if text := _plain(parsed.min_chapter):
        return "chapter", text, "filename"
    if text := _plain(parsed.min_volume):
        return "volume", text, "filename"
    raw = info.get("Number", "").strip()
    if raw and len(raw) <= MAX_NUMBER_TEXT:
        try:
            wanted = Decimal(raw)
        except InvalidOperation:
            return "unknown", "", "filename"
        if wanted.is_finite() and 0 < wanted < MAX_NUMBER:
            return "chapter", format(wanted.normalize(), "f"), "comicinfo"
    return "unknown", "", "filename"


def _text(root: Any, tag: str) -> str:
    found = root.find(tag)
    return (found.text or "").strip() if found is not None else ""


def read_comicinfo(fsops: Fsops, local: str) -> dict[str, str]:
    """The ComicInfo of an adopted archive, read through the safe opener; `{}` when there is none
    or the file cannot be trusted (the name alone is then what we know)."""
    if not local.lower().endswith(ARCHIVES):
        return {}
    try:
        with fsops.open_read(local) as stream, safe_open_archive(stream, name=local) as archive:
            name = next(
                (e.name for e in archive.entries if e.name.lower() == "comicinfo.xml"), None
            )  # any case: Kavita reads `comicinfo.xml` too
            if name is None:
                return {}
            raw = archive.read(name, max_bytes=1 << 20)
        root = safe_parse_xml(raw)
    except (UnsafeArchiveError, UnsafeXmlError, KeyError, OSError):
        return {}
    wanted = ("Series", "LocalizedSeries", "SeriesSort", "LanguageISO", "Number", "AgeRating")
    return {tag: value for tag in wanted if (value := _text(root, tag))}


def _dominant(values: Sequence[str]) -> str | None:
    counted = Counter(v for v in values if v)
    if not counted:
        return None
    best = max(counted.values())
    return sorted(v for v, n in counted.items() if n == best)[0]


# -- the adopter ---------------------------------------------------------------------------------


def ensure_link(engine: Engine, clock: Clock, ctx: DestinationContext, library_ref: str) -> str:
    """The `remote_library` row of the server's library and its link to the destination."""
    library, link = export.remote_library, export.shelf_destination_remote_library
    with engine.begin() as connection:
        library_id = connection.execute(
            sa.select(library.c.id).where(
                library.c.export_target_id == ctx.target_id, library.c.remote_ref == library_ref
            )
        ).scalar()
        if library_id is None:
            library_id = new_ulid()
            connection.execute(
                sa.insert(library).values(
                    id=library_id,
                    export_target_id=ctx.target_id,
                    remote_ref=library_ref,
                    seen_at=clock.now(),
                )
            )
        linked = connection.execute(
            sa.select(link.c.remote_library_id).where(
                link.c.shelf_destination_id == ctx.destination_id,
                link.c.remote_library_id == library_id,
            )
        ).scalar()
        if linked is None:
            connection.execute(
                sa.insert(link).values(
                    shelf_destination_id=ctx.destination_id,
                    remote_library_id=library_id,
                    export_target_id=ctx.target_id,
                )
            )
    return str(library_id)


class Adopter:
    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        *,
        locate: Callable[[str], tuple[str, str]],
        clock: Clock,
        auditor: AccessAuditor,
        tombstones: ContentTombstones,
    ) -> None:
        self._tombstones = tombstones
        self._engine = engine
        self._fsops = fsops
        self._locate = locate
        self._clock = clock
        self._auditor = auditor

    # -- the audit and the link (EF-EXP-055) -----------------------------------------------------

    def _context(self, destination_id: str) -> DestinationContext:
        with self._engine.connect() as connection:
            return load_context(connection, destination_id)

    @staticmethod
    def _remote_root(ctx: DestinationContext) -> str:
        if ctx.root_local is None:
            raise NoFolderError(ctx.destination_id)
        return ctx.remote_folder(ctx.root_local)

    async def _require_adult_only(
        self, client: KavitaClient, ctx: DestinationContext, library_ref: str
    ) -> None:
        refused = await self._auditor.refused_names(
            client, shelf_ceiling=ctx.max_age_rating, remote_library_ref=library_ref,
            remote_root=self._remote_root(ctx),
        )  # fmt: skip
        if refused:
            raise AdoptionRefused(refused)

    async def link_library(
        self, client: KavitaClient, destination_id: str, library_ref: str
    ) -> None:
        """Link a destination to a Kavita library: audited first, refused if children read it,
        audited again once linked (the answer is stored for the daily check)."""
        ctx = self._context(destination_id)
        await self._require_adult_only(client, ctx, library_ref)
        self._ensure_link(ctx, library_ref)
        await self._auditor.audit(client, ctx.target_id)

    def _ensure_link(self, ctx: DestinationContext, library_ref: str) -> str:
        return ensure_link(self._engine, self._clock, ctx, library_ref)

    def _linked_ref(self, ctx: DestinationContext) -> str | None:
        if ctx.remote_library_id is None:
            return None
        with self._engine.connect() as connection:
            ref = connection.execute(
                sa.select(export.remote_library.c.remote_ref).where(
                    export.remote_library.c.id == ctx.remote_library_id
                )
            ).scalar()
        return None if ref is None else str(ref)

    # -- the preview: reads only -----------------------------------------------------------------

    async def preview(
        self, client: KavitaClient, destination_id: str, *, library_ref: str | None = None
    ) -> AdoptionReport:
        """What adopting would do; nothing is written, in the database, on disk or in Kavita."""
        return await self._survey(client, self._context(destination_id), library_ref)

    async def _survey(
        self, client: KavitaClient, ctx: DestinationContext, library_ref: str | None
    ) -> AdoptionReport:
        ref = library_ref or self._linked_ref(ctx)
        if ref is None:
            raise NoFolderError("the destination is not linked to a Kavita library")
        await client.probe()
        await self._require_adult_only(client, ctx, ref)
        remote_root = self._remote_root(ctx)
        libraries = await client.list_libraries()
        library = next((lib for lib in libraries if str(lib.get("id")) == ref), None)
        if library is None:
            raise NoFolderError(f"library {ref} is not in Kavita")
        library_type = dto_type(library)
        settings = {
            "library_type": library_type.kavita_name,
            "exclude_patterns": [str(p) for p in library.get("excludePatterns") or []],
        }
        listed = [
            s
            for s in await client.series_of_library(int(ref))
            if within(s.lowest_folder_path or s.folder_path, remote_root) is not None
        ]
        files = {s.id: await client.series_files(s.id) for s in listed}
        names = sorted({posixpath.basename(f) for paths in files.values() for f in paths})
        parsed = await BulkParse.fetch(client, names, library_type)
        known = self._known_editions(ctx)
        survey = [self._series(ctx, s, files[s.id], parsed, remote_root, known) for s in listed]
        renames, duplicates = self._proposals(survey)
        return AdoptionReport(
            ctx.destination_id,
            ref,
            tuple(survey),
            tuple(renames),
            tuple(duplicates),
            settings,
            fingerprint=_fingerprint(ref, survey),
        )

    def _known_editions(self, ctx: DestinationContext) -> dict[str, str]:
        """Normalized series name -> Edition of the shelf (an Edition already carrying it)."""
        edition, work = catalog.edition, catalog.work
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(edition.c.id, work.c.title)
                .select_from(edition.join(work, work.c.id == edition.c.work_id))
                .where(edition.c.shelf_id == ctx.shelf_id)
                .order_by(edition.c.id)
            )
            found: dict[str, str] = {}
            for row in rows:
                found.setdefault(normalize_series_name(row.title), row.id)
        return found

    def _series(
        self,
        ctx: DestinationContext,
        series: Any,
        paths: Sequence[str],
        parsed: Mapping[str, ParseResult] | BulkParse,
        remote_root: str,
        known: Mapping[str, str],
    ) -> AdoptedSeries:
        adopted: list[AdoptedFile] = []
        infos: list[dict[str, str]] = []
        for remote in sorted(paths):
            name = posixpath.basename(remote)
            try:
                local = ctx.path_map.to_local(remote)
            except PathMapError:
                continue
            seen = self._fsops.observe(local)
            if seen is None or seen[0] != "file":
                continue
            info = read_comicinfo(self._fsops, local)
            infos.append(info)
            answer = parsed(name) if isinstance(parsed, BulkParse) else parsed[name]
            kind, number, origin = number_of(answer, info)
            rel = within(remote, remote_root) or name
            purged = self._tombstones.check_content(
                [ContentHash(FILE_HASH_BY, self._fsops.sha256_of(local))]
            )
            adopted.append(
                AdoptedFile(
                    name, rel, local, kind, number, origin, seen[1].size, seen[1].mtime_ns,
                    seen[1].ino, purged,
                )
            )  # fmt: skip
        parsed_names = [
            (parsed(f.name) if isinstance(parsed, BulkParse) else parsed[f.name]).series_name
            for f in adopted
        ]
        frozen = (
            _dominant([i.get("Series", "") for i in infos])
            or _dominant(parsed_names)
            or series.name
        )
        age = _dominant([i.get("AgeRating", "") for i in infos])
        rating = from_comicinfo(age) if age else None
        edition_id = known.get(normalize_series_name(frozen)) or known.get(
            normalize_series_name(series.name)
        )
        return AdoptedSeries(
            name=series.name, kavita_series_id=series.id,
            series_dir=within(series.lowest_folder_path or series.folder_path, remote_root) or "",
            frozen_series=frozen,
            frozen_localized=_dominant([i.get("LocalizedSeries", "") for i in infos]),
            frozen_sort=_dominant([i.get("SeriesSort", "") for i in infos]),
            frozen_language=_dominant([i.get("LanguageISO", "") for i in infos]),
            has_volume=any(f.kind == "volume" for f in adopted),
            files=tuple(adopted),
            volume_only=bool(adopted) and all(f.kind == "volume" for f in adopted),
            age=(rating.min_age, rating.x) if rating else None,
            edition_id=edition_id,
        )  # fmt: skip

    @staticmethod
    def _proposals(series: Sequence[AdoptedSeries]) -> tuple[list[Rename], list[Duplicate]]:
        renames: list[Rename] = []
        duplicates: list[Duplicate] = []
        for item in series:
            groups: dict[Number, list[AdoptedFile]] = {}
            for file in item.files:
                if file.kind == "unknown":
                    continue
                groups.setdefault((file.kind, file.number), []).append(file)
                try:
                    wanted = export_path(
                        FrozenAttrs(
                            series=item.frozen_series,
                            track="main",
                            number_norm=file.number,
                            kind="volume" if file.kind == "volume" else "unit",
                        )
                    ).rel_path
                except ValueError:
                    continue
                if wanted != file.rel_path:
                    renames.append(Rename(file.rel_path, wanted))
            for (_, number), group in sorted(groups.items()):
                if len(group) > 1:
                    names = tuple(sorted(f.name for f in group))
                    duplicates.append(Duplicate(item.frozen_series, number, names))
        return renames, duplicates

    # -- the adoption: only once confirmed --------------------------------------------------------

    async def adopt(
        self,
        client: KavitaClient,
        destination_id: str,
        *,
        confirm: str | None,
        library_ref: str | None = None,
    ) -> AdoptionReport:
        """`confirm` is the fingerprint the preview gave: adoption goes ahead only on exactly
        what the admin looked at (`PreviewStale` otherwise); none at all is no confirmation."""
        ctx = self._context(destination_id)
        report = await self._survey(client, ctx, library_ref)  # reads, and refuses a child library
        if not confirm:
            raise ConfirmationRequired(
                f"adopting {report.files} files of {len(report.series)} series needs confirmation"
            )
        if confirm != report.fingerprint:
            raise PreviewStale(report.fingerprint)
        self._ensure_link(ctx, report.library_ref)
        ctx = self._context(destination_id)
        now = self._clock.now()
        done = skipped = 0
        with self._engine.begin() as connection:
            self._mark_adopted(connection, ctx, report.pre_adoption_settings)
            for item in report.series:
                added, existing = self._inscribe(connection, ctx, item, now)
                done, skipped = done + added, skipped + existing
        # Kavita is touched last, and only to keep our reserved folder out of its scans
        await client.ensure_exclude_patterns(
            int(report.library_ref), HIDDEN_DIR_PATTERNS, adopted=True, confirmed=True
        )
        await self._auditor.audit(client, ctx.target_id)
        return AdoptionReport(
            report.destination_id, report.library_ref, report.series, report.renames,
            report.duplicates, report.pre_adoption_settings, adopted_files=done,
            already_adopted=skipped,
        )  # fmt: skip

    @staticmethod
    def _mark_adopted(
        connection: Connection, ctx: DestinationContext, settings: dict[str, Any]
    ) -> None:
        d = export.shelf_destination
        row = connection.execute(
            sa.select(d.c.adopted, d.c.pre_adoption_settings).where(d.c.id == ctx.destination_id)
        ).one()
        # the settings of *before* the first adoption are the ones to restore at detach
        values: dict[str, Any] = {"adopted": True}
        if row.pre_adoption_settings is None:
            values["pre_adoption_settings"] = settings
        connection.execute(sa.update(d).where(d.c.id == ctx.destination_id).values(**values))

    # -- recording one series ---------------------------------------------------------------------

    def _inscribe(
        self, connection: Connection, ctx: DestinationContext, item: AdoptedSeries, now: datetime
    ) -> tuple[int, int]:
        record = export.export_record
        taken = set(
            connection.execute(
                sa.select(record.c.rel_path).where(
                    record.c.shelf_destination_id == ctx.destination_id,
                    record.c.state != "recycled",
                    record.c.rel_path.is_not(None),
                )
            ).scalars()
        )
        fresh = [f for f in item.files if f.rel_path not in taken and not f.tombstoned]
        if not fresh:
            return 0, len([f for f in item.files if not f.tombstoned])
        edition_id = item.edition_id or self._create_edition(connection, ctx, item)
        self._freeze_series(connection, ctx, item, edition_id, now)
        self._suggest_age(connection, item, edition_id, now)
        order = _next_order(connection, edition_id)
        for file in fresh:
            unit_id, order = self._unit(connection, edition_id, file, order, now)
            self._record(connection, ctx, item, edition_id, unit_id, file, now)
        return len(fresh), len([f for f in item.files if not f.tombstoned]) - len(fresh)

    @staticmethod
    def _create_edition(
        connection: Connection, ctx: DestinationContext, item: AdoptedSeries
    ) -> str:
        work_id, edition_id = new_ulid(), new_ulid()
        connection.execute(
            sa.insert(catalog.work).values(
                id=work_id,
                kind="manga",
                title=item.frozen_series,
                sort_title=(item.frozen_sort or item.frozen_series).casefold(),
            )
        )
        lang, lang_norm_by = UNKNOWN_LANGUAGE, None
        if item.frozen_language:
            # Kavita's language goes through the same normalizer as any Edition key; one it
            # refuses stays in `lang_raw` and the placeholder stands in the key
            with contextlib.suppress(LanguageError):
                lang = str(edition_key_lang(item.frozen_language))
                lang_norm_by = LANG_NORMALIZER_ID
        connection.execute(
            sa.insert(catalog.edition).values(
                id=edition_id, work_id=work_id, lang=lang, lang_raw=item.frozen_language,
                lang_norm_by=lang_norm_by, translation_type="official", media="images",
                shelf_id=ctx.shelf_id,
            )
        )  # fmt: skip
        return edition_id

    @staticmethod
    def _freeze_series(
        connection: Connection, ctx: DestinationContext, item: AdoptedSeries, edition_id: str,
        now: datetime,
    ) -> None:  # fmt: skip
        """The identity and the dominant values of the series, kept for the files to come."""
        link, series = export.edition_export_series, export.export_series
        existing = connection.execute(
            sa.select(link.c.export_series_id).where(
                link.c.edition_id == edition_id, link.c.remote_library_id == ctx.remote_library_id
            )
        ).scalar()
        values = {
            "kavita_series_id": str(item.kavita_series_id), "frozen_series": item.frozen_series,
            "frozen_localized_series": item.frozen_localized,
            "frozen_series_sort": item.frozen_sort, "frozen_language": item.frozen_language,
            "has_volume": item.has_volume, "series_dir": item.series_dir, "frozen_at": now,
        }  # fmt: skip
        if existing is not None:  # an exported series already: its name stays as it is
            connection.execute(
                sa.update(series).where(series.c.id == existing, series.c.frozen_at.is_(None))
                .values(**values)
            )  # fmt: skip
            return
        series_id = new_ulid()
        connection.execute(
            sa.insert(series).values(
                id=series_id, shelf_destination_id=ctx.destination_id,
                remote_library_id=ctx.remote_library_id,
                proposal_key={"adopted": True, "kavita_series_id": item.kavita_series_id},
                normalized_name=normalize_series_name(item.name), name_raw=item.name, **values,
            )
        )  # fmt: skip
        connection.execute(
            sa.insert(link).values(
                edition_id=edition_id, remote_library_id=ctx.remote_library_id,
                export_series_id=series_id, assigned_at=now, assigned_by="first_export",
            )
        )  # fmt: skip

    @staticmethod
    def _suggest_age(
        connection: Connection, item: AdoptedSeries, edition_id: str, now: datetime
    ) -> None:
        """The reverse rule of EF-EXP-011: a suggestion, never a rating (EF-BIB-036, M1b)."""
        if item.age is None:
            return
        value, x = item.age
        table = catalog.age_suggestion
        exists = connection.execute(
            sa.select(table.c.id).where(
                table.c.entity_type == "edition",
                table.c.entity_id == edition_id,
                table.c.value == value,
                table.c.x == x,
                table.c.provenance == "comicinfo",
            )
        ).scalar()
        if exists is None:
            connection.execute(
                sa.insert(table).values(
                    id=new_ulid(), entity_type="edition", entity_id=edition_id, value=value, x=x,
                    provenance="comicinfo", seen_at=now,
                )
            )  # fmt: skip

    @staticmethod
    def _unit(
        connection: Connection, edition_id: str, file: AdoptedFile, order: int, now: datetime
    ) -> tuple[str, int]:
        """The Unit an adopted file carries: the one already there for that number, else a new
        one. A volume of unknown range is one unnumbered Unit (no chapter is guessed)."""
        unit = catalog.unit
        if file.kind == "chapter":
            found = connection.execute(
                sa.select(unit.c.id).where(
                    unit.c.edition_id == edition_id,
                    unit.c.track == "main",
                    unit.c.number_segment == "",
                    unit.c.number_norm == file.number,
                    unit.c.part == "",
                )
            ).scalar()
            if found is not None:
                return str(found), order
            unit_id = new_ulid()
            connection.execute(
                sa.insert(unit).values(
                    id=unit_id, edition_id=edition_id, number_raw=file.number,
                    number_norm=file.number, number_sort=float(file.number), track="main",
                    state="adopted",
                )
            )  # fmt: skip
            return unit_id, order
        volume_id = None
        if file.kind == "volume":
            volume = catalog.volume
            volume_id = connection.execute(
                sa.select(volume.c.id).where(
                    volume.c.edition_id == edition_id, volume.c.number == file.number
                )
            ).scalar()
            if volume_id is None:
                volume_id = new_ulid()
                connection.execute(
                    sa.insert(volume).values(
                        id=volume_id, edition_id=edition_id, number=file.number,
                        number_sort=float(file.number),
                    )
                )  # fmt: skip
            found = connection.execute(
                sa.select(unit.c.id).where(
                    unit.c.edition_id == edition_id,
                    unit.c.track == "unnumbered",
                    unit.c.volume_id == volume_id,
                )
            ).scalar()
            if found is not None:
                return str(found), order
        unit_id = new_ulid()
        connection.execute(
            sa.insert(unit).values(
                id=unit_id, edition_id=edition_id, volume_id=volume_id,
                number_raw=f"Vol.{file.number}" if file.kind == "volume" else file.name,
                number_norm="", track="unnumbered", source_order=order, state="adopted",
            )
        )  # fmt: skip
        return unit_id, order + 1

    def _record(
        self,
        connection: Connection,
        ctx: DestinationContext,
        item: AdoptedSeries,
        edition_id: str,
        unit_id: str,
        file: AdoptedFile,
        now: datetime,
    ) -> None:
        digest = self._fsops.sha256_of(file.local_path)
        item_id = new_ulid()
        content_id = new_ulid()
        connection.execute(
            sa.insert(storage.content).values(
                id=content_id, unit_id=unit_id, variant_id=None, kind="images",
                media_type="application/vnd.comicbook+zip", format="cbz",
                retention="adopted_ref", path=None, original_sha256=digest,
                store_provenance="received", size=file.size,
            )
        )  # fmt: skip
        known = file.kind != "unknown"
        frozen = FrozenAttrs(
            series=item.frozen_series,
            track="main" if known else "unnumbered",
            number_norm=file.number,
            kind="volume" if file.kind == "volume" else "unit",
            template_ref=DEFAULT_TEMPLATE_REF,
        )
        connection.execute(
            sa.insert(export.export_item).values(
                id=item_id, edition_id=edition_id, kind="adopted",
                frozen_attrs={**frozen.__dict__}, content_id=content_id,
            )
        )  # fmt: skip
        add_coverage(connection, item_id, unit_id)
        connection.execute(
            sa.update(export.unit_coverage)
            .where(
                export.unit_coverage.c.carrier_type == "export_item",
                export.unit_coverage.c.carrier_id == item_id,
            )
            .values(origin=file.origin)
        )
        connection.execute(
            sa.insert(export.export_record).values(
                shelf_destination_id=ctx.destination_id, export_item_id=item_id,
                locator_type="path", rel_path=file.rel_path, template_ref=DEFAULT_TEMPLATE_REF,
                format="cbz", size=file.size, mtime_ns=file.mtime_ns, file_hash=digest,
                exported_at=now, state="adopted",
            )
        )  # fmt: skip
        root_id, root_rel = self._locate(file.local_path)
        record_destination_copy(
            connection, unit_id=unit_id, export_item_id=item_id, content_hash=digest,
            file_hash=digest, physical_root_id=root_id, rel_path=root_rel, size=file.size,
            mtime_ns=file.mtime_ns, ino=file.ino, dev=self._fsops.device_of(file.local_path),
            verified_at=now,
        )  # fmt: skip


def _fingerprint(library_ref: str, series: Sequence[AdoptedSeries]) -> str:
    """A digest of the series, their paths, sizes and `mtime_ns`: any change moves it."""
    document = [
        library_ref,
        [
            [
                s.kavita_series_id,
                s.name,
                s.series_dir,
                [[f.rel_path, f.size, f.mtime_ns, f.ino] for f in s.files],
            ]
            for s in sorted(series, key=lambda x: x.kavita_series_id)
        ],
    ]
    return hashlib.sha256(json.dumps(document, ensure_ascii=True).encode()).hexdigest()[:32]


def dto_type(library: Mapping[str, Any]) -> LibraryType:
    try:
        return LibraryType(int(library.get("type", 0)))
    except (TypeError, ValueError):
        return LibraryType.MANGA


def _next_order(connection: Connection, edition_id: str) -> int:
    unit = catalog.unit
    top = connection.execute(
        sa.select(sa.func.max(unit.c.source_order)).where(
            unit.c.edition_id == edition_id, unit.c.track == "unnumbered"
        )
    ).scalar()
    return int(top or 0) + 1


class AdoptionSession:
    """The adopter and its Kavita client, driven from a synchronous command."""

    def __init__(self, adopter: Adopter, client: KavitaClient) -> None:
        self._adopter = adopter
        self._client = client

    def preview(self, destination_id: str, library_ref: str | None = None) -> AdoptionReport:
        return asyncio.run(
            self._adopter.preview(self._client, destination_id, library_ref=library_ref)
        )

    def adopt(
        self, destination_id: str, *, confirm: str | None, library_ref: str | None = None
    ) -> AdoptionReport:
        return asyncio.run(
            self._adopter.adopt(
                self._client, destination_id, confirm=confirm, library_ref=library_ref
            )
        )
