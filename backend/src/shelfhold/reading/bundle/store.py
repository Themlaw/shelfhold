"""Versions of a block: what is written, reused, and purged (ADR-0053 §5, §8; ADR-0048).

A version is made at the first download of a content, not when the feed is drawn. Its identity is
`content_fp`, a keyed fingerprint (HMAC-SHA-256 under a key derived from the keyring) of the names,
sizes and CRC-32 of every entry, so that the same members always give the same fingerprint, on any
restart, and nobody can compute it without the key. It is also written in the first KiB of the
file, and recorded as the `document_alias` of the version (`sha256:<fp>`, with KOReader's partial
MD5 of the bytes) so that a position sent by the e-reader finds its version (L47).

The counter of a version is per (Edition, part, profile) and is never reused. Retention keeps the
latest `keep_versions` and any version used in the last `keep_days`; a purged version keeps a row
that answers 410, loses its members (no fingerprint of a file is kept) and its alias.
"""

import hashlib
import hmac
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import bundle, export
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.tombstone_aead import hkdf_sha256
from shelfhold.reading.bundle.limits import BundleLimits
from shelfhold.reading.domain.bundle_zip import ZipEntry

_INFO = b"shelfhold/bundle/v1"


def fingerprint_key(keyring: Keyring) -> bytes:
    """The oldest key of the keyring, so a rotation does not change any fingerprint (the keyring
    keeps its retired keys, and a backup holds them)."""
    oldest = min(keyring.keys, key=lambda k: (k.created_at, k.kid))
    return hkdf_sha256(oldest.material, info=_INFO)


def content_fp(key: bytes, entries: Sequence[ZipEntry]) -> str:
    manifest = "".join(f"{e.name}\0{e.size}\0{e.crc32}\n" for e in entries).encode("ascii")
    return hmac.new(key, manifest, hashlib.sha256).hexdigest()


def member_fp(key: bytes, file_hash: str | None) -> str:
    """Of the file the manifest says is there: a purged hash leaves no clear trace here."""
    return hmac.new(key, (file_hash or "").encode("ascii"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class MemberRow:
    rank: int
    item_id: str | None
    unit_id: str | None
    label: str
    fp: str | None
    pages_digest: str | None
    first_page: int
    page_count: int
    canonical: bool


@dataclass(frozen=True)
class VersionRow:
    id: str
    edition_id: str
    part_index: int
    profile_user_id: str
    number: int
    content_fp: str
    koreader_md5: str
    size: int
    state_key: str
    excluded: dict[str, int]
    purged: bool


def _row(r: sa.Row[tuple[object, ...]]) -> VersionRow:
    return VersionRow(
        r.id,
        r.edition_id,
        r.part_index,
        r.profile_user_id,
        r.number,
        r.content_fp,
        r.koreader_md5,
        r.size,
        r.state_key,
        dict(r.excluded or {}),
        r.purged_at is not None,
    )


def latest(
    connection: Connection, edition_id: str, part_index: int, user_id: str
) -> VersionRow | None:
    v = bundle.bundle_version
    row = connection.execute(
        sa.select(v)
        .where(
            v.c.edition_id == edition_id,
            v.c.part_index == part_index,
            v.c.profile_user_id == user_id,
            v.c.purged_at.is_(None),
        )
        .order_by(v.c.number.desc())
        .limit(1)
    ).first()
    return _row(row) if row else None


def next_number(connection: Connection, edition_id: str, part_index: int, user_id: str) -> int:
    v = bundle.bundle_version
    top = connection.execute(
        sa.select(sa.func.max(v.c.number)).where(
            v.c.edition_id == edition_id,
            v.c.part_index == part_index,
            v.c.profile_user_id == user_id,
        )
    ).scalar()
    return int(top or 0) + 1


def by_id(connection: Connection, version_id: str) -> VersionRow | None:
    row = connection.execute(
        sa.select(bundle.bundle_version).where(bundle.bundle_version.c.id == version_id)
    ).first()
    return _row(row) if row else None


def members_of(connection: Connection, version_id: str) -> list[MemberRow]:
    m = bundle.bundle_member
    rows = connection.execute(
        sa.select(m).where(m.c.version_id == version_id).order_by(m.c.rank)
    ).all()
    return [
        MemberRow(
            r.rank,
            r.export_item_id,
            r.unit_id,
            r.chapter_label,
            r.content_fp,
            r.pages_digest,
            r.first_page,
            r.page_count,
            r.page_map_state == "candidate",
        )
        for r in rows
    ]


def mark_served(connection: Connection, version_id: str, now: datetime) -> None:
    v = bundle.bundle_version
    connection.execute(sa.update(v).where(v.c.id == version_id).values(last_used_at=now))


def reusable(
    connection: Connection, edition_id: str, part_index: int, user_id: str, fp: str
) -> VersionRow | None:
    v = bundle.bundle_version
    row = connection.execute(
        sa.select(v).where(
            v.c.edition_id == edition_id,
            v.c.part_index == part_index,
            v.c.profile_user_id == user_id,
            v.c.content_fp == fp,
            v.c.purged_at.is_(None),
        )
    ).first()
    return _row(row) if row else None


def create(
    connection: Connection,
    *,
    edition_id: str,
    part_index: int,
    user_id: str,
    alias_item_id: str,
    fp: str,
    koreader_md5: str,
    state_key: str,
    size: int,
    entry_count: int,
    excluded: dict[str, int],
    members: Sequence[MemberRow],
    now: datetime,
) -> VersionRow:
    version_id = new_ulid()
    number = next_number(connection, edition_id, part_index, user_id)
    connection.execute(
        sa.insert(bundle.bundle_version).values(
            id=version_id,
            edition_id=edition_id,
            part_index=part_index,
            profile_user_id=user_id,
            number=number,
            content_fp=fp,
            koreader_md5=koreader_md5,
            state_key=state_key,
            size=size,
            entry_count=entry_count,
            page_count=sum(m.page_count for m in members),
            member_count=len(members),
            excluded=excluded,
            last_used_at=now,
        )
    )
    connection.execute(
        sa.insert(bundle.bundle_member),
        [
            {
                "version_id": version_id,
                "rank": m.rank,
                "export_item_id": m.item_id,
                "unit_id": m.unit_id,
                "chapter_label": m.label,
                "content_fp": m.fp,
                "pages_digest": m.pages_digest,
                "first_page": m.first_page,
                "page_count": m.page_count,
                "page_map_state": "candidate" if m.canonical else "unverified",
            }
            for m in members
        ],
    )
    connection.execute(
        sa.insert(export.document_alias)
        .prefix_with("OR IGNORE")
        .values(
            export_item_id=alias_item_id,
            file_hash=f"sha256:{fp}",
            content_hash=f"sha256:{fp}",
            koreader_partial_md5=koreader_md5,
        )
    )
    row = by_id(connection, version_id)
    assert row is not None  # noqa: S101
    return row


def prune_one_profile(
    connection: Connection,
    edition_id: str,
    part_index: int,
    user_id: str,
    now: datetime,
    limits: BundleLimits,
) -> int:
    v = bundle.bundle_version
    rows = connection.execute(
        sa.select(v.c.id, v.c.content_fp, v.c.created_at, v.c.last_used_at)
        .where(
            v.c.edition_id == edition_id,
            v.c.part_index == part_index,
            v.c.profile_user_id == user_id,
            v.c.purged_at.is_(None),
        )
        .order_by(v.c.number.desc())
    ).all()
    cutoff = now - timedelta(days=limits.keep_days)
    doomed = [r for r in rows[limits.keep_versions :] if (r.last_used_at or r.created_at) < cutoff]
    for r in doomed:
        retire(connection, r.id, r.content_fp, edition_id, part_index, now)
    return len(doomed)


def retire(
    connection: Connection,
    version_id: str,
    fp: str,
    edition_id: str,
    part_index: int,
    now: datetime,
) -> None:
    v, m = bundle.bundle_version, bundle.bundle_member
    connection.execute(sa.delete(m).where(m.c.version_id == version_id))
    connection.execute(sa.update(v).where(v.c.id == version_id).values(purged_at=now))
    shared = connection.execute(
        sa.select(sa.func.count()).where(
            v.c.edition_id == edition_id,
            v.c.part_index == part_index,
            v.c.content_fp == fp,
            v.c.purged_at.is_(None),
        )
    ).scalar_one()
    if not shared:  # no other profile holds the same file: its digest resolves nothing any more
        connection.execute(
            sa.delete(export.document_alias).where(
                export.document_alias.c.file_hash == f"sha256:{fp}",
                export.document_alias.c.export_item_id.in_(
                    sa.select(bundle.bundle_partition.c.export_item_id).where(
                        bundle.bundle_partition.c.edition_id == edition_id,
                        bundle.bundle_partition.c.part_index == part_index,
                    )
                ),
            )
        )


def prune_all(connection: Connection, now: datetime, limits: BundleLimits) -> int:
    v = bundle.bundle_version
    groups = connection.execute(
        sa.select(v.c.edition_id, v.c.part_index, v.c.profile_user_id)
        .where(v.c.purged_at.is_(None))
        .distinct()
    ).all()
    return sum(
        prune_one_profile(connection, g.edition_id, g.part_index, g.profile_user_id, now, limits)
        for g in groups
    )
