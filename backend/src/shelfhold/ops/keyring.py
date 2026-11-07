"""The lost-keyring exit over the tombstone service (EF-BIB-053, R62).

`waiting` is cheap (the registry of the database against the keyring, no replay of the journals):
it is read by `/healthz`. The waiver itself builds the service over the mounted roots and runs the
reserved action there, where its rule and its audit live.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.composition import open_fsops
from shelfhold.config import Settings
from shelfhold.export.application.roots import load_roots
from shelfhold.export.domain.path_map import PathMap, PathMode
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.i18n import t
from shelfhold.identity.public import RESERVED_ACTIONS, Allowed, Principal, authorize_reserved
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.freeze import FreezeState
from shelfhold.infrastructure.fsops.tombstone_journal import journal_kid_names, sentinel_kid_tags
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.infrastructure.schema import coherence, identity, platform
from shelfhold.infrastructure.secrets import diagnostics, registry
from shelfhold.infrastructure.secrets.errors import (
    ExportError,
    KeyConflictError,
    KeyringError,
    SecretsError,
)
from shelfhold.infrastructure.secrets.export import (
    decrypt_export,
    read_export_file,
    read_passphrase_file,
)
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.infrastructure.secrets.tombstone_aead import verify_kid_tag
from shelfhold.infrastructure.volumes import ensure_volumes
from shelfhold.integrations.apprise import operating_bus
from shelfhold.library.public import SetAsideRefusedError, TombstoneRoot, TombstoneService
from shelfhold.library.tombstones.journal import MAX_JOURNAL_BYTES
from shelfhold.ops.errors import CodedRefusal, ExitUnavailableError, InvalidChoiceError
from shelfhold.ops.ports import WaitingItem

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["TombstoneKeyringExit"]

_IMPORT = threading.Lock()  # one import at a time in this process; the instance lock does the rest
MAX_AUDITED_KIDS = 100
IMPORT_FOLDER = "import"  # `<data>/import/`: where the API reads the two files of an import
SIMPLE_NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")


class TombstoneKeyringExit:
    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        clock: Clock,
        composition: Composition | None = None,
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._clock = clock
        self._composition = composition

    def waiting(self) -> list[WaitingItem]:
        try:
            keyring = diagnostics.boot_check(self._settings, create=False, announce=False)
        except SecretsError:
            return []  # no readable keyring: the boot check says so, this is not a key to waive
        kid, lost = coherence.tombstone_kid, coherence.tombstone_lost_kid
        with self._engine.connect() as connection:
            known = set(connection.execute(sa.select(kid.c.kid)).scalars())
            waived = set(connection.execute(sa.select(lost.c.kid)).scalars())
        cited = self._cited_by_journals(keyring, waived)
        known |= cited
        owed = coherence.tombstone_pending_root
        with self._engine.connect() as connection:
            owing = sorted(set(connection.execute(sa.select(owed.c.root_id)).scalars()))
        frozen = self._frozen()
        # while the instance is `restored_behind` no line can be written: the way out is the
        # restore's, not a flush
        command = "shelfhold reconcile --adopt-after-restore" if frozen else ""
        return [
            WaitingItem("tombstone_key_unknown", "tombstones", k, "kid not in the keyring")
            for k in sorted(known - waived)
            if keyring.find(k) is None
        ] + [
            WaitingItem(
                "tombstone_lines_owed", "tombstones", root_id, "lines owed to a root", command
            )
            for root_id in owing
        ]

    def _cited_by_journals(self, keyring: Keyring, waived: set[str]) -> set[str]:
        """The kids the journal of a root cites and the keyring lacks, as replay would block on
        them: the sentinel of that root registers them with a tag that verifies under a key we
        hold. Needed because a database restored from before the rotation may not cite the kid
        at all. Read only; the journals are read once per change (a cache, by size and date)."""
        with self._engine.connect() as connection:
            roots = connection.execute(
                sa.select(platform.physical_root.c.root_id, platform.physical_root.c.path_entry)
            ).all()
        found: set[str] = set()
        for root_id, path in roots:
            unknown = {
                kid
                for kid in journal_kid_names(path, max_bytes=MAX_JOURNAL_BYTES)
                if keyring.find(kid) is None and kid not in waived
            }
            if not unknown:
                continue
            registry_tags = sentinel_kid_tags(path)
            for kid in unknown:
                for signer, tag in registry_tags.get(kid, {}).items():
                    key = keyring.find(signer)
                    if key is not None and verify_kid_tag(key.material, root_id, kid, tag):
                        found.add(kid)
                        break
        return found

    def _service(self, *, freeze: bool = False) -> tuple[TombstoneService, list[TombstoneRoot]]:
        """The service over the mounted roots. A root counts as mounted when its sentinel can
        be judged, `restored_behind` included (the exits below are how that state is left)."""
        keyring = diagnostics.boot_check(self._settings, create=False, announce=False)
        with self._engine.connect() as connection:
            paths = [r.path_entry for r in connection.execute(sa.select(platform.physical_root))]
            loaded = load_roots(connection, PathMap.same_path(paths))
        fsops = open_fsops(loaded, umask=self._settings.umask)
        if freeze:
            fsops.freeze()
        service = TombstoneService(self._engine, keyring, fsops, operating_bus(self._clock))
        roots = [
            TombstoneRoot(
                r.root_id, r.path,
                mounted=fsops.root_state(r.path) in (RootState.OK, RootState.RESTORED_BEHIND),
                predecessor_root_ids=r.predecessors,
            )
            for r in loaded.roots
            if r.mode is PathMode.RW
        ]  # fmt: skip
        return service, roots

    def set_aside_journal(
        self, *, principal: Principal, root_id: str | None = None
    ) -> dict[str, str]:
        service, roots = self._service()
        try:
            return service.set_aside_journal(
                roots, principal=principal, now=self._clock.now(), root_id=root_id
            )
        except SetAsideRefusedError as error:
            raise ExitUnavailableError(str(error)) from error

    def _frozen(self) -> bool:
        with self._engine.connect() as connection:
            return FreezeState.read(connection).frozen

    def flush_owed(self) -> list[str]:
        if self._frozen():
            raise ExitUnavailableError(
                "restored_behind: nothing is written yet, "
                "run `shelfhold reconcile --adopt-after-restore`"
            )
        service, roots = self._service()
        for root in roots:
            service.flush_pending(root)
        return service.owed_roots()

    def accept_lost_keyring(
        self, *, confirmation: str, principal: Principal, frozen: bool = False
    ) -> list[str]:
        service, roots = self._service(freeze=frozen)  # frozen: read the journals, write none
        return service.accept_lost_keyring(
            roots, confirmation=confirmation, principal=principal, now=self._clock.now(),
            frozen=frozen,
        )  # fmt: skip

    def import_keyring(
        self,
        *,
        passphrase_file: str,
        in_file: str,
        principal: Principal,
        replay: bool = True,
        confined: bool = False,
    ) -> dict[str, Any]:
        """`secrets import` (reserved: fresh session or host CLI, never a key, audited).

        Order, and what a kill between two steps leaves: (1) the passphrase file and the export
        are read and checked, the keys merged in memory: nothing is written; (2) the keyring is
        replaced in one atomic step (fsynced file, rename, fsynced folder): it is the old one or
        the merged one, never half of one, and the merged one is the durable intention; (3) the
        registry of the database and the audit row commit together; (4) the lines are replayed.
        Running the command again with the same file finds nothing to add and does (3) and (4)
        again, which is how an interruption is resumed. Both channels (the socket of the server,
        the host CLI under the instance lock) already exclude every other writer of the keyring.
        """
        now = self._clock.now()
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS["secrets.import"], now=now,
            fresh_auth_s=self._settings.fresh_auth_s, hardened=self._settings.auth_hardened,
        )  # fmt: skip
        if not isinstance(verdict, Allowed):  # the route and the socket decided first
            raise CodedRefusal("forbidden", type(verdict).__name__)
        if confined:  # the API: names in `<data>/import/`, never a path chosen by the caller
            folder = self._import_folder()
            passphrase_path, export_path = (
                folder / n for n in self._simple((passphrase_file, in_file))
            )
        else:
            for name in (passphrase_file, in_file):
                if not os.path.isabs(name):
                    raise InvalidChoiceError("give the files as absolute paths")
            passphrase_path, export_path = Path(passphrase_file), Path(in_file)
        try:
            passphrase = read_passphrase_file(passphrase_path)
            register_secret_value(passphrase)
            text = read_export_file(export_path, nofollow=confined)
            imported = decrypt_export(text, passphrase)
            diagnostics.register_for_scrubbing(imported)  # no log line may carry a key
        except ExportError as error:
            raise InvalidChoiceError(str(error)) from None
        del passphrase
        with _IMPORT:
            added, target = self._write_keyring(imported)
            kids = added or tuple(key.kid for key in imported.keys)
            audited = "kids:" + ",".join(kids[:MAX_AUDITED_KIDS])
            with self._engine.begin() as connection:
                registry.sync_registry(connection, target)
                # a run that adds nothing and finds its audit row already there is the rerun of
                # an import killed after its commit: it is not audited twice
                done = not added and self._already_audited(connection, audited)
                if not done:
                    connection.execute(
                        sa.insert(identity.audit).values(
                            id=new_ulid(),
                            actor_id=principal.actor_id,
                            action="secrets.import",
                            target=audited,
                            session_id=principal.session_id,
                            auth_time=principal.auth_time,
                        )
                    )
            if self._composition is not None:
                self._composition.keyring_changed()
            replayed = self._replay() if replay else 0
        return {
            "added": list(added),
            "present": len(imported.keys) - len(added),
            "replayed": replayed,
            "deferred": not replay,  # restored_behind: `reconcile --adopt-after-restore` replays
        }

    @staticmethod
    def _already_audited(connection: sa.Connection, target: str) -> bool:
        found = connection.execute(
            sa.select(identity.audit.c.id).where(
                identity.audit.c.action == "secrets.import", identity.audit.c.target == target
            )
        ).first()
        return found is not None

    def _import_folder(self) -> Path:
        folder = self._settings.data_dir / IMPORT_FOLDER
        if folder.is_symlink() or not folder.is_dir():
            raise InvalidChoiceError(f"create the folder {IMPORT_FOLDER}/ in the data folder first")
        return folder

    @staticmethod
    def _simple(names: tuple[str, ...]) -> tuple[str, ...]:
        for name in names:
            if SIMPLE_NAME.fullmatch(name) is None or name.startswith("."):
                raise InvalidChoiceError("give plain file names (letters, digits, . _ -)")
        return names

    def _write_keyring(self, imported: Keyring) -> tuple[tuple[str, ...], Keyring]:
        store = diagnostics.store_for(self._settings)
        try:
            if not store.exists():  # the keyring is gone (restore of /data without it): adopt it
                ensure_volumes(self._settings)
                adopted = imported.exported(self._clock.now())
                store.create(adopted)
                return tuple(key.kid for key in imported.keys), adopted
            merge = store.load().merge(imported, now=self._clock.now())
            if merge.added:
                store.write(merge.keyring)
            return merge.added, merge.keyring
        except KeyConflictError as error:
            raise InvalidChoiceError(str(error)) from None
        except KeyringError as error:
            raise ExitUnavailableError(t("secrets.keyring_error", detail=error)) from None
        except OSError as error:
            raise ExitUnavailableError(
                t("secrets.write_failed", detail=error.strerror or "I/O error")
            ) from None

    def _replay(self) -> int:
        """The journals and the lines owed to a root, read again with the merged keyring."""
        with self._engine.connect() as connection:
            configured = connection.execute(sa.select(platform.instance.c.instance_id)).first()
        if configured is None:
            return 0  # an instance that was never set up holds no line to read
        try:
            service, roots = self._service()
            report = service.replay(roots)
        except (OSError, sa.exc.SQLAlchemyError, SecretsError) as error:
            raise ExitUnavailableError(t("secrets.import_replay_failed")) from error
        return report.flushed + report.merged
