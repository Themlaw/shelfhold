"""The full detachment of a Kavita target: Shelfhold lets go, Kavita keeps working on its own tree
(M3 L30, EF-EXP-031; the lighter `detach --restore-upstream` of EF-EXP-056 stays as it is).

What it does, in this order (each step is safe to run again, so a stop anywhere is finished by the
same command):

1. **the hidden files** of each root the target lives in: the reserved folder `.shelfhold/` and
   the sentinel, in one journaled intention per root, then the root is demoted to read-only (as
   `forget-root` does), so Shelfhold neither writes it nor expects its sentinel any more. A root
   that another target still uses is left alone and said;
2. **the Kavita settings**: `ExcludePatterns` as they were before the adoption (Shelfhold's own
   pattern goes, the admin's own stay), the library type read back (`settings_restore`);
3. **the dedicated key** is revoked in Kavita through the account's password;
4. **the manifest** of every destination is forgotten and the target with it. The exported files
   are not touched: the tree is the admin's, whole, and Kavita opens it as it is.

The order is the safety: the hidden folder goes first (while Kavita still excludes it, nothing
is indexed twice), the settings next (Kavita needs the key to be told), the key last. If a step
fails the later ones are not run, the report says so, and the manifest stays until the key is
revoked.

Never destroyed behind the admin's back: what `.shelfhold/` holds that is user data. A bin entry
is a deleted book still recoverable and a received archive is a book not yet exported; either one
stops the detachment (`hidden_data_present`) until `discard_hidden_data` is said by name.

A reserved action (`detach.full`): a fresh admin session or the host CLI, never a key, a member or
a child, with the phrase `detach <target id>` typed. The final report lists what stays: the
downloads in Suwayomi, the Kavita accounts, `/store`.
"""

import asyncio
import hmac
import os
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.export.application.intents import (
    IntentJournal,
    OpenIntent,
    holds_any,
    open_intent_paths,
    within_any,
)
from shelfhold.export.application.locks import LockBusyError, ResourceLocks, path_resource
from shelfhold.export.application.remove_destination import forget_destination
from shelfhold.export.application.snapshot import DestinationContext, load_context
from shelfhold.export.domain.path_map import PathMode, within
from shelfhold.export.domain.physical_roots import (
    RECEIVED_DIR,
    RECYCLE_DIR,
    RESERVED_DIR,
    SENTINEL_FILE,
    TOMBSTONE_JOURNAL,
)
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.settings_restore import (
    SettingsRestored,
    restore_library_settings,
)
from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    Allowed,
    AuditEntry,
    Principal,
    audit_insert,
    authorize_reserved,
)
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fsops import Fsops, FsopsError
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, export, platform

__all__ = [
    "DETACH_SCOPE",
    "DetachPreview",
    "DetachRefusedError",
    "DetachReport",
    "DetachWarning",
    "FullDetacher",
    "KeyRevoker",
    "Leftover",
    "RootRef",
    "detach_phrase",
]

log = structlog.get_logger()

DETACH_SCOPE = "hidden_area"  # what the intention of the erasure of a root says about itself
_ACTION = "detach.full"


def detach_phrase(target_id: str) -> str:
    """What the admin types: the id, so that a pasted command names its target."""
    return f"detach {target_id}"


@dataclass(frozen=True)
class RootRef:
    """A physical root: its id (the database's) and its path on this machine."""

    root_id: str
    path: str


RootDirectory = Callable[[str], RootRef | None]  # a local path -> the physical root holding it


class KeyRevoker(Protocol):
    """Revokes the dedicated key in Kavita with the account's password (the one door that needs
    it, like `target rotate-key`). True when Kavita no longer knows the key."""

    def revoke(self, target_id: str, *, credential: str | None) -> bool: ...


class DetachRefusedError(PermissionError):
    """Nothing was changed. `reason`: `not_allowed`, `confirmation_required`, `intent_open` (an
    open file operation lies under a root, I9), `hidden_data_present` (a bin entry or an archive
    waiting for its export would be destroyed), `busy`, `unknown_target`."""

    def __init__(self, reason: str, *, verdict: object | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.verdict = verdict


@dataclass(frozen=True)
class DetachWarning:
    code: str
    count: int = 0


@dataclass(frozen=True)
class DetachPreview:
    target_id: str
    warnings: tuple[DetachWarning, ...]
    phrase: str


@dataclass(frozen=True)
class Leftover:
    """What stays after the detachment, or what could not be done. `count` is a number of
    objects, never a name or a path."""

    code: str
    count: int = 0


@dataclass
class DetachReport:
    target_id: str
    hidden_removed: tuple[str, ...] = ()  # the roots whose reserved folder went
    settings: tuple[SettingsRestored, ...] = ()
    key_revoked: bool = False
    forgotten: int = 0  # manifest lines
    cancelled: int = 0  # intentions given up by `settle(resume=False)`
    leftovers: tuple[Leftover, ...] = field(default_factory=tuple)
    complete: bool = False

    @property
    def settings_restored(self) -> SettingsRestored | None:
        return self.settings[0] if self.settings else None


# What blocks the end, in the report: these codes make the detachment incomplete.
_INCOMPLETE = frozenset({"hidden_not_erased", "settings_not_restored", "key_not_revoked"})


class FullDetacher:
    def __init__(
        self,
        engine: Engine,
        fsops: Fsops,
        journal: IntentJournal,
        *,
        clock: Clock,
        roots: RootDirectory,
        locks: ResourceLocks | None = None,
        store_path: str | None = None,
        run: Callable[[Coroutine[Any, Any, Any]], Any] = asyncio.run,
        fresh_auth_s: int = 900,
    ) -> None:
        self._engine = engine
        self._fsops = fsops
        self._journal = journal
        self._clock = clock
        self._roots = roots
        self._locks = locks
        self._store = store_path
        self._run = run
        self._fresh_auth_s = fresh_auth_s

    # -- the warning ----------------------------------------------------------------------------

    def preview(self, target_id: str) -> DetachPreview:
        """What the detachment would do and what it would lose; nothing is written."""
        with self._engine.connect() as conn:
            contexts = self._contexts(conn, target_id)
            files = sum(1 for c in contexts for r in _records(conn, c.destination_id) if r.rel_path)
            following = _follows(conn, contexts, active_only=True)
        erase, shared = self._split_roots(target_id, contexts)
        data = sum(self._hidden_data(r) for r in erase)
        warnings = [
            DetachWarning("files_kept", files),
            DetachWarning("hidden_erased", len(erase)),
            DetachWarning("key_revoked", 1),
            DetachWarning("manifest_forgotten", files),
        ]
        if data:
            warnings.append(DetachWarning("hidden_data_present", data))
        if shared:
            warnings.append(DetachWarning("root_shared", len(shared)))
        if following:
            warnings.append(DetachWarning("follows_active", following))
        if any(self._has_tombstone_journal(r) for r in erase):
            warnings.append(DetachWarning("tombstone_journal_erased", 1))
        return DetachPreview(target_id, tuple(warnings), detach_phrase(target_id))

    # -- the detachment -------------------------------------------------------------------------

    def apply(
        self,
        target_id: str,
        *,
        principal: Principal,
        typed: str | None,
        client: KavitaClient | None,
        revoker: KeyRevoker,
        credential: str | None = None,
        discard_hidden_data: bool = False,
        job_id: str | None = None,
    ) -> DetachReport:
        self._authorize(principal)
        if typed is None or not hmac.compare_digest(
            typed.strip().encode(), detach_phrase(target_id).encode()
        ):
            raise DetachRefusedError("confirmation_required")
        with self._engine.connect() as conn:
            contexts = self._contexts(conn, target_id)
        erase, shared = self._split_roots(target_id, contexts)
        report = DetachReport(target_id)
        with self._held(erase, job_id or new_ulid()):
            self._settle_own(target_id, report, resume=True)
            self._require_free(erase)
            if not discard_hidden_data and any(self._hidden_data(r) for r in erase):
                raise DetachRefusedError("hidden_data_present")
            left: list[Leftover] = []
            self._erase_hidden(target_id, erase, report, left)
            if not any(leftover.code in _INCOMPLETE for leftover in left):
                self._restore_settings(contexts, client, report, left)
            if not any(leftover.code in _INCOMPLETE for leftover in left):
                self._revoke(target_id, revoker, credential, report, left)
            report.complete = not any(leftover.code in _INCOMPLETE for leftover in left)
            if report.complete:
                report.forgotten = self._forget(principal, target_id, contexts)
            left.extend(self._leftovers(contexts, shared))
            report.leftovers = tuple(left)
        log.info(
            "detach.done", target_id=target_id, complete=report.complete,
            roots=len(report.hidden_removed), forgotten=report.forgotten,
        )  # fmt: skip
        return report

    def settle(self, target_id: str, *, resume: bool, principal: Principal) -> DetachReport:
        """After a stop. `resume`: close what the stop left finished (the erasure that did reach
        the disk), leave the rest to the same command run again. Otherwise give up: the
        intentions whose erasure did not happen are cancelled and nothing else changes."""
        self._authorize(principal)
        report = DetachReport(target_id)
        self._settle_own(target_id, report, resume=resume)
        return report

    # -- the steps ------------------------------------------------------------------------------

    def _erase_hidden(
        self, target_id: str, roots: list[RootRef], report: DetachReport, left: list[Leftover]
    ) -> None:
        done: list[str] = []
        for root in roots:
            if self._erased(root):
                self._demote(root.root_id)
                continue
            area = f"{root.path}/{RESERVED_DIR}"
            intent = self._journal.record(
                op="delete", src=area,
                extra={"scope": DETACH_SCOPE, "target_id": target_id, "root_id": root.root_id},
            )  # fmt: skip
            self._journal.mark_applying(intent)
            try:
                self._fsops.erase_host_area(root.path)
            except (OSError, FsopsError):
                self._journal.cancel(intent)
                left.append(Leftover("hidden_not_erased", 1))
                continue
            self._close_erased(intent, root.root_id)
            done.append(root.path)
        report.hidden_removed = tuple(done)

    def _restore_settings(
        self,
        contexts: list[DestinationContext],
        client: KavitaClient | None,
        report: DetachReport,
        left: list[Leftover],
    ) -> None:
        restored: list[SettingsRestored] = []
        seen: set[str] = set()
        with self._engine.connect() as conn:
            before = {
                row.id: row.pre_adoption_settings
                for row in conn.execute(
                    sa.select(
                        export.shelf_destination.c.id,
                        export.shelf_destination.c.pre_adoption_settings,
                    ).where(export.shelf_destination.c.id.in_([c.destination_id for c in contexts]))
                )
            }
            refs = {c.destination_id: _remote_ref(conn, c.remote_library_id) for c in contexts}
        if client is None:
            left.append(Leftover("settings_not_restored", len(contexts)))
            return
        for ctx in contexts:
            ref = refs[ctx.destination_id]
            if ref is None or ref in seen:
                continue
            seen.add(ref)
            try:
                restored.append(
                    self._run(restore_library_settings(client, ref, before[ctx.destination_id]))
                )
            except Exception as error:  # Kavita unreachable, library gone: said, never hidden
                log.warning("detach.settings_failed", error=type(error).__name__)
                left.append(Leftover("settings_not_restored", 1))
        report.settings = tuple(restored)
        if any(not s.type_unchanged for s in restored):  # told, not forced
            left.append(
                Leftover("library_type_changed", sum(not s.type_unchanged for s in restored))
            )

    def _revoke(
        self,
        target_id: str,
        revoker: KeyRevoker,
        credential: str | None,
        report: DetachReport,
        left: list[Leftover],
    ) -> None:
        try:
            revoked = revoker.revoke(target_id, credential=credential)
        except Exception as error:  # the account's door failed: the key stays, and it is said
            log.warning("detach.revoke_failed", error=type(error).__name__)
            revoked = False
        report.key_revoked = revoked
        if not revoked:
            left.append(Leftover("key_not_revoked", 1))

    def _forget(
        self, principal: Principal, target_id: str, contexts: list[DestinationContext]
    ) -> int:
        forgotten = 0
        with self._engine.begin() as conn:
            for ctx in contexts:
                forgotten += forget_destination(conn, ctx.destination_id)
            libraries = export.remote_library
            conn.execute(sa.delete(export.kavita_instance).where(
                export.kavita_instance.c.target_id == target_id
            ))  # fmt: skip
            conn.execute(sa.delete(libraries).where(libraries.c.export_target_id == target_id))
            conn.execute(
                sa.delete(export.export_target).where(export.export_target.c.id == target_id)
            )
            conn.execute(
                audit_insert(
                    AuditEntry(
                        actor_id=principal.actor_id,
                        action=_ACTION,
                        at=self._clock.now(),
                        target=target_id,
                        session_id=principal.session_id,
                        auth_time=principal.auth_time,
                    )
                )
            )
        return forgotten

    # -- after a stop ---------------------------------------------------------------------------

    def _settle_own(self, target_id: str, report: DetachReport, *, resume: bool) -> None:
        """The erasure intentions of this target a stop left open: an erasure that did reach the
        disk is closed (and its root demoted), one that did not is given up, to be redone fresh
        by `apply` (`resume`) or not at all (`resume=False`)."""
        for intent in self._journal.open_intents():
            if not _ours(intent, target_id) or intent.src is None:
                continue
            root = str(intent.src).removesuffix(f"/{RESERVED_DIR}")
            if self._erased(RootRef("", root)):
                self._close_erased(intent.id, str(intent.payload.get("root_id", "")))
            else:
                self._journal.cancel(intent.id)
                if not resume:
                    report.cancelled += 1

    def _close_erased(self, intent_id: str, root_id: str) -> None:
        with self._engine.begin() as conn:
            self._journal.mark_done(intent_id, within=conn)
            if root_id:
                _demote_row(conn, root_id)

    def _demote(self, root_id: str) -> None:
        if root_id:
            with self._engine.begin() as conn:
                _demote_row(conn, root_id)

    # -- the checks -----------------------------------------------------------------------------

    def _authorize(self, principal: Principal) -> None:
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS[_ACTION], now=self._clock.now(),
            fresh_auth_s=self._fresh_auth_s,
        )  # fmt: skip
        if not isinstance(verdict, Allowed):
            raise DetachRefusedError("not_allowed", verdict=verdict)

    @staticmethod
    def _contexts(conn: Connection, target_id: str) -> list[DestinationContext]:
        found = conn.execute(
            sa.select(export.export_target.c.id).where(export.export_target.c.id == target_id)
        ).scalar()
        if found is None:
            raise DetachRefusedError("unknown_target")
        d = export.shelf_destination
        ids = conn.execute(sa.select(d.c.id).where(d.c.target_id == target_id).order_by(d.c.id))
        return [load_context(conn, destination) for destination in ids.scalars()]

    def _split_roots(
        self, target_id: str, contexts: list[DestinationContext]
    ) -> tuple[list[RootRef], list[RootRef]]:
        """The roots to erase and the roots another target still uses (left alone, said)."""
        found: dict[str, RootRef] = {}
        for ctx in contexts:
            for entry in ctx.path_map.entries:
                if entry.mode is PathMode.RW and (ref := self._roots(entry.local)) is not None:
                    found[ref.root_id] = ref
        with self._engine.connect() as conn:
            others = [
                list(row.path_map or [])
                for row in conn.execute(
                    sa.select(export.export_target.c.path_map).where(
                        export.export_target.c.id != target_id
                    )
                ).all()
            ]
        entries = [str(e.get("local", "")) for rows in others for e in rows if isinstance(e, dict)]
        erase: list[RootRef] = []
        shared: list[RootRef] = []
        for ref in sorted(found.values(), key=lambda r: r.path):
            clash = any(
                within(local, ref.path) is not None or within(ref.path, local) is not None
                for local in entries
                if local
            )
            (shared if clash else erase).append(ref)
        return erase, shared

    def _require_free(self, roots: list[RootRef]) -> None:
        with self._engine.connect() as conn:
            bases = open_intent_paths(conn)
        for root in roots:
            if within_any(root.path, bases) or holds_any(root.path, bases):
                raise DetachRefusedError("intent_open")

    def _erased(self, root: RootRef) -> bool:
        """Nothing of the reserved folder and the sentinel is left at the top of the root."""
        for name in (RESERVED_DIR, SENTINEL_FILE):
            try:
                if self._fsops.observe(f"{root.path}/{name}", host=True) is not None:
                    return False
            except FsopsError:
                return False
        return True

    def _hidden_data(self, root: RootRef) -> int:
        """Entries in the bin and archives waiting for their export: user data, never silently
        destroyed."""
        total = 0
        for folder in (RECYCLE_DIR, RECEIVED_DIR):
            try:
                total += len(self._fsops.list_dir(f"{root.path}/{folder}", host=True))
            except (FsopsError, OSError):
                continue
        return total

    def _has_tombstone_journal(self, root: RootRef) -> bool:
        try:
            return self._fsops.observe(f"{root.path}/{TOMBSTONE_JOURNAL}", host=True) is not None
        except FsopsError:
            return False

    @contextmanager
    def _held(self, roots: list[RootRef], job_id: str) -> Iterator[None]:
        """The roots are held for the length of the detachment (no export writes them), released
        whatever happens; a lease that a kill left expires by itself."""
        if self._locks is None or not roots:
            yield
            return
        try:
            held = self._locks.hold_resources(job_id, sorted(path_resource(r.path) for r in roots))
            held.__enter__()
        except LockBusyError:
            raise DetachRefusedError("busy") from None
        try:
            yield
        finally:
            held.__exit__(None, None, None)

    # -- the report -----------------------------------------------------------------------------

    def _leftovers(
        self, contexts: list[DestinationContext], shared: list[RootRef]
    ) -> list[Leftover]:
        with self._engine.connect() as conn:
            downloads = _follows(conn, contexts, active_only=False)
        out = [
            Leftover("suwayomi_downloads", downloads),  # the files in Suwayomi are its own
            Leftover("kavita_accounts", 1),  # the dedicated account, and the ones the admin made
            Leftover("store", self._store_files()),  # /store is Shelfhold's, and stays
        ]
        if shared:
            out.append(Leftover("root_shared", len(shared)))
        return out

    def _store_files(self) -> int:
        """How many files /store holds (its reserved folder and sentinel are not counted)."""
        if not self._store or not Path(self._store).is_dir():
            return 0
        total = 0
        for _base, dirs, names in os.walk(self._store):
            dirs[:] = [d for d in dirs if d != RESERVED_DIR]
            total += len([n for n in names if not n.startswith(".shelfhold")])
        return total


def _demote_row(conn: Connection, root_id: str) -> None:
    """The root keeps its copies on record but is no longer written or expected stamped."""
    table = platform.physical_root
    conn.execute(sa.update(table).where(table.c.root_id == root_id).values(mode="adopt_ro"))


def _ours(intent: OpenIntent, target_id: str) -> bool:
    return (
        intent.op == "delete"
        and intent.payload.get("scope") == DETACH_SCOPE
        and intent.payload.get("target_id") == target_id
    )


def _records(conn: Connection, destination_id: str) -> list[Any]:
    record = export.export_record
    return list(
        conn.execute(sa.select(record).where(record.c.shelf_destination_id == destination_id))
    )


def _remote_ref(conn: Connection, remote_library_id: str | None) -> str | None:
    if remote_library_id is None:
        return None
    library = export.remote_library
    return conn.execute(
        sa.select(library.c.remote_ref).where(library.c.id == remote_library_id)
    ).scalar()


def _follows(conn: Connection, contexts: list[DestinationContext], *, active_only: bool) -> int:
    """The series Shelfhold follows for the shelves of these destinations."""
    shelves = [c.shelf_id for c in contexts]
    if not shelves:
        return 0
    binding, edition = acquisition.source_binding, catalog.edition
    stmt = (
        sa.select(sa.func.count(sa.distinct(binding.c.id)))
        .select_from(binding.join(edition, edition.c.id == binding.c.edition_id))
        .where(edition.c.shelf_id.in_(shelves))
    )
    if active_only:
        stmt = stmt.where(binding.c.active.is_(True))
    return int(conn.execute(stmt).scalar_one())
