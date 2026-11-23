"""Quotas and retention of the derived caches (EF-BIB-018, D17).

Every cache the host creates is bounded, so that a library that grows never fills the disk with
what it can make again: a quota in bytes for what lives in files (the cover thumbnails, the
renditions a client without a format is served — a KEPUB is a kind of rendition — and the bodies
of the HTTP cache) and for the payloads of the metadata providers (with a second, inner bound in
rows), and a retention in days for each. The sweep runs daily and evicts by **last access**
(LRU): the `last_used_at` of a thumbnail (touched at most once a day by its service), the access
time of a rendition's file (its read touches it the same way), the `stored_at` of an HTTP entry,
the `fetched_at` of a metadata row. What the retention no longer keeps goes first, then the least
recently used, until the quota holds — and while the low-disk guard of M3 says the volume of
`/data` is under its free-space mark, the guard passes first: the sweep erases as it decides and
asks the guard again after each file, so it stops as soon as the volume is back above it.

The sweep is a system emitter of the closed list (`cache_retention_purge`, passe 13, N2): no
credential triggers it, and in exchange it erases **only paths the host itself created** — the
cache folders under `/data`, at the name patterns their writers use, checked here before anything
is opened, then erased through the `/data` writer (`infrastructure.volumes._unlink_cache_file`:
a descriptor walk, a link
on the way refused, every step on the device of `/data`, and the last name a regular file or a
link — a folder is never removed, not even as a tree). Never `/store`, `/library`, `.shelfhold/`,
nor an original: those are not under the cache folders, and a path that is not one of them is
refused (`ValueError`) before a descriptor is opened. fsops, which has no root under `/data`,
refuses every path for this emitter: the perimeter lives here, and its test proves it.

A rendition an in-flight request is serving is pinned from its lookup to its read
(`RenditionService.pinned`): the sweep never evicts a file a request is about to read, and it
asks the pin again under its lock at every unlink. Two rows may name one file (the same bytes
converted from two sources): the file is counted once and is kept while one of its rows stays.
The in-memory HTTP cache of the plugins' Fetcher is bounded where it lives (`HttpCache`); nothing
of it is on disk, so no sweep of it exists.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.config import Settings
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import platform, storage
from shelfhold.infrastructure.volumes import _unlink_cache_file, open_data_folder
from shelfhold.library.application.renditions import _NAME as RENDITION_NAME
from shelfhold.library.application.renditions import _SHARD as RENDITION_SHARD
from shelfhold.library.application.renditions import CACHE_DIR as RENDITIONS_DIR
from shelfhold.library.application.renditions import RenditionService
from shelfhold.library.application.thumbnails import _NAME as THUMBNAIL_NAME
from shelfhold.library.application.thumbnails import _SHARD as THUMBNAIL_SHARD

__all__ = [
    "CACHE_PURGE_EMITTER",
    "CachePolicy",
    "CacheSweep",
    "cache_state",
    "erase_cache_path",
    "purge_caches",
]

log = structlog.get_logger(__name__)

CACHE_PURGE_EMITTER = "cache_retention_purge"  # the system emitter of the closed list (N2)
THUMBNAILS_DIR = "thumbnails"
HTTP_DIR = "cache/http"
_RENDITION_PREFIX = RENDITIONS_DIR.split("/")  # ["cache", "renditions"]
_HTTP_PREFIX = HTTP_DIR.split("/")  # ["cache", "http"]
_PLAIN = re.compile(r"[0-9a-z][0-9a-z._-]*")  # a body name of the HTTP cache: plain, no climb


@dataclass(frozen=True)
class CachePolicy:
    """The quota and the retention of each cache (the D17 defaults, read from the settings). The
    metadata cache is bounded twice: in bytes of payloads (D17) and in rows."""

    thumbnails_quota_bytes: int
    thumbnails_retention_days: int
    renditions_quota_bytes: int
    renditions_retention_days: int
    http_quota_bytes: int
    http_retention_days: int
    metadata_rows: int
    metadata_quota_bytes: int = 256 * 1024 * 1024

    @classmethod
    def from_settings(cls, settings: Settings) -> CachePolicy:
        return cls(
            settings.cache_thumbnails_quota_mib * 1024 * 1024,
            settings.cache_thumbnails_retention_days,
            settings.cache_renditions_quota_mib * 1024 * 1024,
            settings.cache_renditions_retention_days,
            settings.cache_http_quota_mib * 1024 * 1024,
            settings.cache_http_retention_days,
            settings.cache_metadata_rows,
            settings.cache_metadata_quota_mib * 1024 * 1024,
        )


@dataclass(frozen=True)
class CacheSweep:
    """What one pass of the sweep took, cache by cache (files count the grey variants too)."""

    thumbnails_files: int = 0
    thumbnails_rows: int = 0
    renditions_files: int = 0
    renditions_rows: int = 0
    http_files: int = 0
    http_rows: int = 0
    metadata_rows: int = 0


# -- the paths the host created, and nothing else -----------------------------------------------


def _checked(rel: str) -> str:
    """The path of a cache file the host creates, or `ValueError`: the closed allowlist of the
    emitter. The name patterns are the writers' own (a file the host did not name is not the
    sweep's business), and nothing outside the cache folders is ever admitted."""
    parts = rel.split("/")
    if not rel or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"not a plain relative path: {rel!r}")
    if (
        len(parts) == 3
        and parts[0] == THUMBNAILS_DIR
        and THUMBNAIL_SHARD.fullmatch(parts[1])
        and THUMBNAIL_NAME.fullmatch(parts[2])
    ):
        return rel
    if (
        len(parts) == 4
        and parts[0:2] == _RENDITION_PREFIX
        and RENDITION_SHARD.fullmatch(parts[2])
        and RENDITION_NAME.fullmatch(parts[3])
    ):
        return rel
    if (
        len(parts) >= 3
        and parts[0:2] == _HTTP_PREFIX
        and all(_PLAIN.fullmatch(part) for part in parts[2:])
    ):
        return rel
    raise ValueError(f"{rel!r} is not a path the host created in a cache")


def _erase_file(data_dir: Path, rel: str, *, cache: str) -> int | None:
    """Erase one cache file, the emitter's allowlist checked first: how many went (0 when there
    was nothing there — the row that names it goes all the same), or None when the row must
    stay: a path that is not one the host created is refused (`ValueError`), an erasure that
    failed (`OSError`) leaves the file. Both are logged and the sweep goes on."""
    try:
        return int(erase_cache_path(data_dir, rel))
    except ValueError as error:
        log.warning("cache.path_refused", cache=cache, path=rel, error=type(error).__name__)
    except OSError as error:
        log.warning("cache.erase_failed", cache=cache, path=rel, error=type(error).__name__)
    return None


def erase_cache_path(data_dir: Path, rel: str) -> bool:
    """Erase one file of a cache, the emitter's allowlist checked first: the only eraser of the
    sweep, and the one a test asks for a refusal. A regular file or a link only, on the device
    of `/data` (`_unlink_cache_file`, the `/data` writer's own, this function its only caller):
    a folder is refused, never removed as a tree; a path that is not there any more answers
    False. Whether something went."""
    return _unlink_cache_file(data_dir, _checked(rel))


# -- the sweep -----------------------------------------------------------------------------------


def _erase_thumbnail(data_dir: Path, row: Any) -> int | None:
    """Erase the files one thumbnail row names (its colour variant, and the grey one): how many
    went, or None when the row must stay (a refused path, a file that would not go — both
    logged by `_erase_file`)."""
    digest = str(row.content_hash).removeprefix("sha256:")
    gone = 0
    for name in (f"{digest}-{row.size}.webp", f"{digest}-{row.size}-grey.webp"):
        found = _erase_file(data_dir, f"{THUMBNAILS_DIR}/{digest[:2]}/{name}", cache="thumbnails")
        if found is None:
            return None
        gone += found
    return gone


def _thumbnails(
    engine: Engine,
    data_dir: Path,
    now: datetime,
    policy: CachePolicy,
    pressure: Callable[[], bool] | None,
) -> tuple[int, int]:
    """The retention first (a thumbnail nobody looked at for `retention_days`), then the quota:
    the least recently used goes until the quota holds — and while the low-disk guard says the
    volume of `/data` is still under its mark, the guard passes first (D17), erasing as it
    decides and asking the guard again after each file, so it stops as soon as the volume is
    back above. The grey variant of a size has no row: it goes with its colour thumbnail. A
    path that is not a file the host created is skipped with a warning, row and all: the sweep
    goes on."""
    table = storage.thumbnail
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(
                table.c.content_hash,
                table.c.size,
                table.c.bytes,
                table.c.last_used_at,
                table.c.created_at,
            )
        ).all()
    cutoff = now - timedelta(days=policy.thumbnails_retention_days)
    kept: list[Any] = []
    doomed: list[Any] = []
    total = 0
    for row in rows:
        used = row.last_used_at or row.created_at
        if used < cutoff:
            doomed.append(row)
        else:
            kept.append(row)
            total += int(row.bytes)
    kept.sort(key=lambda row: row.last_used_at or row.created_at)
    files = 0
    taken: list[Any] = []
    for row in doomed:  # the retention goes first, whatever the guard says
        gone = _erase_thumbnail(data_dir, row)
        if gone is not None:
            files += gone
            taken.append(row)
    while kept and (total > policy.thumbnails_quota_bytes or (pressure is not None and pressure())):
        row = kept.pop(0)
        gone = _erase_thumbnail(data_dir, row)  # erased here: the guard sees the space back
        if gone is None:
            continue  # the file stayed: the row stays too, and the guard is asked again
        files += gone
        total -= int(row.bytes)
        taken.append(row)
    if taken:
        with write_transaction(engine) as connection:
            connection.execute(
                sa.delete(table).where(
                    sa.tuple_(table.c.content_hash, table.c.size).in_(
                        [(row.content_hash, row.size) for row in taken]
                    )
                )
            )
    return files, len(taken)


def _rendition_key(row: Any) -> str:
    digest = str(row.content_hash).removeprefix("sha256:")
    return f"{digest[:2]}/{digest}.{row.kind}"


def _renditions(
    engine: Engine,
    data_dir: Path,
    now: datetime,
    policy: CachePolicy,
    pinned: frozenset[str],
    pressure: Callable[[], bool] | None,
) -> tuple[int, int]:
    """The LRU key of a rendition is the access time of its file (its read touches it at most
    once a day); a file nobody read since it was made keeps its creation. Two rows may name one
    file (the same bytes converted from two sources): the file is counted once and is kept while
    one of its rows stays. A pinned rendition — one an in-flight request is serving, from its
    lookup to its read — is never evicted, by retention or by quota, but it occupies the disk all
    the same; the pin is asked again under its lock at every unlink, for a request may have taken
    the file since the pass began. The retention goes first, then the quota and the low-disk
    guard erase as they decide — the guard is asked again after each file, so it stops as soon as
    the volume of `/data` is back above its mark. A row whose file is gone is dropped: the file
    is the cache."""
    table = storage.rendition
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(
                table.c.source_content_hash,
                table.c.kind,
                # the stored text, not the value read back through the JSON type: the row is
                # deleted by the very bytes that key it, whatever form the writer left them in
                sa.cast(table.c.params, sa.Text()).label("params_text"),
                table.c.content_hash,
                table.c.size,
                table.c.created_at,
            )
        ).all()
    files: dict[str, dict[str, Any]] = {}  # shard-relative name -> the file and its rows
    for row in rows:
        key = _rendition_key(row)
        entry = files.setdefault(key, {"size": int(row.size), "mtime": None, "rows": []})
        entry["rows"].append(row)
    shards: dict[str, list[str]] = {}
    for key in files:
        shard, _, name = key.partition("/")
        shards.setdefault(shard, []).append(name)
    for shard, names in shards.items():
        try:
            with open_data_folder(data_dir, f"{RENDITIONS_DIR}/{shard}") as folder:
                for name in names:
                    stat = folder.stat_of(name)
                    if stat is not None:
                        files[f"{shard}/{name}"]["mtime"] = stat.st_mtime
        except (OSError, ValueError):
            continue  # a linked or missing shard: its rows are dropped below all the same
    cutoff = now - timedelta(days=policy.renditions_retention_days)
    ledger: list[Any] = []  # rows of a file that is not there: a ledger of nothing
    doomed: list[tuple[str, dict[str, Any]]] = []  # past their retention: (key, file)
    kept: list[tuple[float, str, dict[str, Any]]] = []  # under the quota's hand
    total = 0
    for key, entry in files.items():
        mtime = entry["mtime"]
        if mtime is None:
            ledger.extend(entry["rows"])
            continue
        if entry["rows"][0].content_hash in pinned:
            total += entry["size"]  # it stays and occupies the disk all the same
            continue  # a request is serving it: not the sweep's to take
        if datetime.fromtimestamp(mtime, tz=UTC) < cutoff:
            doomed.append((key, entry))  # leaving: not the quota's to count
            continue
        total += entry["size"]  # once per file, however many rows name it
        kept.append((mtime, key, entry))
    kept.sort(key=lambda item: item[0])
    files_gone = 0
    taken: list[Any] = []
    for key, entry in doomed:  # the retention goes first, whatever the guard says
        with RenditionService.erasable(entry["rows"][0].content_hash) as free:
            if not free:
                continue  # pinned since the picture: the next pass decides again
            gone = _erase_file(data_dir, f"{RENDITIONS_DIR}/{key}", cache="renditions")
            if gone is None:
                continue  # refused or failed: the file stays, so do its rows
            files_gone += gone
            taken.extend(entry["rows"])
    while kept and (total > policy.renditions_quota_bytes or (pressure is not None and pressure())):
        _, key, entry = kept.pop(0)
        with RenditionService.erasable(entry["rows"][0].content_hash) as free:
            if not free:
                continue  # pinned since the decision: the file and its rows wait for the next pass
            gone = _erase_file(data_dir, f"{RENDITIONS_DIR}/{key}", cache="renditions")
            if gone is None:
                continue  # refused or failed: the file stayed, so did the disk it occupies
            files_gone += gone  # erased here: the guard sees the space it asked for
            total -= entry["size"]
            taken.extend(entry["rows"])
    taken.extend(ledger)
    if taken:
        with write_transaction(engine) as connection:
            for row in taken:
                connection.execute(
                    sa.delete(table).where(
                        table.c.source_content_hash == row.source_content_hash,
                        table.c.kind == row.kind,
                        table.c.params == sa.type_coerce(row.params_text, sa.Text()),
                    )
                )
    return files_gone, len(taken)


def _take_http_body(data_dir: Path, row: Any) -> int:
    """Erase the body one HTTP row names, wherever it sits: how many files went. A body that is
    not a file the host created — outside the cache folder, or a folder at its name — is not the
    sweep's to erase: the body stays and a warning is written; a body that would not go is
    logged with its class. The row goes all the same, for nothing will ever serve it again
    (nothing writes this table today; the sweep is ready for the writer that does)."""
    if row.body_ref is None:
        return 0
    try:
        return int(erase_cache_path(data_dir, str(row.body_ref)))
    except ValueError:
        log.warning("cache.http_body_outside", scope=row.scope)
    except OSError as error:
        log.warning(
            "cache.erase_failed", cache="http", path=str(row.body_ref),
            error=type(error).__name__,
        )  # fmt: skip
    return 0


def _http(
    engine: Engine,
    data_dir: Path,
    now: datetime,
    policy: CachePolicy,
    pressure: Callable[[], bool] | None,
) -> tuple[int, int]:
    """The HTTP cache expires by its own validators (`stored_at` + `max_age`), then the quota
    evicts the oldest stored — and while the low-disk guard says the volume is under its mark,
    the guard passes first (D17), erasing as it decides and asking the guard again after each
    body, so it stops as soon as the volume is back above. A body is sized wherever it sits
    under the cache folder, a sub-folder included (the folder of its own is opened by
    descriptor)."""
    table = platform.http_cache
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(
                table.c.scope,
                table.c.url_hash,
                table.c.stored_at,
                table.c.max_age,
                table.c.body_ref,
            )
        ).all()
    sizes: dict[str, int] = {}
    for row in rows:
        if row.body_ref is None:
            continue
        rel = str(row.body_ref)
        try:
            folder, _, name = rel.rpartition("/")
            with open_data_folder(data_dir, folder) as opened:
                stat = opened.stat_of(name)
                if stat is not None:
                    sizes[rel] = stat.st_size
        except (OSError, ValueError):
            continue
    cutoff = now - timedelta(days=policy.http_retention_days)
    kept: list[tuple[datetime, Any]] = []
    doomed: list[Any] = []
    total = 0
    for row in rows:
        expired = row.stored_at + timedelta(seconds=int(row.max_age)) < now
        if expired or row.stored_at < cutoff:
            doomed.append(row)
        else:
            kept.append((row.stored_at, row))
            total += sizes.get(str(row.body_ref), 0)
    kept.sort(key=lambda pair: pair[0])
    files = 0
    for row in doomed:  # the expiry goes first, whatever the guard says
        files += _take_http_body(data_dir, row)
    while kept and (total > policy.http_quota_bytes or (pressure is not None and pressure())):
        _, row = kept.pop(0)
        files += _take_http_body(data_dir, row)  # erased here: the guard sees the space back
        total -= sizes.get(str(row.body_ref), 0)
        doomed.append(row)
    if doomed:
        with write_transaction(engine) as connection:
            gone = int(
                connection.execute(
                    sa.delete(table).where(
                        sa.tuple_(table.c.scope, table.c.url_hash).in_(
                            [(row.scope, row.url_hash) for row in doomed]
                        )
                    )
                ).rowcount
            )
    else:
        gone = 0
    return files, gone


def _metadata(engine: Engine, now: datetime, policy: CachePolicy) -> int:
    """The answers of the metadata providers: the expired rows go, then the quota in rows keeps
    the most recently fetched, then the D17 quota in bytes of payloads does the same (the
    database file does not shrink when a row goes, so the low-disk guard has no say here: the
    file caches are what gives the disk back). Rows only, never a file."""
    table = platform.metadata_cache
    payload_bytes = sa.func.length(sa.cast(table.c.payload, sa.BLOB))
    with write_transaction(engine) as connection:
        gone = int(connection.execute(sa.delete(table).where(table.c.expires_at <= now)).rowcount)
        rows = connection.execute(
            sa.select(
                table.c.provider,
                table.c.key_hash,
                table.c.fetched_at,
                payload_bytes.label("payload_bytes"),
            ).order_by(table.c.fetched_at.desc(), table.c.provider, table.c.key_hash)
        ).all()
        rows = list(rows)
        if len(rows) > policy.metadata_rows:  # the inner bound, in rows
            for row in rows[policy.metadata_rows :]:
                gone += int(
                    connection.execute(
                        sa.delete(table).where(
                            table.c.provider == row.provider, table.c.key_hash == row.key_hash
                        )
                    ).rowcount
                )
            rows = rows[: policy.metadata_rows]
        total = sum(int(row.payload_bytes) for row in rows)
        while total > policy.metadata_quota_bytes and rows:  # the D17 bound, in bytes
            row = rows.pop()
            gone += int(
                connection.execute(
                    sa.delete(table).where(
                        table.c.provider == row.provider, table.c.key_hash == row.key_hash
                    )
                ).rowcount
            )
            total -= int(row.payload_bytes)
    return gone


def purge_caches(
    engine: sa.Engine,
    data_dir: Path,
    clock: Clock,
    policy: CachePolicy,
    *,
    pinned: frozenset[str] = frozenset(),
    pressure: Callable[[], bool] | None = None,
) -> CacheSweep:
    """One pass of the retention and the quotas over every derived cache (the daily job
    `retention.caches`). `pinned` is what an in-flight request is serving
    (`RenditionService.pinned()`): none of it is evicted, and the pin is asked again under its
    lock at every unlink, for a request may have taken a file since the pass began. `pressure`
    is the low-disk guard of M3 on the volume of `/data`: while it says the volume is under its
    mark, it passes before the quotas — the sweep erases as it decides, asks the guard again
    after each file, and stops as soon as the volume is back above. Only
    `/data` is touched: no library disk is woken, so the pass runs at any hour."""
    now = clock.now()
    thumbs_files, thumbs_rows = _thumbnails(engine, data_dir, now, policy, pressure)
    rend_files, rend_rows = _renditions(engine, data_dir, now, policy, pinned, pressure)
    http_files, http_rows = _http(engine, data_dir, now, policy, pressure)
    metadata_rows = _metadata(engine, now, policy)
    return CacheSweep(
        thumbs_files, thumbs_rows, rend_files, rend_rows, http_files, http_rows, metadata_rows
    )


# -- what `shelfhold status` and the Health page show --------------------------------------------


def cache_state(connection: sa.Connection, policy: CachePolicy) -> list[dict[str, object]]:
    """Every cache with its use and its quota, read from the database alone (no disk is opened:
    a library disk stays asleep). `used_bytes` is None for what the database does not size (the
    bodies of the HTTP cache); the metadata cache shows both its bounds, the D17 one in bytes of
    payloads and the inner one in rows."""
    thumbnails = storage.thumbnail
    renditions = storage.rendition
    state: list[dict[str, object]] = []
    for table, column, kind, quota in (
        (thumbnails, thumbnails.c.bytes, "thumbnails", policy.thumbnails_quota_bytes),
        (renditions, renditions.c.size, "renditions", policy.renditions_quota_bytes),
    ):
        entries, used = connection.execute(
            sa.select(sa.func.count(), sa.func.coalesce(sa.func.sum(column), 0)).select_from(table)
        ).one()
        state.append(
            {
                "kind": kind,
                "entries": int(entries),
                "used_bytes": int(used),
                "quota_bytes": quota,
                "over": int(used) > quota,
            }
        )
    http_entries = int(
        connection.execute(sa.select(sa.func.count()).select_from(platform.http_cache)).scalar_one()
    )
    state.append(
        {
            "kind": "http",
            "entries": http_entries,
            "used_bytes": None,
            "quota_bytes": policy.http_quota_bytes,
            "over": False,  # the bodies are not sized by the database; the sweep enforces
        }
    )
    metadata_entries, metadata_used = connection.execute(
        sa.select(
            sa.func.count(),
            sa.func.coalesce(
                sa.func.sum(sa.func.length(sa.cast(platform.metadata_cache.c.payload, sa.BLOB))), 0
            ),
        ).select_from(platform.metadata_cache)
    ).one()
    state.append(
        {
            "kind": "metadata",
            "entries": int(metadata_entries),
            "used_bytes": int(metadata_used),  # the payloads, the D17 bound
            "quota_bytes": policy.metadata_quota_bytes,
            "quota_entries": policy.metadata_rows,  # the inner bound, in rows
            "over": int(metadata_used) > policy.metadata_quota_bytes
            or int(metadata_entries) > policy.metadata_rows,
        }
    )
    return state
