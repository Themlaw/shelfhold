"""Who may read which byte of a book, and the ticket that says so (EF-LEC-005, ENF-SEC-004).

`/content/{ticket}/{path}` is the only door through which an octet of a book reaches a browser. The
ticket (`reading.domain.content_ticket`) is a signed, short-lived word bound to a profile, a
carrier,
a scope and the session that asked for it. Holding it is **not** being allowed: on every request
this service looks again, in this order and before it reads or hashes anything,

1. the ticket is ours, unexpired, and the session behind it is the caller's (the cookie of the
   session, or, on a separate origin where no cookie is sent, a live session of the profile whose
   fingerprint the ticket carries);
2. the profile still sees the carrier (`check_carrier`: the one filter of every surface), so a
   ceiling lowered or a Work reclassified takes effect on the next byte;
3. the entry is a plain name of the archive, and, for an excerpt, one of the closed list the server
   holds for that ticket;
4. only then is the archive opened, by the port, which reads the **neutralized derivative** of an
   `untrusted` EPUB and never its original (ADR-0047, I35 extended).

Every refusal is `ContentRefusedError`: one answer, so a client learns nothing of what exists.
Nothing here is client-supplied as a path: a name is only ever compared with the names of the
archive the server chose.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import sqlalchemy as sa

from shelfhold.identity.public import live_session_ids
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
    "ContentKey",
    "ContentRefusedError",
    "IssuedTicket",
    "NoReadableCopyError",
    "ServedEntry",
]

_INFO = b"shelfhold/content-ticket/v1"
_KEY_TTL_S = 30.0
_MAX_LISTS = 2048


class NoReadableCopyError(LookupError):
    """The carrier has no file that may be read (no derivative, a format that is not read here)."""


class ContentRefusedError(LookupError):
    """The one answer of every refusal of `/content`: a ticket that is not good, a profile that
    may not see, an entry that is not allowed or not there."""

    def __init__(self) -> None:
        super().__init__("content refused")


class ContentArchive(Protocol):
    def names(self) -> frozenset[str]: ...
    def read(self, name: str) -> bytes: ...
    def close(self) -> None: ...


class ArchiveSource(Protocol):
    """The files of a carrier that may be read. `NoReadableCopyError` when there is none."""

    def open(self, carrier_type: str, carrier_id: str) -> ContentArchive: ...


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
class ServedEntry:
    body: bytes
    media_type: str
    filename: str
    etag: str


class _Lists:
    """The closed lists of entries of the excerpts in force, by digest. They live here, in this
    process, for as long as a ticket can: the ticket carries only the digest."""

    def __init__(self) -> None:
        self._by_digest: OrderedDict[str, tuple[frozenset[str], float]] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, entries: frozenset[str], until: float, now: float) -> str:
        digest = hashlib.sha256("\n".join(sorted(entries)).encode()).hexdigest()
        with self._lock:
            for old in [d for d, (_e, u) in self._by_digest.items() if u <= now]:
                del self._by_digest[old]
            self._by_digest[digest] = (
                entries,
                max(until, self._by_digest.get(digest, (None, 0))[1]),
            )
            self._by_digest.move_to_end(digest)
            while len(self._by_digest) > _MAX_LISTS:
                self._by_digest.popitem(last=False)
        return digest

    def get(self, digest: str, now: float) -> frozenset[str] | None:
        with self._lock:
            found = self._by_digest.get(digest)
        return None if found is None or found[1] <= now else found[0]


class ContentAccess:
    def __init__(
        self,
        engine: sa.Engine,
        key: Callable[[], bytes],
        now: Callable[[], datetime],
        archives: ArchiveSource,
        *,
        content_origin: str | None = None,
    ) -> None:
        self._engine, self._key, self._now = engine, key, now
        self.archives = archives  # a field: the port is the composition's, a test may swap it
        self.content_origin = content_origin.rstrip("/") if content_origin else None
        self._lists = _Lists()

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
        with self._engine.connect() as connection:
            check_carrier(connection, user_id, carrier_type, carrier_id)
        now = self._now()
        expires = now + timedelta(seconds=TICKET_TTL_S)
        digest: str | None = None
        if scope == "excerpt":
            if not entries or not all(safe_entry(e) for e in entries):
                raise ValueError("an excerpt ticket names a closed list of plain entries")
            digest = self._lists.put(frozenset(entries), expires.timestamp(), now.timestamp())
        elif entries is not None:
            raise ValueError("only an excerpt ticket carries a list of entries")
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

    def url(self, token: str, entry: str) -> str:
        """Where the reader fetches `entry`: this origin, or the separate one when it is set."""
        return f"{self.content_origin or ''}/content/{token}/{entry}"

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
                check_carrier(connection, claims.user_id, claims.carrier_type, claims.carrier_id)
        except UnknownCarrierError:
            raise ContentRefusedError from None
        if not safe_entry(entry):
            raise ContentRefusedError
        if claims.scope == "excerpt":
            allowed = self._lists.get(claims.entries or "", now.timestamp())
            if allowed is None or entry not in allowed:
                raise ContentRefusedError
        try:
            archive = self.archives.open(claims.carrier_type, claims.carrier_id)
        except NoReadableCopyError:
            raise ContentRefusedError from None
        try:
            if entry not in archive.names():
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
            live = live_session_ids(connection, claims.user_id, now.astimezone(UTC))
        return any(_same(session_fingerprint(key, s), claims.session_fp) for s in live)


def _same(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode(), right.encode())
