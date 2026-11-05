"""Per-root write leases (ENF-OPS-015, 02 section 10.2).

A lease lives in `<root>/.shelfhold/lease` and exists only while a write session is open on that
root: taken at the opening, renewed by a heartbeat during the batch, checked before every write and
released at the end. Outside a session nothing is written under the library, which lets the disks
sleep. The file is read and written through `Fsops` (the only writer) on the root that was just
judged against its sentinel and generation counter, by **compare-and-swap** (the old lease is moved
aside atomically and must be the one that was judged; the new one is created exclusively), so two
takers can never both believe they hold it.

`WriteSessions.enforce()` plugs the check into `Fsops` itself: from then on *every* write, whoever
issues it (session, intent journal, replay at boot), is refused without our valid lease.

Taking over the lease of another instance needs all of: its validity has run out (a lease dated
further in the future than the allowed clock drift counts as run out and is logged), the holder's
`flock` is no longer held, and the sentinel and generation read in the same open root are sound.
"""

import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import structlog

from shelfhold.export.domain.physical_roots import RootState
from shelfhold.infrastructure.clock import DEFAULT_SKEW, Clock
from shelfhold.infrastructure.fsops import (
    Fsops,
    FsopsError,
    LeaseRaceError,
    ReadOnlyRootError,
    RootBlockedError,
)
from shelfhold.infrastructure.fsops.identity import FileIdentity
from shelfhold.infrastructure.instance.errors import LeaseConflictError, LeaseLostError
from shelfhold.infrastructure.instance.generation import GenerationCounter
from shelfhold.infrastructure.instance.lock import holder_is_alive

LEASE_VALIDITY = timedelta(minutes=3)
HEARTBEAT_INTERVAL = timedelta(seconds=30)


@dataclass(frozen=True)
class Lease:
    instance_id: str
    boot_id: str  # one value per process start: tells two runs of one instance apart
    renewed_at: datetime
    token: str  # changes at every write: the compare-and-swap identity
    lock_path: str | None = (
        None  # the holder's instance lock, to tell a live holder from a dead one
    )

    def dumps(self) -> bytes:
        return json.dumps(
            {
                "instance_id": self.instance_id,
                "boot_id": self.boot_id,
                "renewed_at": self.renewed_at.isoformat(),
                "token": self.token,
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
                str(doc["boot_id"]),
                datetime.fromisoformat(doc["renewed_at"]),
                str(doc["token"]),
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
        boot_id: str | None = None,
        generations: GenerationCounter | None = None,
        on_behind: Callable[[], None] | None = None,
    ) -> None:
        self._generations = generations
        self._on_behind = on_behind  # a root remounted ahead of the database (EF-INT-026)
        self._fsops = fsops
        self.instance_id = instance_id
        self.boot_id = boot_id or uuid.uuid4().hex
        self._clock = clock
        self._lock_path = lock_path

    def enforce(self) -> None:
        """Make `Fsops` refuse every write without our valid lease on the root (ENF-OPS-015)."""
        self._fsops.enforce_lease(self._verify)

    def _verify(self, root_path: str, raw: bytes | None) -> None:
        if raw is None:
            raise LeaseLostError(f"{root_path}: no write session is open on this root")
        current = Lease.parse(raw)
        if not self._is_mine(current) or self.expired(current):
            raise LeaseLostError(f"{root_path}: the write lease is lost")

    def _is_mine(self, lease: Lease) -> bool:
        return lease.instance_id == self.instance_id and lease.boot_id == self.boot_id

    def read_lease(self, root_path: str) -> Lease | None:
        raw = self._fsops.read_lease(root_path)
        return None if raw is None else Lease.parse(raw)

    def _own(self) -> Lease:
        return Lease(
            self.instance_id,
            self.boot_id,
            self._clock.now(),
            uuid.uuid4().hex,
            str(self._lock_path) if self._lock_path else None,
        )

    def expired(self, lease: Lease) -> bool:
        """Run out, or dated beyond the allowed drift in the future (a holder whose clock is ahead
        must not keep a root for ever): the latter is logged."""
        age = self._clock.now() - lease.renewed_at
        if age < -DEFAULT_SKEW:
            structlog.get_logger().warning(
                "lease_dated_in_the_future", instance_id=lease.instance_id, ahead=str(-age)
            )
            return True
        return age >= LEASE_VALIDITY

    def _root_of(self, local: str) -> str:
        status = self._fsops.status(local)
        if status.read_only:
            raise ReadOnlyRootError(f"{status.root_path} is an adoption root: never written")
        return status.root_path

    def take(self, root_path: str) -> None:
        def decide(raw: bytes | None) -> bytes | Literal["remove"] | None:
            if raw is not None:
                current = Lease.parse(raw)
                if not self._is_mine(current) and not self._previous_run_of_ours(current):
                    if not self.expired(current):
                        raise LeaseConflictError(
                            f"{root_path} is leased by instance {current.instance_id}"
                        )
                    if self._holder_alive(current):
                        raise LeaseConflictError(
                            f"{root_path}: the lease ran out but instance {current.instance_id} "
                            "still holds its lock"
                        )
            return self._own().dumps()

        try:
            self._fsops.update_lease(root_path, decide)
        except LeaseRaceError as error:
            raise LeaseConflictError(f"{root_path}: lost the race for the lease") from error

    def _previous_run_of_ours(self, lease: Lease) -> bool:
        """Same instance, another start: it is dead since we hold the instance lock of this /data
        (only claimed when the caller says it holds that lock by giving its path)."""
        return (
            lease.instance_id == self.instance_id
            and lease.boot_id != self.boot_id
            and self._lock_path is not None
        )

    def _holder_alive(self, lease: Lease) -> bool:
        if not lease.lock_path or (self._lock_path and Path(lease.lock_path) == self._lock_path):
            return False  # our own lock says nothing about the holder: only the expiry counts
        return holder_is_alive(Path(lease.lock_path))

    def renew(self, root_path: str) -> None:
        def decide(raw: bytes | None) -> bytes | Literal["remove"] | None:
            current = None if raw is None else Lease.parse(raw)
            if current is None or not self._is_mine(current):
                raise LeaseLostError(f"{root_path}: the lease is no longer ours")
            if self.expired(current):
                raise LeaseLostError(f"{root_path}: the lease ran out")
            return self._own().dumps()

        try:
            self._fsops.update_lease(root_path, decide)
        except LeaseRaceError as error:
            raise LeaseLostError(f"{root_path}: the lease changed under us") from error

    def check(self, root_path: str) -> None:
        """Before each write: still ours and valid; renewed when the heartbeat is due."""
        current = self.read_lease(root_path)
        if current is None or not self._is_mine(current) or self.expired(current):
            raise LeaseLostError(f"{root_path}: the write lease is lost")
        if self._clock.now() - current.renewed_at >= HEARTBEAT_INTERVAL:
            self.renew(root_path)

    def release(self, root_path: str) -> None:
        def decide(raw: bytes | None) -> bytes | Literal["remove"] | None:
            if raw is None:
                return None
            try:
                mine = self._is_mine(Lease.parse(raw))
            except LeaseConflictError:
                return None
            return "remove" if mine else None

        with suppress(FsopsError, OSError):  # a root that went away: the lease simply runs out
            self._fsops.update_lease(root_path, decide)

    def _advance_generations(self, roots: list[str]) -> None:
        """Once per session and root: the database first, the sentinel after (I39)."""
        if self._generations is None:
            return
        for root in roots:
            self._fsops.require_stampable(root)  # a refusal here must not move the database
            seen = self._fsops.sentinel_of(root)
            current = self._generations.current(root)
            if current is None:
                continue
            if seen is not None and current == seen.generation + 1:
                moved = current  # one ahead already (an earlier stop): re-stamp, do not advance
            else:
                advanced = self._generations.advance(root)
                if advanced is None:
                    continue
                moved = advanced
            self._fsops.stamp_sentinel(root, generation=moved)

    @contextmanager
    def open(
        self, *paths: str, guard: Callable[[], None] | None = None
    ) -> Iterator["WriteSession"]:
        roots = sorted({self._root_of(path) for path in paths})
        taken: list[str] = []
        try:
            for root in roots:
                try:
                    self.take(root)
                except RootBlockedError as blocked:
                    # a root that was away at start comes back ahead of the database: detect it
                    # now, not at the next start
                    if blocked.state is RootState.RESTORED_BEHIND and self._on_behind is not None:
                        self._on_behind()
                    raise
                taken.append(root)
            self._advance_generations(taken)
            yield WriteSession(self, tuple(roots), guard)
        finally:
            for root in taken:
                self.release(root)


class WriteSession:
    """Writes, each preceded by the lease check of the root(s) it touches."""

    def __init__(
        self,
        sessions: WriteSessions,
        roots: tuple[str, ...],
        guard: Callable[[], None] | None = None,
    ) -> None:
        self._sessions = sessions
        self._roots = roots
        self._fsops: Fsops = sessions._fsops
        self._alive = guard  # e.g. `ResourceLocks` handle: a lost resource lock stops the writes

    def _guard(self, *paths: str) -> None:
        if self._alive is not None:
            self._alive()
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

    def write_mirror(self, root_path: str, name: str, data: bytes) -> FileIdentity:
        self._guard(root_path)
        return self._fsops.write_mirror(root_path, name, data)

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
