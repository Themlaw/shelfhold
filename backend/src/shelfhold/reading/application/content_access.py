"""Who may read which byte of a book, and the ticket that says so (EF-LEC-005, ENF-SEC-004).

`/content/{ticket}/{path}` is the only door through which an octet of a book reaches a browser. The
ticket (`reading.domain.content_ticket`) is a signed, short-lived word bound to a profile, a
carrier,
a scope and the session that asked for it. Holding it is **not** being allowed: on every request
this service looks again, in this order and before it reads or hashes anything,

1. the ticket is ours, unexpired, and the session behind it is the caller's (the cookie of the
   session, or, on a separate origin where no cookie is sent, a live session of the profile whose
   fingerprint the ticket carries);
2. the profile still sees the carrier **and the content it resolves to** (`check_carrier`, the one
   filter of every surface, on both: an `export_item` names an Edition and a `content`, and nothing
   ties them), so a ceiling lowered or a Work reclassified takes effect on the next byte;
3. the scope is allowed (ADR-0055 D-P1: a full read is the kept copy of a content that is not
   exported to a destination the profile reads; any other file is an excerpt);
4. the entry is a plain name of the archive;
5. only then is the archive opened, by the port, which is given the scope: it reads the
   **neutralized derivative** of an `untrusted` EPUB and never its original (ADR-0047, I35
   extended), and for an excerpt it computes the closed list of entries itself, from that file,
   and refuses everything outside it. The ticket carries the digest of that list, so a file that
   changed under it is refused.

Every refusal is `ContentRefusedError`: one answer, so a client learns nothing of what exists.
Nothing here is client-supplied as a path: a name is only ever compared with the names of the
archive the server chose.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import quote

import sqlalchemy as sa

from shelfhold.identity.public import live_session_ids
from shelfhold.infrastructure.export_states import LIVE_STATES
from shelfhold.infrastructure.schema import export, identity, storage
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.tombstone_aead import hkdf_sha256
from shelfhold.reading.application.progress import UnknownCarrierError, check_carrier
from shelfhold.reading.domain.content_ticket import (
    TICKET_TTL_S,
    Scope,
    TicketClaims,
    open_ticket,
    seal,
    session_fingerprint,
)
from shelfhold.reading.domain.content_types import media_type_of, safe_entry

__all__ = [
    "ArchiveSource",
    "ContentAccess",
    "ContentArchive",
    "ContentChangedError",
    "ContentKey",
    "ContentRefusedError",
    "ExcerptUnavailableError",
    "FullReadNotAllowedError",
    "IssuedTicket",
    "NoReadableCopyError",
    "ResolvedCarrier",
    "ServedEntry",
]

_INFO = b"shelfhold/content-ticket/v1"
_KEY_TTL_S = 30.0


class NoReadableCopyError(LookupError):
    """The carrier has no file that may be read (no derivative, a format that is not read here)."""


class ExcerptUnavailableError(NoReadableCopyError):
    """No excerpt can be cut from this file within the bound (a single-document EPUB, a first
    chapter beyond the share allowed): the typed refusal « extrait indisponible », never the whole
    file."""


class FullReadNotAllowedError(ValueError):
    """A full read is the kept copy of a content that is not exported to a destination the profile
    reads (ADR-0055 D-P1); anything else is an excerpt."""


class ContentChangedError(LookupError):
    """The file under a full ticket is no longer the one the ticket was made for (replaced between
    two pages): typed, so that the reader loads the manifest again. Only ever said to the holder of
    a ticket that is good and whose profile still sees the carrier."""

    def __init__(self) -> None:
        super().__init__("content changed")


class ContentRefusedError(LookupError):
    """The one answer of every refusal of `/content`: a ticket that is not good, a profile that
    may not see, an entry that is not allowed or not there."""

    def __init__(self) -> None:
        super().__init__("content refused")


class ContentArchive(Protocol):
    def names(self) -> frozenset[str]: ...
    def fingerprint(self) -> str:
        """What the file is, cheaply (its registered identity, size and time): a full ticket pins
        it, so that a file replaced between two pages is refused, not served."""
        ...

    def read(self, name: str) -> bytes: ...
    def whole(self, max_bytes: int) -> bytes:
        """The file itself (the neutralized derivative of an EPUB), for a full scope only and
        never more than `max_bytes` (`ValueError` past it): what a rendition is made from."""
        ...

    def close(self) -> None: ...


class ArchiveSource(Protocol):
    """The files of a carrier that may be read. `NoReadableCopyError` when there is none."""

    def open(self, carrier_type: str, carrier_id: str, scope: Scope = "full") -> ContentArchive:
        """The file to read for `scope`. For an excerpt the archive shows only the entries of the
        closed list the source computed (`names()` and `read()`), or `ExcerptUnavailableError`."""
        ...


class ContentKey:
    """`HKDF-SHA256(oldest key of the keyring, "shelfhold/content-ticket/v1")`: the material of the
    keyring (it is in every backup), another purpose. It never leaves this process."""

    def __init__(self, keyring: Callable[[], Keyring]) -> None:
        self._keyring = keyring
        self._cached: tuple[float, bytes] | None = None

    def value(self) -> bytes:
        import time

        now = time.monotonic()
        if self._cached is None or now - self._cached[0] > _KEY_TTL_S:
            oldest = min(self._keyring().keys, key=lambda k: (k.created_at, k.kid))
            self._cached = (now, hkdf_sha256(oldest.material, info=_INFO))
        return self._cached[1]


@dataclass(frozen=True)
class IssuedTicket:
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class ResolvedCarrier:
    """What a carrier the profile sees is: the content it resolves to, its format, and whether the
    profile may read it in full (ADR-0055 D-P1) or only as an excerpt."""

    content_id: str
    format: str
    full_allowed: bool


@dataclass(frozen=True)
class ServedEntry:
    body: bytes
    media_type: str
    filename: str
    etag: str


class ContentAccess:
    def __init__(
        self,
        engine: sa.Engine,
        key: Callable[[], bytes],
        now: Callable[[], datetime],
        archives: ArchiveSource,
        *,
        content_origin: str | None = None,
        hardened: bool = True,
    ) -> None:
        self._engine, self._key, self._now = engine, key, now
        self.archives = archives  # a field: the port is the composition's, a test may swap it
        self.content_origin = content_origin.rstrip("/") if content_origin else None
        self._hardened = hardened

    # -- issuing: called by the server for a profile it has authenticated, never from a request --

    def issue(
        self,
        *,
        user_id: str,
        session_id: str,
        carrier_type: str,
        carrier_id: str,
        scope: Scope = "full",
        entries: Collection[str] | None = None,
    ) -> IssuedTicket:
        """A ticket for `user_id` on a carrier she may see (`UnknownCarrierError` otherwise: one
        answer for absent and hidden). An excerpt names the closed list of entries it opens."""
        if entries is not None:
            raise ValueError("the list of an excerpt is computed by the server, never given")
        with self._engine.connect() as connection:
            content_id = self._checked(connection, user_id, carrier_type, carrier_id)
            if scope == "full" and not self._full_allowed(
                connection, user_id, carrier_type, content_id
            ):
                raise FullReadNotAllowedError
        digest: str | None = None
        archive = self.archives.open(carrier_type, carrier_id, scope)  # refused here: no copy, or
        try:  # an excerpt that cannot be cut; a full ticket pins the file, an excerpt its list
            digest = _digest(archive.names()) if scope == "excerpt" else _pin(archive)
        finally:
            archive.close()
        now = self._now()
        expires = now + timedelta(seconds=TICKET_TTL_S)
        key = self._key()
        token = seal(
            key,
            TicketClaims(
                user_id=user_id,
                carrier_type=carrier_type,
                carrier_id=carrier_id,
                scope=scope,
                entries=digest,
                session_fp=session_fingerprint(key, session_id),
                expires_at=int(expires.timestamp()),
            ),
        )
        return IssuedTicket(token, expires)

    def resolve(self, user_id: str, carrier_type: str, carrier_id: str) -> ResolvedCarrier:
        """The carrier as `issue` would judge it, without a ticket: `UnknownCarrierError` when the
        profile does not see it (the one answer for absent and hidden)."""
        with self._engine.connect() as connection:
            return self.resolve_in(connection, user_id, carrier_type, carrier_id)

    def resolve_in(
        self, connection: sa.Connection, user_id: str, carrier_type: str, carrier_id: str
    ) -> ResolvedCarrier:
        """`resolve` on the caller's connection: a write that must decide « full or excerpt » reads
        it inside its own transaction, so that what it checked is what it writes under."""
        content_id = self._checked(connection, user_id, carrier_type, carrier_id)
        kind = connection.execute(
            sa.select(storage.content.c.format).where(storage.content.c.id == content_id)
        ).scalar()
        if kind is None:
            raise UnknownCarrierError
        allowed = self._full_allowed(connection, user_id, carrier_type, content_id)
        return ResolvedCarrier(content_id, str(kind), allowed)

    def scope_of(
        self, token: str, *, user_id: str, carrier_type: str, carrier_id: str
    ) -> Scope | None:
        """The scope of a ticket that is ours, alive, this profile's and for this very carrier,
        else None (one answer for forged, expired and someone else's). Reads nothing else: it
        grants nothing."""
        claims = open_ticket(self._key(), token, now=self._now())
        if (
            claims is None
            or claims.user_id != user_id
            or (claims.carrier_type, claims.carrier_id) != (carrier_type, carrier_id)
        ):
            return None
        return claims.scope

    def url(self, token: str, entry: str) -> str:
        """Where the reader fetches `entry`: this origin, or the separate one when it is set. The
        name is the archive's own, so it is percent-encoded: an entry called `%2e%2e/x?y#z` must
        not climb out of `/content/<ticket>/` or end the path where the archive's name does not."""
        return f"{self.content_origin or ''}/content/{token}/{quote(entry, safe='/')}"

    # -- serving -----------------------------------------------------------------------------

    def read(
        self,
        token: str,
        entry: str,
        *,
        session: tuple[str, str] | None,
    ) -> ServedEntry:
        """The bytes of `entry` for whoever holds `token`. `session` is `(user_id, session_id)` of
        the cookie of the request, or None where no cookie is sent (a separate origin)."""
        now = self._now()
        key = self._key()
        claims = open_ticket(key, token, now=now)
        if claims is None or not self._session_ok(claims, key, session, now):
            raise ContentRefusedError
        try:
            with self._engine.connect() as connection:  # every request, before anything is read
                content_id = self._checked(
                    connection, claims.user_id, claims.carrier_type, claims.carrier_id
                )
                if claims.scope == "full" and not self._full_allowed(
                    connection, claims.user_id, claims.carrier_type, content_id
                ):
                    raise ContentRefusedError
        except UnknownCarrierError:
            raise ContentRefusedError from None
        if not safe_entry(entry):
            raise ContentRefusedError
        try:
            archive = self.archives.open(claims.carrier_type, claims.carrier_id, claims.scope)
        except NoReadableCopyError:
            raise ContentRefusedError from None
        try:
            names = archive.names()
            if claims.scope == "excerpt" and _digest(names) != claims.entries:
                raise ContentRefusedError  # the file is not the one the excerpt was cut from
            if claims.scope == "full" and _pin(archive) != claims.entries:
                raise ContentChangedError  # replaced since the ticket: load the manifest again
            if entry not in names:
                raise ContentRefusedError
            body = archive.read(entry)
        except (KeyError, OSError, ValueError):
            raise ContentRefusedError from None
        finally:
            archive.close()
        etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        return ServedEntry(body, media_type_of(entry), entry.rsplit("/", 1)[-1], etag)

    def _session_ok(
        self, claims: TicketClaims, key: bytes, session: tuple[str, str] | None, now: datetime
    ) -> bool:
        if session is not None:
            user_id, session_id = session
            return user_id == claims.user_id and _same(
                session_fingerprint(key, session_id), claims.session_fp
            )
        if self.content_origin is None:  # same origin: the cookie is always there, no cookie = no
            return False
        with self._engine.connect() as connection:  # a live session of the profile, by fingerprint
            live = live_session_ids(
                connection, claims.user_id, now.astimezone(UTC), hardened=self._hardened
            )
        return any(_same(session_fingerprint(key, s), claims.session_fp) for s in live)

    # -- what a carrier is, and what may be read of it ----------------------------------------

    @staticmethod
    def _checked(
        connection: sa.Connection, user_id: str, carrier_type: str, carrier_id: str
    ) -> str:
        """The profile sees the carrier **and** the content it resolves to: an `export_item` names
        an Edition and a `content`, and nothing ties the two. Returns the content id."""
        check_carrier(connection, user_id, carrier_type, carrier_id)
        if carrier_type == "content":
            return carrier_id
        found = connection.execute(
            sa.select(export.export_item.c.content_id).where(export.export_item.c.id == carrier_id)
        ).scalar()
        if found is None:
            raise UnknownCarrierError
        check_carrier(connection, user_id, "content", str(found))
        return str(found)

    @staticmethod
    def _full_allowed(
        connection: sa.Connection, user_id: str, carrier_type: str, content_id: str
    ) -> bool:
        """ADR-0055 D-P1: the full reader is for the kept copy (a `content`) that is not exported
        to a destination the profile reads (a live, verified account of her own there): that one is
        read where it was sent. An `export_item` is that exported file: an excerpt only."""
        if carrier_type != "content":
            return False
        record, item, account = (
            export.export_record,
            export.export_item,
            identity.destination_account,
        )
        readable_elsewhere = connection.execute(
            sa.select(sa.literal(1))
            .select_from(
                item.join(record, record.c.export_item_id == item.c.id).join(
                    account, account.c.shelf_destination_id == record.c.shelf_destination_id
                )
            )
            .where(
                item.c.content_id == content_id,
                record.c.state.in_(sorted(LIVE_STATES)),
                account.c.user_id == user_id,
                account.c.revoked_at.is_(None),
                account.c.verified_at.is_not(None),
            )
            .limit(1)
        ).first()
        return readable_elsewhere is None


def _digest(names: Collection[str]) -> str:
    return hashlib.sha256("\n".join(sorted(names)).encode()).hexdigest()


def _pin(archive: ContentArchive) -> str:
    return "fp:" + hashlib.sha256(archive.fingerprint().encode()).hexdigest()


def _same(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode(), right.encode())
