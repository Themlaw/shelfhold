"""Per-root write leases (ENF-OPS-015, 02 section 10.2).

A lease lives in `<root>/.shelfhold/lease` and exists only while a write session is open on that
root: taken at the opening, renewed by a heartbeat during the batch, checked before every write and
released at the end. Outside a session nothing is written under the library, which lets the disks
sleep. The file is read and written through `Fsops` (the only writer), on the root that was just
judged against its sentinel and generation counter.

Taking over the lease of another instance needs all of: its validity has run out, the holder's
`flock` is no longer held, and the sentinel and generation read in the same open root are sound.
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fsops import Fsops, FsopsError, ReadOnlyRootError
from shelfhold.infrastructure.fsops.identity import FileIdentity
from shelfhold.infrastructure.instance.errors import LeaseConflictError, LeaseLostError
from shelfhold.infrastructure.instance.lock import holder_is_alive

LEASE_VALIDITY = timedelta(minutes=3)
HEARTBEAT_INTERVAL = timedelta(seconds=30)


@dataclass(frozen=True)
class Lease:
    instance_id: str
    renewed_at: datetime
    lock_path: str | None = (
        None  # the holder's instance lock, to tell a live holder from a dead one
    )

    def dumps(self) -> bytes:
        return json.dumps(
            {
                "instance_id": self.instance_id,
                "renewed_at": self.renewed_at.isoformat(),
                "lock_path": self.lock_path,
            }
        ).encode()

    @classmethod
    def parse(cls, raw: bytes) -> "Lease":
        try:
            doc = json.loads(raw)
            lock_path = doc.get("lock_path")
            return cls(
                str(doc["instance_id"]),
                datetime.fromisoformat(doc["renewed_at"]),
                str(lock_path) if lock_path else None,
            )
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            raise LeaseConflictError("unreadable lease file: not taking it over") from error


class WriteSessions:
    def __init__(
        self,
        fsops: Fsops,
        *,
        instance_id: str,
        clock: Clock,
        lock_path: Path | None = None,
    ) -> None:
        self._fsops = fsops
        self.instance_id = instance_id
        self._clock = clock
        self._lock_path = lock_path

    def read_lease(self, root_path: str) -> Lease | None:
        raw = self._fsops.read_lease(root_path)
        return None if raw is None else Lease.parse(raw)

    def _own(self) -> Lease:
        return Lease(
            self.instance_id,
            self._clock.now(),
            str(self._lock_path) if self._lock_path else None,
        )

    def expired(self, lease: Lease) -> bool:
        return self._clock.now() - lease.renewed_at >= LEASE_VALIDITY

    def _root_of(self, local: str) -> str:
        status = self._fsops.status(local)
        if status.read_only:
            raise ReadOnlyRootError(f"{status.root_path} is an adoption root: never written")
        return status.root_path

    def take(self, root_path: str) -> None:
        def decide(raw: bytes | None) -> bytes | Literal["remove"] | None:
            if raw is not None:
                current = Lease.parse(raw)
                if current.instance_id != self.instance_id:
                    if not self.expired(current):
                        raise LeaseConflictError(
                            f"{root_path} is leased by instance {current.instance_id}"
                        )
                    if current.lock_path and holder_is_alive(Path(current.lock_path)):
                        raise LeaseConflictError(
                            f"{root_path}: the lease ran out but instance {current.instance_id} "
                            "still holds its lock"
                        )
            return self._own().dumps()

        self._fsops.update_lease(root_path, decide)
        confirmed = self.read_lease(root_path)  # a racing taker may have written after us
        if confirmed is None or confirmed.instance_id != self.instance_id:
            raise LeaseConflictError(f"{root_path}: lost the race for the lease")

    def renew(self, root_path: str) -> None:
        def decide(raw: bytes | None) -> bytes | Literal["remove"] | None:
            current = None if raw is None else Lease.parse(raw)
            if current is None or current.instance_id != self.instance_id:
                raise LeaseLostError(f"{root_path}: the lease is no longer ours")
            if self.expired(current):
                raise LeaseLostError(f"{root_path}: the lease ran out")
            return self._own().dumps()

        self._fsops.update_lease(root_path, decide)

    def check(self, root_path: str) -> None:
        """Before each write: still ours and valid; renewed when the heartbeat is due."""
        current = self.read_lease(root_path)
        if current is None or current.instance_id != self.instance_id or self.expired(current):
            raise LeaseLostError(f"{root_path}: the write lease is lost")
        if self._clock.now() - current.renewed_at >= HEARTBEAT_INTERVAL:
            self.renew(root_path)

    def release(self, root_path: str) -> None:
        def decide(raw: bytes | None) -> bytes | Literal["remove"] | None:
            if raw is None:
                return None
            try:
                mine = Lease.parse(raw).instance_id == self.instance_id
            except LeaseConflictError:
                return None
            return "remove" if mine else None

        with suppress(FsopsError, OSError):  # a root that went away: the lease simply runs out
            self._fsops.update_lease(root_path, decide)

    @contextmanager
    def open(self, *paths: str) -> Iterator["WriteSession"]:
        roots = sorted({self._root_of(path) for path in paths})
        taken: list[str] = []
        try:
            for root in roots:
                self.take(root)
                taken.append(root)
            yield WriteSession(self, tuple(roots))
        finally:
            for root in taken:
                self.release(root)


class WriteSession:
    """Writes, each preceded by the lease check of the root(s) it touches."""

    def __init__(self, sessions: WriteSessions, roots: tuple[str, ...]) -> None:
        self._sessions = sessions
        self._roots = roots
        self._fsops = sessions._fsops

    def _guard(self, *paths: str) -> None:
        for root in sorted({self._sessions._root_of(path) for path in paths}):
            if root not in self._roots:
                raise LeaseLostError(f"{root} was not opened for this write session")
            self._sessions.check(root)

    def heartbeat(self) -> None:
        for root in self._roots:
            self._sessions.renew(root)

    def write_atomic(self, local: str, data: bytes, **options: object) -> FileIdentity:
        self._guard(local)
        return self._fsops.write_atomic(local, data, **options)  # type: ignore[arg-type]

    def mkdir(self, local: str, *, parents: bool = True) -> None:
        self._guard(local)
        self._fsops.mkdir(local, parents=parents)

    def unlink(self, local: str) -> None:
        self._guard(local)
        self._fsops.unlink(local)

    def rmdir(self, local: str) -> None:
        self._guard(local)
        self._fsops.rmdir(local)

    def rename(self, src: str, dst: str, *, replace: bool = False) -> None:
        self._guard(src, dst)
        self._fsops.rename(src, dst, replace=replace)

    def recycle(self, local: str) -> str:
        self._guard(local)
        return self._fsops.recycle(local)

    def copy_across(
        self, src: str, dst: str, *, parents: bool = False, replace: bool = False
    ) -> str:
        self._guard(dst)
        return self._fsops.copy_across(src, dst, parents=parents, replace=replace)
