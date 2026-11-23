"""Quotas and retention of the derived caches (EF-BIB-018, D17).

Every cache the host creates is bounded, so that a library that grows never fills the disk with
what it can make again: a quota in bytes for what lives in files (the cover thumbnails, the
renditions a client without a format is served — a KEPUB is a kind of rendition — and the bodies
of the HTTP cache), a quota in rows for what lives in the database (the answers of the metadata
providers), and a retention in days for each. The sweep runs daily and evicts by **last access**
(LRU): the `last_used_at` of a thumbnail (touched at most once a day by its service), the access
time of a rendition's file (its read touches it the same way), the `stored_at` of an HTTP entry,
the `fetched_at` of a metadata row. What the retention no longer keeps goes first, then the least
recently used, until the quota holds.

The sweep is a system emitter of the closed list (`cache_retention_purge`, passe 13, N2): no
credential triggers it, and in exchange it erases **only paths the host itself created** — the
cache folders under `/data`, at the name patterns their writers use, checked here before anything
is opened, then erased through the `/data` layer (`erase_data_path`: a descriptor walk, a link on
the way refused, a link at the end removed as a link). Never `/store`, `/library`, `.shelfhold/`,
nor an original: those are not under the cache folders, and a path that is not one of them is
refused (`ValueError`) before a descriptor is opened. fsops, which has no root under `/data`,
refuses every path for this emitter: the perimeter lives here, and its test proves it.

A rendition served by an in-flight `/content` ticket is pinned for the time of the read
(`RenditionService.pinned`): the sweep never evicts a file a request is serving. The in-memory
HTTP cache of the plugins' Fetcher is bounded where it lives (`HttpCache`); nothing of it is on
disk, so no sweep of it exists.
"""

from __future__ import annotations

import re
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
from shelfhold.infrastructure.volumes import erase_data_path, open_data_folder
from shelfhold.library.application.renditions import _NAME as RENDITION_NAME
from shelfhold.library.application.renditions import _SHARD as RENDITION_SHARD
from shelfhold.library.application.renditions import CACHE_DIR as RENDITIONS_DIR
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
    """The quota and the retention of each cache (the D17 defaults, read from the settings)."""

    thumbnails_quota_bytes: int
    thumbnails_retention_days: int
    renditions_quota_bytes: int
    renditions_retention_days: int
    http_quota_bytes: int
    http_retention_days: int
    metadata_rows: int

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


def erase_cache_path(data_dir: Path, rel: str) -> bool:
    """Erase one file of a cache, the emitter's allowlist checked first: the only eraser of the
    sweep, and the one a test asks for a refusal. Whether something went."""
    return erase_data_path(data_dir, _checked(rel))


# -- the sweep -----------------------------------------------------------------------------------


def _thumbnails(
    engine: Engine, data_dir: Path, now: datetime, policy: CachePolicy
) -> tuple[int, int]:
    """The retention first (a thumbnail nobody looked at for `retention_days`), then the quota:
    the least recently used goes until the quota holds. The grey variant of a size has no row:
    it goes with its colour thumbnail."""
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
    while total > policy.thumbnails_quota_bytes and kept:
        row = kept.pop(0)
        doomed.append(row)
        total -= int(row.bytes)
    files = 0
    for row in doomed:
        digest = str(row.content_hash).removeprefix("sha256:")
        for name in (f"{digest}-{row.size}.webp", f"{digest}-{row.size}-grey.webp"):
            files += int(erase_cache_path(data_dir, f"{THUMBNAILS_DIR}/{digest[:2]}/{name}"))
    if doomed:
        with write_transaction(engine) as connection:
            connection.execute(
                sa.delete(table).where(
                    sa.tuple_(table.c.content_hash, table.c.size).in_(
                        [(row.content_hash, row.size) for row in doomed]
                    )
                )
            )
    return files, len(doomed)


def _rendition_key(row: Any) -> str:
    digest = str(row.content_hash).removeprefix("sha256:")
    return f"{digest[:2]}/{digest}.{row.kind}"


def _renditions(
    engine: Engine, data_dir: Path, now: datetime, policy: CachePolicy, pinned: frozenset[str]
) -> tuple[int, int]:
    """The LRU key of a rendition is the access time of its file (its read touches it at most
    once a day); a file nobody read since it was made keeps its creation. A pinned rendition —
    one an in-flight `/content` ticket is serving — is never evicted, by retention or by quota.
    A row whose file is gone is dropped: the file is the cache."""
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
    access: dict[str, float] = {}  # shard-relative name -> mtime
    shards: dict[str, list[str]] = {}
    for row in rows:
        shard = str(row.content_hash).removeprefix("sha256:")[:2]
        name = f"{str(row.content_hash).removeprefix('sha256:')}.{row.kind}"
        shards.setdefault(shard, []).append(name)
    for shard, names in shards.items():
        try:
            with open_data_folder(data_dir, f"{RENDITIONS_DIR}/{shard}") as folder:
                for name in names:
                    stat = folder.stat_of(name)
                    if stat is not None:
                        access[f"{shard}/{name}"] = stat.st_mtime
        except (OSError, ValueError):
            continue  # a linked or missing shard: its rows are dropped below all the same
    cutoff = now - timedelta(days=policy.renditions_retention_days)
    kept: list[tuple[float, Any]] = []
    doomed: list[Any] = []
    total = 0
    for row in rows:
        key = _rendition_key(row)
        mtime = access.get(key)
        if mtime is None:
            doomed.append(row)  # the file is gone: the row is a ledger of nothing
            continue
        total += int(row.size)  # a pinned one counts too: it occupies the disk all the same
        if row.content_hash in pinned:
            continue  # a request is serving it: not the sweep's to take
        if datetime.fromtimestamp(mtime, tz=UTC) < cutoff:
            doomed.append(row)
            total -= int(row.size)
        else:
            kept.append((mtime, row))
    kept.sort(key=lambda pair: pair[0])
    while total > policy.renditions_quota_bytes and kept:
        _, row = kept.pop(0)
        doomed.append(row)
        total -= int(row.size)
    files = 0
    for row in doomed:
        files += int(erase_cache_path(data_dir, f"{RENDITIONS_DIR}/{_rendition_key(row)}"))
    if doomed:
        with write_transaction(engine) as connection:
            for row in doomed:
                connection.execute(
                    sa.delete(table).where(
                        table.c.source_content_hash == row.source_content_hash,
                        table.c.kind == row.kind,
                        table.c.params == sa.type_coerce(row.params_text, sa.Text()),
                    )
                )
    return files, len(doomed)


def _http(engine: Engine, data_dir: Path, now: datetime, policy: CachePolicy) -> tuple[int, int]:
    """The HTTP cache expires by its own validators (`stored_at` + `max_age`), then the quota
    evicts the oldest stored. A body that is not under the cache folder is not the sweep's to
    erase: its row stays and a warning is written (nothing writes this table today; the sweep is
    ready for the writer that does)."""
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
        try:
            with open_data_folder(data_dir, HTTP_DIR) as folder:
                stat = folder.stat_of(_body_name(str(row.body_ref)))
                if stat is not None:
                    sizes[str(row.body_ref)] = stat.st_size
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
    while total > policy.http_quota_bytes and kept:
        _, row = kept.pop(0)
        doomed.append(row)
        total -= sizes.get(str(row.body_ref), 0)
    files = 0
    for row in doomed:
        if row.body_ref is None:
            continue
        try:
            files += int(erase_cache_path(data_dir, str(row.body_ref)))
        except ValueError:
            log.warning("cache.http_body_outside", scope=row.scope)
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


def _body_name(body_ref: str) -> str:
    """The name of a body inside the HTTP cache folder (the row keeps a path relative to /data;
    the folder is opened by its own descriptor, so only the tail is asked of it)."""
    parts = [part for part in body_ref.split("/") if part not in ("", ".")]
    if len(parts) < 3 or parts[0:2] != HTTP_DIR.split("/") or ".." in parts:
        raise ValueError(f"not a body of the HTTP cache: {body_ref!r}")
    return "/".join(parts[2:])


def _metadata(engine: Engine, now: datetime, policy: CachePolicy) -> int:
    """The answers of the metadata providers: the expired rows go, then the quota in rows keeps
    the most recently fetched. Rows only, never a file."""
    table = platform.metadata_cache
    with write_transaction(engine) as connection:
        gone = int(connection.execute(sa.delete(table).where(table.c.expires_at <= now)).rowcount)
        count = int(connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())
        if count > policy.metadata_rows:
            keep_from = connection.execute(
                sa.select(table.c.fetched_at)
                .order_by(table.c.fetched_at.desc(), table.c.provider, table.c.key_hash)
                .offset(policy.metadata_rows - 1)
                .limit(1)
            ).scalar()
            if keep_from is not None:
                gone += int(
                    connection.execute(
                        sa.delete(table).where(table.c.fetched_at < keep_from)
                    ).rowcount
                )
    return gone


def purge_caches(
    engine: sa.Engine,
    data_dir: Path,
    clock: Clock,
    policy: CachePolicy,
    *,
    pinned: frozenset[str] = frozenset(),
) -> CacheSweep:
    """One pass of the retention and the quotas over every derived cache (the daily job
    `retention.caches`). `pinned` is what an in-flight `/content` ticket is serving
    (`RenditionService.pinned()`): none of it is evicted. Only `/data` is touched: no library
    disk is woken, so the pass runs at any hour."""
    now = clock.now()
    thumbs_files, thumbs_rows = _thumbnails(engine, data_dir, now, policy)
    rend_files, rend_rows = _renditions(engine, data_dir, now, policy, pinned)
    http_files, http_rows = _http(engine, data_dir, now, policy)
    metadata_rows = _metadata(engine, now, policy)
    return CacheSweep(
        thumbs_files, thumbs_rows, rend_files, rend_rows, http_files, http_rows, metadata_rows
    )


# -- what `shelfhold status` and the Health page show --------------------------------------------


def cache_state(connection: sa.Connection, policy: CachePolicy) -> list[dict[str, object]]:
    """Every cache with its use and its quota, read from the database alone (no disk is opened:
    a library disk stays asleep). `used_bytes` is None for what the database does not size (the
    bodies of the HTTP cache) and the metadata quota is in rows."""
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
    metadata_entries = int(
        connection.execute(
            sa.select(sa.func.count()).select_from(platform.metadata_cache)
        ).scalar_one()
    )
    state.append(
        {
            "kind": "metadata",
            "entries": metadata_entries,
            "used_bytes": None,
            "quota_bytes": None,
            "quota_entries": policy.metadata_rows,
            "over": metadata_entries > policy.metadata_rows,
        }
    )
    return state
