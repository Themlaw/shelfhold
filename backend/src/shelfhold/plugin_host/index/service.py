"""The client of a signed plugin index (EF-SRC-017, D14, ADR-0056): pin, refresh, revoke, fetch.

A repository is pinned by its URL and by the *public key* the admin gave (its key id is the
fingerprint shown everywhere); none ships with the product. A refresh goes through the host's
Fetcher — the SSRF guard, the size bound, the neutral User-Agent — and verifies the minisign
signature before anything is believed: the prehashed format, the pinned key id, the trusted
comment bound to the document's own version and expiry, the expiry (never more than 90 days
out), and the monotone `version` (a replayed index, and two indexes for one number, are
refused). Revocations are kept once seen, even if a later index omits them. A rotation moves
the pin only on a double signature: the same document signed by the pinned key and by the next
one the document names. A wheel is fetched by the same Fetcher and its sha256 is checked
against what the index vouches for — before the guard of M1b ever sees the file.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import sqlalchemy as sa
import structlog
from shelfhold_sdk.fetcher import Response
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fetcher import FetchLimits, GuardedFetcher, Network
from shelfhold.infrastructure.fetcher.client import Download
from shelfhold.infrastructure.fetcher.errors import FetchError
from shelfhold.infrastructure.http_headers import require_plain_web_url
from shelfhold.infrastructure.schema import acquisition
from shelfhold.plugin_host.index.document import (
    MAX_AHEAD,
    IndexDocument,
    IndexEntry,
    parse_document,
    trusted_comment_of,
)
from shelfhold.plugin_host.index.minisign import (
    PluginIndexError,
    parse_public_key,
    verify_detached,
)

__all__ = ["MAX_INDEX_BYTES", "MAX_WHEEL_BYTES", "RepoRecord", "RepoStore", "index_fetcher"]

log = structlog.get_logger(__name__)

MAX_INDEX_BYTES = 1024 * 1024  # an index document, not a page
MAX_WHEEL_BYTES = 64 * 1024 * 1024  # a plugin wheel; a bigger one is refused while it streams
INDEX_USER_AGENT = "shelfhold-plugin-index"  # the product, nothing of the instance
SIGNATURE_SUFFIX = ".minisig"
NEXT_SIGNATURE_SUFFIX = ".next.minisig"


class IndexFetcher(Protocol):
    """The host's Fetcher: the only way out, behind the SSRF guard at every hop."""

    async def get(
        self,
        url: str,
        *,
        headers: Any = None,
        params: Any = None,
        referer: str | None = None,
        max_bytes: int | None = None,
    ) -> Response: ...

    async def download(
        self,
        url: str,
        sink: Any,
        *,
        max_bytes: int,
        headers: Any = None,
        referer: str | None = None,
        total_timeout: float | None = None,
    ) -> Download: ...


def index_fetcher(network: Network) -> GuardedFetcher:
    """The one client of an index refresh: guarded, bounded while it reads, neutral agent."""
    limits = FetchLimits(max_bytes=MAX_INDEX_BYTES, total_timeout=30.0, max_redirects=2)
    return GuardedFetcher(
        network.resolver,
        limits=limits,
        transport=network.transport,
        user_agent=INDEX_USER_AGENT,
        quiet_failures=True,  # a refused refresh is an `info` line, not an incident
    )


@dataclass(frozen=True)
class RepoRecord:
    """A repository as it is pinned: the URL, the key, the last version believed."""

    url: str
    pubkey: str
    key_id: str
    last_version: int
    expires: datetime | None
    added_at: datetime
    content_sha256: str | None = None  # the document of `last_version`: two indexes for one
    # number are refused, so what the number said is remembered


@dataclass(frozen=True)
class RefreshOutcome:
    """What a refresh believed: the verified document, and whether anything changed."""

    document: IndexDocument
    applied: bool  # False: the same version, the same content, nothing to do
    rotated: bool = False


def _record(row: Any) -> RepoRecord:
    return RepoRecord(
        url=row["url"],
        pubkey=row["pubkey"],
        key_id=row["key_id"],
        last_version=row["last_version"],
        expires=row["expires"],
        added_at=row["added_at"],
        content_sha256=row["content_sha256"],
    )


class RepoStore:
    """The repositories the admin pinned, and what their indexes said, in the host's database."""

    def __init__(self, engine: sa.Engine, *, now: Callable[[], datetime]) -> None:
        self._engine = engine
        self._now = now

    # -- the pin ---------------------------------------------------------------------------------

    def list(self) -> list[RepoRecord]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(acquisition.plugin_repo).order_by(acquisition.plugin_repo.c.url)
            ).mappings()
            return [_record(row) for row in rows]

    def add(self, url: str, pubkey: str) -> RepoRecord:
        """Pin a repository by its URL and the public key of its signer (the reserved action).

        Nothing is fetched here: the pin is the admin's word, and the first refresh will have
        to be signed by exactly this key. Without the key there is no pin — a repository is
        never trusted on its own word.
        """
        try:
            plain = require_plain_web_url(url.strip())
        except ValueError:
            raise PluginIndexError(
                "url_invalid", "the repository address is not a plain URL"
            ) from None
        key = parse_public_key(pubkey, code="fingerprint_required")
        with write_transaction(self._engine) as connection:
            existing = connection.execute(
                sa.select(acquisition.plugin_repo.c.url).where(
                    acquisition.plugin_repo.c.url == plain
                )
            ).scalar()
            if existing is not None:
                raise PluginIndexError("already_pinned", "this repository is already pinned")
            connection.execute(
                sqlite_insert(acquisition.plugin_repo)
                .values(url=plain, pubkey=key.b64, key_id=key.key_id)
                .on_conflict_do_nothing()
            )
        return next(record for record in self.list() if record.url == plain)

    def remove(self, url: str) -> None:
        """Take the pin away; a repository added back starts from version zero (D14)."""
        with write_transaction(self._engine) as connection:
            deleted = connection.execute(
                sa.delete(acquisition.plugin_repo).where(acquisition.plugin_repo.c.url == url)
            ).rowcount
        if not deleted:
            raise PluginIndexError("not_found", "no pinned repository at this address")

    # -- the refresh -----------------------------------------------------------------------------

    async def refresh(self, record: RepoRecord, fetcher: IndexFetcher) -> RefreshOutcome:
        """Fetch, verify and apply an index: the Fetcher carries it, the signature decides."""
        document_bytes = await self._get(fetcher, record.url)
        signature = await self._get(fetcher, record.url + SIGNATURE_SUFFIX)
        key = parse_public_key(record.pubkey, code="key_mismatch")
        comment = verify_detached(document_bytes, signature, key)
        document = parse_document(document_bytes)
        if comment != trusted_comment_of(document):
            raise PluginIndexError(
                "trusted_comment_mismatch",
                "the signed comment does not say the document's own version and expiry",
            )
        now = self._now()
        if document.expires <= now:
            raise PluginIndexError("expired", "the index has expired")
        if document.expires - now > MAX_AHEAD:
            raise PluginIndexError("expires_too_far", "the index expires in more than 90 days")
        if document.version < record.last_version:
            raise PluginIndexError(
                "rollback", f"version {document.version} is behind the last one seen"
            )
        content = "sha256:" + hashlib.sha256(document_bytes).hexdigest()
        if document.version == record.last_version:
            if content == record.content_sha256:
                return RefreshOutcome(document, applied=False)  # the same number, the same words
            raise PluginIndexError("same_version", "two indexes exist for one version number")

        rotated = False
        pubkey, key_id = record.pubkey, record.key_id
        if document.next_pubkey is not None:
            # the pin moves only on a double signature: the pinned key above, the next key here
            try:
                next_signature = await self._get(fetcher, record.url + NEXT_SIGNATURE_SUFFIX)
                next_comment = verify_detached(document_bytes, next_signature, document.next_pubkey)
            except PluginIndexError:
                raise PluginIndexError(
                    "rotation_refused", "the next key did not sign the same document"
                ) from None
            if next_comment != trusted_comment_of(document):
                raise PluginIndexError(
                    "rotation_refused", "the next key did not sign the same document"
                )
            pubkey, key_id = document.next_pubkey.b64, document.next_pubkey.key_id
            rotated = True
        self._apply(record, document, pubkey, key_id, content)
        return RefreshOutcome(document, applied=True, rotated=rotated)

    async def _get(self, fetcher: IndexFetcher, url: str) -> bytes:
        try:
            response = await fetcher.get(url, max_bytes=MAX_INDEX_BYTES)
        except FetchError as exc:
            raise PluginIndexError("fetch_refused", exc.code) from None
        if response.status != 200 or not response.body:
            raise PluginIndexError("unreachable", f"the repository answered {response.status}")
        return response.body

    def _apply(
        self, record: RepoRecord, document: IndexDocument, pubkey: str, key_id: str, content: str
    ) -> None:
        """Keep what the index said: the version floor, the expiry, the revocations, the pin."""
        with write_transaction(self._engine) as connection:
            connection.execute(
                sa.update(acquisition.plugin_repo)
                .where(acquisition.plugin_repo.c.url == record.url)
                .values(
                    last_version=document.version,
                    expires=document.expires,
                    pubkey=pubkey,
                    key_id=key_id,
                    content_sha256=content,
                )
            )
            for plugin_key in sorted(document.revoked_plugin_keys):
                connection.execute(
                    sqlite_insert(acquisition.plugin_revocation)
                    .values(kind="plugin_key", value=plugin_key)
                    .on_conflict_do_nothing()
                )
            for wheel in sorted(document.revoked_wheel_sha256):
                connection.execute(
                    sqlite_insert(acquisition.plugin_revocation)
                    .values(kind="wheel_sha256", value=wheel)
                    .on_conflict_do_nothing()
                )
        log.info(
            "plugin_index.refreshed",
            url=record.url,
            version=document.version,
            rotated=pubkey != record.pubkey,
        )

    # -- what the index said ---------------------------------------------------------------------

    def revoked_plugin_keys(self) -> frozenset[str]:
        return self._revocations("plugin_key")

    def revoked_wheel_sha256(self) -> frozenset[str]:
        return self._revocations("wheel_sha256")

    def _revocations(self, kind: str) -> frozenset[str]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(acquisition.plugin_revocation.c.value).where(
                    acquisition.plugin_revocation.c.kind == kind
                )
            )
            return frozenset(row[0] for row in rows)

    # -- the wheel -------------------------------------------------------------------------------

    async def fetch_wheel(
        self, entry: IndexEntry, fetcher: IndexFetcher, destination: Path
    ) -> Path:
        """Fetch a wheel the index vouches for and check its sha256 before anything else sees it.

        The Fetcher streams it to a staging file of `destination` (never the whole wheel in
        memory); a hash that is not the one the index signed is a refusal and leaves nothing
        behind — the guard of M1b never sees a file the index did not vouch for.
        """
        destination.mkdir(parents=True, exist_ok=True)
        handle, staged = tempfile.mkstemp(dir=destination, prefix=".index-wheel-", suffix=".whl")
        path = Path(staged)
        digest = hashlib.sha256()
        try:
            with os.fdopen(handle, "wb") as wheel:
                try:
                    download = await fetcher.download(
                        entry.wheel_url, wheel, max_bytes=MAX_WHEEL_BYTES
                    )
                except FetchError as exc:
                    raise PluginIndexError("fetch_refused", exc.code) from None
            if download.status != 200:
                raise PluginIndexError("unreachable", f"the repository answered {download.status}")
            if download.sha256 != entry.sha256:
                raise PluginIndexError(
                    "wheel_hash_mismatch", "the wheel is not the one the index vouches for"
                )
            with path.open("rb") as wheel:  # the stream's word is not enough: the file is read back
                for chunk in iter(lambda: wheel.read(1024 * 1024), b""):
                    digest.update(chunk)
            if "sha256:" + digest.hexdigest() != entry.sha256:
                raise PluginIndexError(
                    "wheel_hash_mismatch", "the wheel is not the one the index vouches for"
                )
        except PluginIndexError:
            path.unlink(missing_ok=True)  # nothing of a refused wheel stays on disk
            raise
        return path
