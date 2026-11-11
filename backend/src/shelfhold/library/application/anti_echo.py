"""The anti-echo rule (I50, EF-SRC-006): Shelfhold never re-imports its own exports.

A file that a source hands over, whichever source it is (an OPDS catalogue of Kavita, one of
Shelfhold's own, a replica of either), is compared to what Shelfhold exported before it is filed:

- its `file_hash` against `export_record.file_hash` and the old hashes of `document_alias`;
- its `content_hash` (the payload, whatever a server rewrote around it) against
  `export_record.content_hash`, `document_alias.content_hash` and the payload hashes of the
  `content` an `export_item` was made from (original and derived).

A match makes the file a **replica** of the existing `export_item`, never a new Variant: the
request that asked for it leaves no `unit_variant`, no `content` and no file behind. The check
reads the hashes of rows that exist; an absent hash (a record still planned) matches nothing.
This module knows no source: nothing a manifest says can switch it off.
"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.infrastructure.archives import UnsafeArchiveError, safe_open_archive
from shelfhold.infrastructure.content_trust.epub_scan import epub_payload_hash
from shelfhold.infrastructure.schema import acquisition, catalog, export, storage
from shelfhold.library.domain.content_hash import content_hash
from shelfhold.library.replica import record_replica, upstream_echo_of

__all__ = ["EchoOfOwnExport", "OwnExport", "absorb_echo", "own_export_of", "received_content_hash"]

log = structlog.get_logger(__name__)

MatchedBy = Literal["file_hash", "content_hash", "document_alias", "export_content"]
_EPUB = ".epub"


@dataclass(frozen=True)
class OwnExport:
    export_item_id: str
    edition_id: str
    matched_by: MatchedBy


@dataclass(frozen=True)
class EchoOfOwnExport:
    """What was done with the echo: the export it is a replica of, and the replica row."""

    own: OwnExport
    replica_id: str


def received_content_hash(path: Path, kind: str) -> str | None:
    """The payload hash of a received archive, as the store would compute it, or None when the
    archive cannot be read: such a file is not an echo, and the store refuses it as ever."""
    try:
        with safe_open_archive(path, name=f"received{kind}") as archive:
            return epub_payload_hash(archive) if kind == _EPUB else content_hash(archive)
    except (UnsafeArchiveError, OSError, ValueError, KeyError):
        return None


def _first(connection: Connection, query: sa.Select[tuple[str, str]]) -> tuple[str, str] | None:
    row = connection.execute(query.order_by(export.export_item.c.id).limit(1)).first()
    return None if row is None else (str(row.id), str(row.edition_id))


def own_export_of(
    connection: Connection, *, file_hash: str | None, content_hash: str | None
) -> OwnExport | None:
    """The `export_item` whose file or payload this is, or None. NULL never matches."""
    item, record, alias, content = (
        export.export_item,
        export.export_record,
        export.document_alias,
        storage.content,
    )
    base = sa.select(item.c.id, item.c.edition_id)
    probes: list[tuple[MatchedBy, sa.Select[tuple[str, str]]]] = []
    if file_hash:
        probes.append(
            (
                "file_hash",
                base.join(record, record.c.export_item_id == item.c.id).where(
                    record.c.file_hash == file_hash
                ),
            )
        )
        probes.append(
            (
                "document_alias",
                base.join(alias, alias.c.export_item_id == item.c.id).where(
                    alias.c.file_hash == file_hash
                ),
            )
        )
    if content_hash:
        probes.append(
            (
                "content_hash",
                base.join(record, record.c.export_item_id == item.c.id).where(
                    record.c.content_hash == content_hash
                ),
            )
        )
        probes.append(
            (
                "document_alias",
                base.join(alias, alias.c.export_item_id == item.c.id).where(
                    alias.c.content_hash == content_hash
                ),
            )
        )
        probes.append(
            (
                "export_content",
                base.join(content, content.c.id == item.c.content_id).where(
                    sa.or_(
                        content.c.content_hash == content_hash,
                        content.c.derived_hash == content_hash,
                    )
                ),
            )
        )
    for matched_by, query in probes:
        found = _first(connection, query)
        if found is not None:
            return OwnExport(found[0], found[1], matched_by)
    return None


def absorb_echo(
    engine: Engine,
    *,
    request_id: str,
    staged: Path,
    kind: str,
    file_hash: str,
    size: int,
    remote_ref: str,
    keep_shell: bool = False,
) -> EchoOfOwnExport | None:
    """If the received file is an echo of an export of ours, record it as a replica of that
    export and take back what the request had made for it (its wanted Variant and Unit, which
    hold no content): the file leaves nothing in the catalogue. None when it is not an echo.

    `keep_shell`: a Unit a followed series announces stays, and its request is only closed as
    abandoned, which a cycle does not ask again (taking the Unit away would have the next cycle
    announce it again).

    One transaction: the replica exists iff the request's shell is gone.
    """
    payload = received_content_hash(staged, kind)
    request, variant, unit = acquisition.download_request, catalog.unit_variant, catalog.unit
    with engine.connect() as connection:
        connection.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
        with connection.begin():
            own = own_export_of(connection, file_hash=file_hash, content_hash=payload)
            if own is None:
                return None
            replica_id = upstream_echo_of(connection, own.export_item_id, remote_ref, file_hash)
            if replica_id is None:
                replica_id = record_replica(
                    connection,
                    unit_id=None,
                    export_item_id=own.export_item_id,
                    location="upstream",
                    content_hash=payload,
                    file_hash=file_hash,
                    independent=False,  # a source's copy is not one we hold (EF-BIB-045)
                    remote_ref=remote_ref,
                    size=size,
                )
            row = connection.execute(
                sa.select(request.c.variant_id).where(request.c.id == request_id)
            ).first()
            if keep_shell:
                connection.execute(
                    sa.update(request).where(request.c.id == request_id).values(state="abandoned")
                )
            else:
                connection.execute(sa.delete(request).where(request.c.id == request_id))
            if not keep_shell and row is not None and row.variant_id is not None:
                _drop_empty_shell(connection, row.variant_id, variant, unit)
    log.info(
        "acquire.echo_of_own_export",
        request_id=request_id,
        matched_by=own.matched_by,
        export_item_id=own.export_item_id,
    )
    return EchoOfOwnExport(own, replica_id)


def _drop_empty_shell(
    connection: Connection, variant_id: str, variant: sa.Table, unit: sa.Table
) -> None:
    """The Variant the request made and the Unit it made for it, when nothing else holds them.
    A shell that something references is left where it is: the replica is what matters."""
    unit_id = connection.execute(
        sa.select(variant.c.unit_id).where(variant.c.id == variant_id)
    ).scalar()
    held = connection.execute(
        sa.select(storage.content.c.id).where(storage.content.c.variant_id == variant_id)
    ).first()
    if held is not None:
        return
    with suppress(sa.exc.IntegrityError), connection.begin_nested():
        connection.execute(sa.delete(variant).where(variant.c.id == variant_id))
    if unit_id is None:
        return
    others = connection.execute(
        sa.select(variant.c.id).where(variant.c.unit_id == unit_id).limit(1)
    ).first()
    if others is None:
        with suppress(sa.exc.IntegrityError), connection.begin_nested():
            connection.execute(sa.delete(unit).where(unit.c.id == unit_id))
