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
import contextlib
import hmac
import os
from collections.abc import Callable, Collection, Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.export.application.intents import (
    IntentJournal,
    OpenIntent,
    holds_any,
    within_any,
)
from shelfhold.export.application.library_settings import (
    LibraryGoneError,
    RestoreSettings,
    SettingsRestored,
)
from shelfhold.export.application.locks import (
    LockBusyError,
    ResourceLocks,
    dest_resource,
)
from shelfhold.export.application.remove_destination import forget_destination
from shelfhold.export.application.root_ref import RootDirectory, RootRef, copy_scope
from shelfhold.export.application.snapshot import DestinationContext, load_context
from shelfhold.export.domain.path_map import PathMode, within
from shelfhold.export.domain.physical_roots import (
    ERASING_DIR,
    RECEIVED_DIR,
    RECYCLE_DIR,
    RESERVED_DIR,
    SENTINEL_FILE,
    TOMBSTONE_JOURNAL,
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
from shelfhold.infrastructure.schema import acquisition, catalog, coherence, export, platform

__all__ = [
    "DETACH_SCOPE",
    "DetachPreview",
    "DetachRefusedError",
    "DetachReport",
    "DetachWarning",
    "FullDetacher",
    "KeyIdRevoker",
    "KeyRevoker",
    "Leftover",
    "RootRef",
    "full_detach_phrase",
]

log = structlog.get_logger()

DETACH_SCOPE = "hidden_area"  # what the intention of the erasure of a root says about itself
REVOKE_SCOPE = "revoke_key"  # the intention of the revocation of the dedicated key
_ACTION = "detach.full"


def full_detach_phrase(target_id: str) -> str:
    """What the admin types: the id, so that a pasted command names its target."""
    return f"detach {target_id}"


class KeyRevoker(Protocol):
    """Revokes the dedicated key in Kavita with the account's password (the one door that needs
    it, like `target rotate-key`). True when Kavita no longer knows the key."""

    def revoke(self, target_id: str, *, credential: str | None) -> bool: ...


@runtime_checkable
class KeyIdRevoker(KeyRevoker, Protocol):
    """The door that can name the key. The detachment writes the **identifier** of the key (never
    its name, never its value) in its intention before the key goes, and revokes by that
    identifier: a rerun after a stop finds the key gone and the identifier on record, which is a
    revocation already done; and another key of the account is never touched."""

    def key_id(self, target_id: str, *, credential: str | None) -> tuple[str, str] | None:
        """`(identifier, fingerprint of the value)` of the dedicated key, or None when the account
        has no such key. A refused login or an unreachable Kavita raises: it is not "absent"."""
        ...

    def revoke_id(
        self, target_id: str, *, credential: str | None, key_id: str, key_digest: str | None = None
    ) -> str:
        """Deletes that identifier and no other: `revoked` (gone, or already gone), `reused` (the
        identifier now names another key, whose value is not the one sealed: left alone) or
        `failed`."""
        ...


class DetachRefusedError(PermissionError):
    """Nothing was changed. `reason`: `not_allowed`, `confirmation_required`, `intent_open` (an
    open file operation lies under a root, I9), `hidden_data_present` (a bin entry or an archive
    waiting for its export would be destroyed), `tombstone_journal_present` (the erasure journal
    would go: it takes `discard_tombstone_journal`), `busy` (an export, a scan or another job holds
    a destination of the target), `credential_required` (`key_gone` without the account's
    password), `unknown_target`, `past_no_return` (`give up` after an erasure
    began: run the command again to finish)."""

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
    key_gone: bool = (
        False  # `--key-gone`: the key was not there, the account gave the settings back
    )
    forgotten: int = 0  # manifest lines
    cancelled: int = 0  # intentions given up by `settle(resume=False)`
    past_no_return: bool = False  # `settle(resume=False)` after an erasure began: nothing undone
    leftovers: tuple[Leftover, ...] = field(default_factory=tuple)
    complete: bool = False


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
        restore: RestoreSettings,
        library_roots: Callable[[], Collection[str]],
        locks: ResourceLocks | None = None,
        store_path: str | None = None,
        run: Callable[[Coroutine[Any, Any, Any]], Any] = asyncio.run,
        fresh_auth_s: int = 900,
    ) -> None:
        self._engine = engine
        self._restore = restore
        self._library_roots = library_roots  # the roots Shelfhold's own library lives on
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
        if journals := sum(self._has_tombstone_journal(r) for r in erase):
            warnings.append(DetachWarning("tombstone_journal_erased", journals))
        return DetachPreview(target_id, tuple(warnings), full_detach_phrase(target_id))

    # -- the detachment -------------------------------------------------------------------------

    def apply(
        self,
        target_id: str,
        *,
        principal: Principal,
        typed: str | None,
        client: object | None,
        revoker: KeyRevoker,
        credential: str | None = None,
        discard_hidden_data: bool = False,
        discard_tombstone_journal: bool = False,
        key_gone: bool = False,
        account: object | None = None,
        job_id: str | None = None,
    ) -> DetachReport:
        """`key_gone`: the dedicated key no longer exists (deleted by hand, 401): the settings are
        given back through `account` (a session of the admin account, not the dead key) and the
        revocation is skipped when the account confirms no such key is there. A key that is still
        there is revoked as usual: the flag never leaves a live key behind."""
        self._authorize(principal)
        if typed is None or not hmac.compare_digest(
            typed.strip().encode(), full_detach_phrase(target_id).encode()
        ):
            raise DetachRefusedError("confirmation_required")
        if key_gone and not credential:
            raise DetachRefusedError("credential_required")
        report = DetachReport(target_id)
        with self._held(target_id, job_id or new_ulid()):
            with self._engine.connect() as conn:  # read once the target is ours
                contexts = self._contexts(conn, target_id)
            erase, shared = self._split_roots(target_id, contexts)
            self._settle_own(target_id, report, resume=True)
            self._require_free(target_id, erase)
            if not discard_hidden_data and any(self._hidden_data(r) for r in erase):
                raise DetachRefusedError("hidden_data_present")
            journals = [r for r in erase if self._has_tombstone_journal(r)]
            if journals and not discard_tombstone_journal:
                raise DetachRefusedError("tombstone_journal_present")
            left: list[Leftover] = []
            revocation = self._revocation(target_id)
            if revocation is None:  # a revocation on record means the settings were given back
                self._erase_hidden(target_id, erase, report, left)
                if journals and not any(x.code in _INCOMPLETE for x in left):
                    left.append(Leftover("tombstone_journal_erased", len(journals)))
                if not any(leftover.code in _INCOMPLETE for leftover in left):
                    self._restore_settings(
                        contexts, account if key_gone else client, report, left, shared
                    )
            if not any(leftover.code in _INCOMPLETE for leftover in left):
                self._revoke(target_id, revoker, credential, report, left, key_gone=key_gone)
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
        the disk), leave the rest to the same command run again. Otherwise give up: **before** an
        erasure began, the intentions are cancelled and nothing else changes; **after**, there is
        nothing to undo (the folder is gone), so nothing is closed or demoted and the report says
        `past_no_return`: the same command run again finishes."""
        self._authorize(principal)
        report = DetachReport(target_id)
        with self._held(target_id, new_ulid()):
            self._settle_own(target_id, report, resume=resume)
        return report

    # -- the steps ------------------------------------------------------------------------------

    def _erase_hidden(
        self, target_id: str, roots: list[RootRef], report: DetachReport, left: list[Leftover]
    ) -> None:
        done: list[str] = []
        for root in roots:
            open_here = self._open_for_root(target_id, root)
            if self._erased(root):
                if open_here is not None:
                    self._close_erased(open_here.id, root.root_id)
                else:
                    self._demote(root.root_id)
                continue
            intent = self._record_erasure(target_id, root) if open_here is None else open_here.id
            if open_here is None or open_here.state == "pending":
                self._journal.mark_applying(intent)
            try:
                self._fsops.erase_host_area(root.path)
            except (OSError, FsopsError):
                if not self._partly_erased(root):  # nothing moved: nothing to remember
                    self._journal.cancel(intent)
                left.append(Leftover("hidden_not_erased", 1))
                continue
            self._close_erased(intent, root.root_id)
            done.append(root.path)
        report.hidden_removed = tuple(done)

    def _record_erasure(self, target_id: str, root: RootRef) -> str:
        return self._journal.record(
            op="delete", src=f"{root.path}/{RESERVED_DIR}",
            extra={"scope": DETACH_SCOPE, "target_id": target_id, "root_id": root.root_id},
        )  # fmt: skip

    def _restore_settings(
        self,
        contexts: list[DestinationContext],
        client: object | None,
        report: DetachReport,
        left: list[Leftover],
        kept: list[RootRef],
    ) -> None:
        """Gives each library its settings back. The pattern that hides `.shelfhold/` stays on a
        library whose folder holds a root whose reserved folder stays (`kept`): without it Kavita
        would index the bin and the received archives."""
        kept_remote = self._remote_of(contexts, kept)
        restored: list[SettingsRestored] = []
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
        libraries: dict[
            str, list[Any]
        ] = {}  # every destination adopted on one library says its own
        for ctx in contexts:
            if (ref := refs[ctx.destination_id]) is not None:
                libraries.setdefault(ref, []).append(before[ctx.destination_id])
        gone = 0
        for ref, prior_settings in libraries.items():
            try:
                restored.append(self._run(self._restore(client, ref, prior_settings, kept_remote)))
            except LibraryGoneError:  # nothing is left there to give back: said, not a failure
                gone += 1
            except Exception as error:  # Kavita unreachable, refused: said, never hidden
                log.warning("detach.settings_failed", error=type(error).__name__)
                left.append(Leftover("settings_not_restored", 1))
        report.settings = tuple(restored)
        if pinned := sum(s.hidden_pattern_kept for s in restored):
            left.append(Leftover("hidden_pattern_kept", pinned))
        if gone:
            left.append(Leftover("library_gone", gone))
        if any(not s.type_unchanged for s in restored):  # told, not forced
            left.append(
                Leftover("library_type_changed", sum(not s.type_unchanged for s in restored))
            )

    @staticmethod
    def _remote_of(contexts: list[DestinationContext], roots: list[RootRef]) -> tuple[str, ...]:
        """Where Kavita sees these roots (the remote side of the target's path map)."""
        paths = {r.path for r in roots}
        found = {
            e.remote
            for ctx in contexts
            for e in ctx.path_map.entries
            if e.mode is PathMode.RW and any(within(e.local, p) is not None for p in paths)
        }
        return tuple(sorted(found))

    def _revoke(
        self,
        target_id: str,
        revoker: KeyRevoker,
        credential: str | None,
        report: DetachReport,
        left: list[Leftover],
        *,
        key_gone: bool = False,
    ) -> None:
        """The key goes by its identifier, written in an intention (with the digest of its value,
        never the value) before it goes. A stop between the revocation and the end finds, on the
        rerun, an intention that is `done` (nothing to do) or `applying` with the identifier: a
        key absent by that identifier is revoked, and an identifier that now names another key is
        left alone and said. Never by name, never another key of the account."""
        found = self._revocation(target_id)
        if found is not None and found.state == "done":
            report.key_revoked = True
            return
        key_id = None if found is None else found.payload.get("key_id")
        digest = None if found is None else found.payload.get("key_digest")
        named = revoker if isinstance(revoker, KeyIdRevoker) else None
        intent = ""
        try:
            if named is not None and key_id is None:
                handle = named.key_id(target_id, credential=credential)
                if handle is None:  # the account has no such key
                    self._no_key(report, left, key_gone)
                    return
                key_id, digest = handle
            intent = (
                found.id
                if found is not None
                else self._record_revocation(target_id, key_id, digest)
            )
            if found is None or found.state == "pending":
                self._journal.mark_applying(intent)
            if named is not None:
                outcome = named.revoke_id(
                    target_id, credential=credential, key_id=str(key_id),
                    key_digest=None if digest is None else str(digest),
                )  # fmt: skip
            else:
                outcome = (
                    "revoked" if revoker.revoke(target_id, credential=credential) else "failed"
                )
        except Exception as error:  # the account's door failed: the key stays, and it is said
            log.warning("detach.revoke_failed", error=type(error).__name__)
            outcome = "failed"
        if outcome == "failed" and named is None and key_gone:
            self._no_key(report, left, key_gone)
            return
        if outcome in ("revoked", "reused"):
            if intent:
                self._journal.mark_done(intent)
            if outcome == "reused":  # the identifier was given to another key: it stays
                left.append(Leftover("key_id_reused", 1))
        elif (
            intent and named is None
        ):  # without an identifier a failed try leaves nothing to resume
            self._journal.cancel(intent)
        report.key_revoked = outcome in ("revoked", "reused")
        if not report.key_revoked:
            left.append(Leftover("key_not_revoked", 1))

    @staticmethod
    def _no_key(report: DetachReport, left: list[Leftover], key_gone: bool) -> None:
        """No key to name: with `key_gone` that is what the admin said (already gone, said in the
        report), otherwise nothing was done and the detachment is not complete."""
        if key_gone:
            report.key_gone = True
            left.append(Leftover("key_gone", 1))
        else:
            left.append(Leftover("key_not_revoked", 1))

    def _record_revocation(self, target_id: str, key_id: str | None, digest: str | None) -> str:
        return self._journal.record(
            op="delete",
            extra={
                "scope": REVOKE_SCOPE, "target_id": target_id, "key_id": key_id,
                "key_digest": digest,
            },
        )  # fmt: skip

    def _forget(
        self, principal: Principal, target_id: str, contexts: list[DestinationContext]
    ) -> int:
        forgotten = 0
        with self._engine.begin() as conn:
            for ctx in contexts:
                forgotten += forget_destination(
                    conn, ctx.destination_id, copy_scope(ctx, self._roots)
                )
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
        """The erasure intentions of this target a stop left open. An erasure that reached the
        disk is closed (and its root demoted) when the command is run again. Giving up
        (`resume=False`) cancels the intentions of an erasure that has not touched the disk; once
        a root is partly or wholly erased, or a revocation is on record, there is nothing to undo,
        so nothing is closed or demoted and the report says `past_no_return`."""
        mine = [i for i in self._journal.open_intents() if _ours(i, target_id)]
        if not resume and (self._erasure_began(target_id, mine) or self._revocation(target_id)):
            report.past_no_return = True
            return
        for intent in mine:
            if intent.src is None:
                continue
            root = str(intent.src).removesuffix(f"/{RESERVED_DIR}")
            if self._erased(RootRef("", root)):
                self._close_erased(intent.id, str(intent.payload.get("root_id", "")))
            elif not resume or intent.state == "pending":  # nothing touched the disk
                self._journal.cancel(intent.id)
                if not resume:
                    report.cancelled += 1

    def _erasure_began(self, target_id: str, mine: list[OpenIntent]) -> bool:
        """The point of no return: an erasure of this target was closed, or a root of an open one
        is already partly gone (the reserved folder or the sentinel)."""
        if any(row.state == "done" for row in self._intents_of(DETACH_SCOPE, target_id)):
            return True
        return any(
            intent.src is not None
            and self._partly_erased(RootRef("", str(intent.src).removesuffix(f"/{RESERVED_DIR}")))
            for intent in mine
        )

    def _open_for_root(self, target_id: str, root: RootRef) -> OpenIntent | None:
        for intent in self._journal.open_intents():
            if _ours(intent, target_id) and intent.src == f"{root.path}/{RESERVED_DIR}":
                return intent
        return None

    def _revocation(self, target_id: str) -> OpenIntent | None:
        """The intention of the key's revocation, whatever its state but `cancelled`."""
        for row in self._intents_of(REVOKE_SCOPE, target_id):
            if row.state != "cancelled":
                return _open_from(row)
        return None

    def _intents_of(self, scope: str, target_id: str) -> list[Any]:
        table = coherence.fs_intent
        with self._engine.connect() as conn:
            rows = conn.execute(
                sa.select(table).where(table.c.op == "delete").order_by(table.c.created_at)
            ).all()
        return [
            row
            for row in rows
            if isinstance(row.expected_file_id, dict)
            and row.expected_file_id.get("scope") == scope
            and row.expected_file_id.get("target_id") == target_id
        ]

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
        """The roots to erase and the roots that stay: another export target still uses them, or
        Shelfhold's own library lives on them (left alone, said)."""
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
        own_library = set(self._library_roots())
        erase: list[RootRef] = []
        shared: list[RootRef] = []
        for ref in sorted(found.values(), key=lambda r: r.path):
            clash = ref.root_id in own_library or any(
                within(local, ref.path) is not None or within(ref.path, local) is not None
                for local in entries
                if local
            )
            (shared if clash else erase).append(ref)
        return erase, shared

    def _require_free(self, target_id: str, roots: list[RootRef]) -> None:
        """I9: an open file operation of somebody else under a root stops the detachment; the
        target's own erasure and revocation intentions are not somebody else's."""
        bases = frozenset(
            p
            for intent in self._journal.open_intents()
            if not _ours(intent, target_id) and not _is_revocation(intent, target_id)
            for p in (intent.src, intent.dst)
            if p
        )
        for root in roots:
            if within_any(root.path, bases) or holds_any(root.path, bases):
                raise DetachRefusedError("intent_open")

    def _erased(self, root: RootRef) -> bool:
        """Nothing of the reserved folder and the sentinel is left at the top of the root."""
        for name in (RESERVED_DIR, ERASING_DIR, SENTINEL_FILE):
            try:
                if self._fsops.observe(f"{root.path}/{name}", host=True) is not None:
                    return False
            except FsopsError:
                return False
        return True

    def _partly_erased(self, root: RootRef) -> bool:
        """At least one of the reserved folder and the sentinel is gone, or the reserved folder
        is half emptied (renamed to its erasing name)."""
        with contextlib.suppress(FsopsError):
            if self._fsops.observe(f"{root.path}/{ERASING_DIR}", host=True) is not None:
                return True
        for name in (RESERVED_DIR, SENTINEL_FILE):
            try:
                if self._fsops.observe(f"{root.path}/{name}", host=True) is None:
                    return True
            except FsopsError:
                continue
        return False

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
    def _held(self, target_id: str, job_id: str) -> Iterator[None]:
        """Every destination of the target is held, alone (`dest:<id>`, exclusive), for the length
        of the detachment: no export, scan or staging of it runs, and none starts. A job of
        theirs that runs makes this answer `busy`, and the other way round. Released whatever
        happens; a lease that a kill left expires by itself."""
        if self._locks is None:
            yield
            return
        with self._engine.connect() as conn:
            ids = sorted(
                str(i)
                for i in conn.execute(
                    sa.select(export.shelf_destination.c.id).where(
                        export.shelf_destination.c.target_id == target_id
                    )
                ).scalars()
            )
        if not ids:  # an unknown target: the caller says so, nothing to hold
            yield
            return
        try:
            held = self._locks.hold_resources(job_id, [dest_resource(i) for i in ids])
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


def _is_revocation(intent: OpenIntent, target_id: str) -> bool:
    return (
        intent.op == "delete"
        and intent.payload.get("scope") == REVOKE_SCOPE
        and intent.payload.get("target_id") == target_id
    )


def _open_from(row: Any) -> OpenIntent:
    """An intention row, in any state, as the journal's own type (the payload is what matters)."""
    payload = dict(row.expected_file_id or {})
    return OpenIntent(
        id=row.id, op=row.op, src=row.src_path, dst=row.dst_path,
        expected_sha256=row.expected_sha256, expected_file_id=None, state=row.state,
        epoch=row.epoch, schema_version=row.intent_schema_version, plan_id=row.plan_id,
        payload=payload,
    )  # fmt: skip


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
