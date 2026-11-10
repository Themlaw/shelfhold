"""`root takeover|reinit|restored`, `store reinit`, `reconcile --rebaseline-inodes`
(EF-EXP-064, EF-EXP-043, EF-INT-023), over the services of the export context.

Three things are made here and nowhere else: the keyring evidence (`SentinelEvidence`, only when a
keyring can be read), the tombstone service that owes the lines to a re-stamped root, and the
refusals as catalog codes. A root exit is also the *status* of the roots: `waiting()` judges the
sentinels, records the states in the `FreezeState` and lists them with their command.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from shelfhold.composition_errors import KeyringUnavailableError, NoLibraryError
from shelfhold.export.application import waiting_marks
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.export.application.rebaseline import Rebaseliner, RebaselineRefusedError
from shelfhold.export.application.root_bootstrap import RootPolicy, RootRefusedError
from shelfhold.export.application.root_exits import (
    EvidenceSource,
    RootBusyError,
    RootExitError,
    RootExits,
    RootWatcher,
)
from shelfhold.identity.public import Principal
from shelfhold.infrastructure.freeze import ROOT_STATES
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.library.public import RootEvidence, SentinelEvidence, TombstoneService
from shelfhold.ops.errors import CodedRefusal, ExitUnavailableError
from shelfhold.ops.ports import WaitingItem

if TYPE_CHECKING:
    from collections.abc import Sequence

    from shelfhold.composition import Composition

__all__ = ["RootOps"]

_COMMANDS = {
    "foreign_root": "shelfhold root takeover {subject}",
    "root_lost": "shelfhold root reinit {subject}",
    "root_behind": "shelfhold root restored {subject}",
    "store_lost": "shelfhold store reinit",
}


class _Evidence:
    """`SentinelEvidence` and the registry of the sentinel, over one `Fsops`."""

    def __init__(self, evidence: SentinelEvidence, fsops: Fsops) -> None:
        self._evidence = evidence
        self._fsops = fsops

    def examine(self, root_path: str, candidates: Sequence[str]) -> dict[str, RootEvidence]:
        return self._evidence.examine(root_path, candidates)

    def tombstone_kids(self, root_path: str) -> dict[str, dict[str, str]]:
        return self._fsops.tombstone_kids(root_path)


class RootOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp
        settings = comp.settings
        store = str(settings.store_dir)
        self._watcher = comp.root_watcher()
        self._exits = RootExits(
            comp.engine, clock=comp.clock, umask=settings.umask, store_path=store,
            locks=ResourceLocks(comp.engine, clock=comp.clock), watcher=self._watcher,
            evidence=self._evidence, tombstones=self._tombstones,
            policy=RootPolicy(settings.library_dir, settings.data_dir, settings.store_dir),
            fresh_auth_s=settings.fresh_auth_s,
        )  # fmt: skip

    # -- what waits -----------------------------------------------------------------------------

    @property
    def watcher(self) -> RootWatcher:
        return self._watcher

    def waiting(self) -> list[WaitingItem]:
        """Judge the roots now, and list the four states with the command that leaves each."""
        try:
            found = [(v.waiting, v.subject) for v in self._watcher.refresh() if v.waiting]
        except sa.exc.OperationalError:
            # not migrated yet, or the database is busy with the server's write: what was
            # recorded last is the best we can say
            found = self._recorded()
        return [
            WaitingItem(
                state,
                "export",
                subject,
                _REASON[state],
                _COMMANDS[state].format(subject=subject),
                state != "root_behind",
            )  # `root restored` deletes nothing and is not reserved (EF-INT-023, N2)
            for state, subject in found
        ]

    def _recorded(self) -> list[tuple[str, str]]:
        try:
            with self._comp.engine.connect() as connection:
                return [(m.state, m.subject) for m in waiting_marks.read_all(connection)
                        if m.state in ROOT_STATES]  # fmt: skip
        except sa.exc.OperationalError:
            return []

    # -- the exits ------------------------------------------------------------------------------

    def takeover(self, subject: str, *, principal: Principal) -> dict[str, Any]:
        return self._run(lambda: self._exits.takeover(subject, principal=principal).as_dict())

    def reinit(self, subject: str, *, principal: Principal) -> dict[str, Any]:
        return self._run(lambda: self._exits.reinit(subject, principal=principal).as_dict())

    def restored(self, subject: str, *, principal: Principal | None = None) -> dict[str, Any]:
        return self._run(lambda: self._exits.restored(subject, principal=principal).as_dict())

    def store_reinit(self, *, principal: Principal) -> dict[str, Any]:
        return self._run(lambda: self._exits.store_reinit(principal=principal).as_dict())

    def rebaseline_inodes(self, destination_id: str | None = None) -> dict[str, Any]:
        fsops, _loaded, _service = self._read_context()
        try:
            report = Rebaseliner(self._comp.engine, fsops, clock=self._comp.clock).run(
                destination_id
            )
        except RebaselineRefusedError as refusal:
            raise CodedRefusal(
                "rebaseline.refused", f"{refusal.reason}: {', '.join(refusal.files[:20])}"
            ) from None
        return report.as_dict()

    # -- wiring ---------------------------------------------------------------------------------

    def _read_context(self) -> tuple[Fsops, Any, TombstoneService]:
        try:
            return self._comp.read_context()
        except NoLibraryError as error:
            raise ExitUnavailableError("no library is mounted here") from error
        except KeyringUnavailableError as error:
            raise ExitUnavailableError("the keyring cannot be read") from error

    def _evidence(self, fsops: Fsops) -> EvidenceSource | None:
        try:
            return _Evidence(SentinelEvidence(self._comp.keyring(), fsops), fsops)
        except KeyringUnavailableError:
            return None  # a root of a known identity can still be taken over; a foreign one cannot

    def _tombstones(self) -> TombstoneService | None:
        try:
            _fsops, _loaded, service = self._comp.read_context()
        except (NoLibraryError, KeyringUnavailableError):
            return None
        return service

    @staticmethod
    def _run(call: Any) -> dict[str, Any]:
        try:
            return call()  # type: ignore[no-any-return]
        except RootBusyError as error:
            raise CodedRefusal("root.busy", str(error)) from None
        except RootRefusedError as error:
            raise CodedRefusal("ops.invalid_choice", str(error)) from None
        except RootExitError as error:
            raise CodedRefusal(error.code, str(error)) from None


_REASON = {
    "foreign_root": "the sentinel of this root is another instance's, or cannot be trusted",
    "root_lost": "this root has no sentinel: the disk was replaced, or the file was removed",
    "root_behind": "this root is older than the database: an old copy was put back",
    "store_lost": "the sentinel of /store is missing, foreign or behind the database",
}
