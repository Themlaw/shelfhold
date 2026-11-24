"""The client of a signed plugin index (EF-SRC-017, D14, ADR-0056): pin, refresh, revoke, fetch,
install.

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

Since M6 L08 (passe 8, GAP-8-13) a plugin is also *installed* from an index: the wheel the entry
vouches for, fetched and verified, then through the guard like any other. The install is pinned
to the triple (key, index, signer) — and a second index that publishes the same key signed by
another signer is never an update: the install is refused and a distinct one proposed. The
distinct install inherits nothing: the state, the settings and the secrets of the previous
install are held under its signer's fingerprint, and move only on the admin's confirmed word.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import urlsplit

import sqlalchemy as sa
import structlog
from shelfhold_sdk.fetcher import Response
from sqlalchemy import Connection
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.identity.public import AuditEntry, Principal, audit_actor, audit_insert
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fetcher import FetchLimits, GuardedFetcher, Network
from shelfhold.infrastructure.fetcher.client import Download
from shelfhold.infrastructure.fetcher.errors import FetchError
from shelfhold.infrastructure.http_headers import require_plain_web_url
from shelfhold.infrastructure.schema import acquisition
from shelfhold.plugin_host.attestation import THIRD_PARTY_ORIGIN
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
from shelfhold.plugin_host.index.staging import discard_staged_wheel, stage_wheel
from shelfhold.plugin_host.install import (
    Installed,
    Layout,
    install_wheel,
    keep_wheel,
    wheel_plugin_keys,
)
from shelfhold.plugin_host.lifecycle import (
    HOST_VERSION,
    reattach_if_same_origin,
)
from shelfhold.plugin_host.lifecycle import (
    version_key as _version_key,
)
from shelfhold.plugin_host.public import installed_keys
from shelfhold.plugin_host.rpc.runner_wire import KEY as PLUGIN_KEY
from shelfhold.plugin_host.settings import Vault, held_identity, hold_plugin_data

__all__ = [
    "MAX_INDEX_BYTES",
    "MAX_WHEEL_BYTES",
    "IndexInstall",
    "RepoRecord",
    "RepoStore",
    "index_fetcher",
]

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


@dataclass(frozen=True)
class IndexInstall:
    """What an install from an index did: the plugin the guard installed, the held identity
    of a previous install (another signer) whose data waits for the admin's confirmed word,
    and the index's own word about the entry — deprecated, and its named replacement (the
    word is the admin's to read; the replacement is never installed by it)."""

    installed: Installed
    held: str | None = None
    deprecated: bool = False
    replaced_by: str | None = None


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

    def add(self, url: str, pubkey: str, *, principal: Principal | None = None) -> RepoRecord:
        """Pin a repository by its URL and the public key of its signer (the reserved action).

        Nothing is fetched here: the pin is the admin's word, and the first refresh will have
        to be signed by exactly this key. Without the key there is no pin — a repository is
        never trusted on its own word. Over HTTP the audit line (`url:key_id`, like the CLI) is
        written in the same transaction: no pin without its trace.
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
            if principal is not None:
                self._audit(connection, principal, "plugin.index_add", f"{plain}:{key.key_id}")
        return next(record for record in self.list() if record.url == plain)

    def remove(self, url: str, *, principal: Principal | None = None) -> None:
        """Take the pin away; a repository added back starts from version zero (D14)."""
        with write_transaction(self._engine) as connection:
            deleted = connection.execute(
                sa.delete(acquisition.plugin_repo).where(acquisition.plugin_repo.c.url == url)
            ).rowcount
            if not deleted:
                raise PluginIndexError("not_found", "no pinned repository at this address")
            if principal is not None:
                self._audit(connection, principal, "plugin.index_remove", url)

    # -- the refresh -----------------------------------------------------------------------------

    async def refresh(
        self, record: RepoRecord, fetcher: IndexFetcher, *, principal: Principal | None = None
    ) -> RefreshOutcome:
        """Fetch, verify and apply an index: the Fetcher carries it, the signature decides.

        The checks against `record` are an early refusal only: the network is awaited after
        them, so another refresh may have applied a newer index (or a rotation) meanwhile. The
        floor and the pin are therefore re-read under the write lock in `_apply`, which refuses
        a document that would roll the floor back or take a retired key back.
        """
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
        applied = self._apply(record, document, pubkey, key_id, content, principal=principal)
        return RefreshOutcome(document, applied=applied, rotated=rotated)

    async def _get(self, fetcher: IndexFetcher, url: str) -> bytes:
        try:
            response = await fetcher.get(url, max_bytes=MAX_INDEX_BYTES)
        except FetchError as exc:
            raise PluginIndexError("fetch_refused", exc.code) from None
        if response.status != 200 or not response.body:
            raise PluginIndexError("unreachable", f"the repository answered {response.status}")
        return response.body

    def _audit(self, connection: Any, principal: Principal, action: str, target: str) -> None:
        """The audit line of an HTTP action, on the caller's connection: the action and its
        trace commit or fail together."""
        connection.execute(
            audit_insert(
                AuditEntry(
                    actor_id=audit_actor(principal),
                    action=action,
                    at=self._now(),
                    target=target,
                    session_id=principal.session_id,
                    auth_time=principal.auth_time,
                )
            )
        )

    def _apply(
        self,
        record: RepoRecord,
        document: IndexDocument,
        pubkey: str,
        key_id: str,
        content: str,
        *,
        principal: Principal | None = None,
    ) -> bool:
        """Keep what the index said: the version floor, the expiry, the revocations, the pin.

        The row is re-read under the write lock (`BEGIN IMMEDIATE`): the `record` the caller
        handed was read before the network answered, and another refresh may have moved the
        floor or the pin since. An older index, a second index for a number already kept, or a
        document signed by a key a rotation retired are refused here, never applied. False:
        the same number said the same thing, nothing changed.
        """
        with write_transaction(self._engine) as connection:
            row = (
                connection.execute(
                    sa.select(acquisition.plugin_repo).where(
                        acquisition.plugin_repo.c.url == record.url
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise PluginIndexError(
                    "not_found", "the repository was removed while the index was read"
                )
            current = _record(row)
            if document.version < current.last_version:
                raise PluginIndexError(
                    "rollback", f"version {document.version} is behind the last one seen"
                )
            if document.version == current.last_version:
                if content == current.content_sha256:
                    return False  # the same number, the same words: nothing to do
                raise PluginIndexError("same_version", "two indexes exist for one version number")
            if current.pubkey != record.pubkey:
                # the pin moved to another key while this document was being verified: what
                # the pinned key signed then is not what the pinned key signs now
                raise PluginIndexError(
                    "rotation_refused", "the pin moved to another key while this index was read"
                )
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
            if pubkey != record.pubkey:
                # A verified rotation is the same signer under a new key, never a new publisher:
                # the installs pinned to the retired key stay pinned to that signer. Without
                # this, the next install from the same index would hold the previous install's
                # data as if another signer had taken the key over (M6 L08 review).
                connection.execute(
                    sa.update(acquisition.plugin_install)
                    .where(
                        acquisition.plugin_install.c.index_url == record.url,
                        acquisition.plugin_install.c.signer_pubkey == record.pubkey,
                    )
                    .values(signer_pubkey=pubkey)
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
            for entry in document.plugins:
                # the index's own word about an install it vouches for: a version it
                # deprecates, or a successor it names instead of the one kept, is kept in
                # the install row — the migration moves by that word, never by what the
                # host remembers of a document it no longer has
                connection.execute(
                    sa.update(acquisition.plugin_install)
                    .where(
                        acquisition.plugin_install.c.plugin_id == entry.key,
                        acquisition.plugin_install.c.index_url == record.url,
                    )
                    .values(replaced_by=entry.replaced_by)
                )
            if principal is not None:
                self._audit(connection, principal, "plugin.index_refresh", record.url)
        log.info(
            "plugin_index.refreshed",
            url=record.url,
            version=document.version,
            rotated=pubkey != record.pubkey,
        )
        return True

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
        self,
        entry: IndexEntry,
        fetcher: IndexFetcher,
        destination: Path,
        *,
        name: str | None = None,
    ) -> Path:
        """Fetch a wheel the index vouches for and check its sha256 before anything else sees it.

        The Fetcher streams it to a staging file of `destination` (never the whole wheel in
        memory); a hash that is not the one the index signed is a refusal and leaves nothing
        behind — the guard of M1b never sees a file the index did not vouch for. `name` stages
        the wheel under the name the index vouches for: the installer reads the tags of the
        filename (M6 L08).
        """
        handle, path = stage_wheel(destination, name)
        digest = hashlib.sha256()
        accepted = False
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
            accepted = True
            return path
        finally:
            if not accepted:  # nothing of a refused wheel stays on disk, whatever refused it
                discard_staged_wheel(path)

    # -- the install from an index (M6 L08, EF-SRC-017 passe 8, GAP-8-13) -----------------------

    def _previous_pin(self, key: str) -> tuple[str | None, str | None] | None:
        """The (index, signer) the previous install of this key was pinned to, if any: the row
        outlives the removal, which is the point — the pin says whose word installed it."""
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(
                    acquisition.plugin_install.c.index_url,
                    acquisition.plugin_install.c.signer_pubkey,
                ).where(acquisition.plugin_install.c.plugin_id == key)
            ).first()
        return None if row is None else (row.index_url, row.signer_pubkey)

    def _pin(self, key: str, installed: Installed, record: RepoRecord, entry: Any) -> None:
        """Keep the triple (key, index, signer) this install is pinned to, and the api,
        `min_host` and `replaced_by` its entry declared: a later host judges the install and
        its migration by the index's own word, not by what it remembers of the document. The
        row is the one `AttestationStore.sync_installed` keeps in step at start-up; it never
        touches the pin."""
        digest = (
            installed.digest
            if installed.digest.startswith("sha256:")
            else f"sha256:{installed.digest}"
        )
        values = {
            "version": installed.version,
            "sha256": digest,
            "index_url": record.url,
            "signer_pubkey": record.pubkey,
            "api": str(entry.api),
            "min_host": entry.min_host,
            "replaced_by": entry.replaced_by,
        }
        with write_transaction(self._engine) as connection:
            connection.execute(
                sqlite_insert(acquisition.plugin_install)
                .values(plugin_id=key, origin=THIRD_PARTY_ORIGIN, enabled=False, **values)
                .on_conflict_do_update(index_elements=["plugin_id"], set_=values)
            )

    def unpin(self, key: str) -> None:
        """Take the index pin of a key away: a wheel the admin gave by hand is pinned by the
        admin's own act, and the row must not say an index signed code nobody signed (M6 L08)."""
        with write_transaction(self._engine) as connection:
            connection.execute(
                sa.update(acquisition.plugin_install)
                .where(acquisition.plugin_install.c.plugin_id == key)
                .values(index_url=None, signer_pubkey=None)
            )

    async def install_from(
        self,
        record: RepoRecord,
        key: str,
        layout: Layout,
        fetcher: IndexFetcher,
        *,
        vault: Vault | None = None,
        held_audit: Callable[[str, Connection], None] | None = None,
    ) -> IndexInstall:
        """Install the plugin this index vouches for, under the guard, its key pinned to
        (index, signer).

        The index is refreshed and verified first; the entry must exist and neither the key
        nor the wheel may be revoked. The wheel must declare exactly the key its entry names:
        an entry is the index's word for *this* plugin, and a wheel that answers under another
        key would inherit that key's state, settings and secrets (M6 L08 review). A key already
        installed is never overwritten — and when another signer publishes it, the refusal
        says what to do: remove the installed plugin and install it as a distinct one. The
        distinct install inherits nothing: what the previous install owns in the host (state,
        settings, secrets) is held under its signer's fingerprint before the wheel is fetched,
        and moves only on the admin's confirmed word. The wheel is fetched and verified by
        `fetch_wheel`, installed by the guard, and the staged file leaves with the install.
        """
        outcome = await self.refresh(record, fetcher)
        # The refresh may have rotated the pin: the signer this install is pinned to is the one
        # the repository pins now, never the key the caller read before the network answered.
        record = next((r for r in self.list() if r.url == record.url), record)
        entry = next((e for e in outcome.document.plugins if e.key == key), None)
        if entry is None:
            raise PluginIndexError("unknown_plugin", f"the index does not list {key!r}")
        if key in self.revoked_plugin_keys():
            raise PluginIndexError("plugin_revoked", f"the key of {key!r} was revoked")
        if entry.sha256 in self.revoked_wheel_sha256():
            raise PluginIndexError("wheel_revoked", "the index revoked this wheel")
        if _version_key(entry.min_host) > _version_key(HOST_VERSION):
            raise PluginIndexError(
                "min_host",
                f"the index vouches for a plugin that needs host {entry.min_host} or newer "
                f"(this host is {HOST_VERSION})",
            )
        previous = self._previous_pin(key)
        held: str | None = None
        if previous is not None and previous != (record.url, record.pubkey):
            if key in installed_keys(layout):
                raise PluginIndexError(
                    "signer_pinned",
                    "another signer publishes this key: this is not an update — remove the "
                    "installed plugin first and install it as a distinct one",
                )
            # the previous install is gone; its data is held, never inherited (GAP-8-13)
            fingerprint = (
                "wheel"
                if previous[1] is None
                else parse_public_key(previous[1], code="key_mismatch").key_id
            )
            held_id = held_identity(key, fingerprint)
            if hold_plugin_data(
                self._engine,
                vault,
                key,
                held_id,
                audit=(lambda connection: held_audit(held_id, connection))
                if held_audit is not None
                else None,
            ):
                held = held_id
        wheel_name = PurePosixPath(urlsplit(entry.wheel_url).path).name
        if not wheel_name.endswith(".whl") or wheel_name == ".whl" or not wheel_name[:-4]:
            raise PluginIndexError(
                "wheel_name_invalid", "the wheel address does not name a wheel file"
            )
        wheel = await self.fetch_wheel(entry, fetcher, layout.root / "index", name=wheel_name)
        try:
            # Validate the key format matches the expected pattern (runner_wire.KEY)
            if not PLUGIN_KEY.fullmatch(key):
                raise PluginIndexError(
                    "key_invalid",
                    f"the key {key!r} does not match the expected plugin key pattern",
                )
            if wheel_plugin_keys(wheel) != frozenset({key}):
                raise PluginIndexError(
                    "wheel_key_mismatch",
                    "the wheel does not declare exactly the key its index entry names",
                )
            installed = install_wheel(layout, wheel, entry.sha256)
            # M6 L09a (EF-SRC-020, D15): the wheel of the installed version is kept (the
            # rollback's own) while the staged copy is still there — the staged file leaves
            # with the try, the kept copy never was on sys.path
            keep_wheel(layout, key, wheel)
        finally:
            discard_staged_wheel(wheel)  # the installed copy is the guard's own
        self._pin(key, installed, record, entry)
        # the bindings orphaned by an uninstall of the same key come back only when the origin
        # did not change: the previous install's (index, signer), read before this install
        # rewrote the row — never the row itself, which now tells this install's own origin
        reattached = reattach_if_same_origin(self._engine, key, previous, record.url, record.pubkey)
        if reattached:
            log.info("plugin_lifecycle.reattached", key=key, bindings=reattached)
        return IndexInstall(
            installed, held, deprecated=entry.deprecated, replaced_by=entry.replaced_by
        )
