"""`target remove` and `detach --full` over the services of the export context (M3 L30,
EF-EXP-031): the preview that says what would be lost, the removal of a destination (keep the
files or take them away) and the full detachment of a Kavita target.

Both are reserved actions (the service checks them again, here and not only at the door): a fresh
admin session or the host CLI, never a key, a member or a child, with the phrase typed. The
refusals leave as catalog codes (`removal.refused`, `detach.refused`) whose message is a closed
word, never a path or a name.

The roots are read when a command runs (`target add` registers one while the server runs). The
exported files are never opened here: `Fsops` is the only door to the disk, the journal the only
way to a deletion.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from shelfhold.export.application.detach import (
    DetachRefusedError,
    DetachReport,
    FullDetacher,
)
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.export.application.remove_destination import (
    RemovalMode,
    RemovalRefusedError,
    RemovalReport,
    RemoveDestination,
)
from shelfhold.export.application.root_ref import RootRef
from shelfhold.export.application.target_state import load_fingerprint
from shelfhold.export.targets.kavita.account_session import KavitaAccountSession
from shelfhold.export.targets.kavita.errors import TargetError
from shelfhold.export.targets.kavita.settings_restore import restore_library_settings
from shelfhold.identity.public import Principal
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.library.public import roots_holding_library_copies
from shelfhold.ops.errors import CodedRefusal, InvalidChoiceError

if TYPE_CHECKING:
    from shelfhold.composition import Composition
    from shelfhold.export.application.roots import LoadedRoots
    from shelfhold.ops.ports import HeadlessOps

__all__ = ["DetachOps"]

_MODES = {m.value: m for m in RemovalMode}


class _Revoker:
    """The account's door: Kavita revokes the key for a login, so the password comes with it."""

    def __init__(self, headless: HeadlessOps) -> None:
        self._headless = headless

    def revoke(self, target_id: str, *, credential: str | None) -> bool:
        key = self.key_id(target_id, credential=credential)
        return key is not None and (
            self.revoke_id(target_id, credential=credential, key_id=key) == "revoked"
        )

    def key_id(self, target_id: str, *, credential: str | None) -> str | None:
        return self._headless.target_find_key(target=target_id, password=credential)

    def revoke_id(self, target_id: str, *, credential: str | None, key_id: str) -> str:
        return self._headless.target_revoke_key_id(
            target=target_id, password=credential, key_id=key_id
        )


class DetachOps:
    def __init__(self, comp: Composition, headless: HeadlessOps) -> None:
        self._comp = comp
        self._headless = headless

    # -- the pieces -----------------------------------------------------------------------------

    def _context(self) -> tuple[Fsops, LoadedRoots, IntentJournal]:
        fsops, loaded, _tombstones = self._comp.read_context()
        return fsops, loaded, IntentJournal(self._comp.engine, fsops, clock=self._comp.clock)

    @staticmethod
    def _root_directory(loaded: LoadedRoots) -> Callable[[str], RootRef | None]:
        rows = {r.root_id: r for r in loaded.roots}

        def roots(local: str) -> RootRef | None:
            root_id = loaded.root_id_for(local)
            return None if root_id is None else RootRef(root_id, rows[root_id].path)

        return roots

    def _remover(
        self,
        fsops: Fsops,
        loaded: LoadedRoots,
        journal: IntentJournal,
        scan: Callable[[str], None] | None,
    ) -> RemoveDestination:
        comp = self._comp
        return RemoveDestination(
            comp.engine, fsops, journal, clock=comp.clock, roots=self._root_directory(loaded),
            locks=ResourceLocks(comp.engine, clock=comp.clock), request_scan=scan,
            fresh_auth_s=comp.settings.fresh_auth_s,
        )  # fmt: skip

    def _scan_of(self, destination_id: str) -> Callable[[str], None] | None:
        """One scan of the folder once its files are gone: best effort, Kavita may be asleep."""
        import sqlalchemy as sa

        from shelfhold.infrastructure.schema import export

        with self._comp.engine.connect() as conn:
            target = conn.execute(
                sa.select(export.shelf_destination.c.target_id).where(
                    export.shelf_destination.c.id == destination_id
                )
            ).scalar()
        if target is None or str(target) not in self._comp.kavita_targets():
            return None
        client = self._comp.kavita_client(str(target))

        def scan(remote_folder: str) -> None:
            with contextlib.suppress(TargetError):  # the periodic scan finds the change anyway
                asyncio.run(client.scan_folder(remote_folder))

        return scan

    # -- list the destinations ------------------------------------------------------------------

    def list_destinations(self) -> list[dict[str, Any]]:
        import json

        import sqlalchemy as sa

        from shelfhold.infrastructure.schema import export

        shelf = export.shelf
        destination = export.shelf_destination
        with self._comp.engine.connect() as connection:
            rows = connection.execute(
                sa.select(
                    destination.c.id,
                    destination.c.shelf_id,
                    shelf.c.name.label("shelf_name"),
                    destination.c.target_id,
                    destination.c.root_subpath,
                    destination.c.transport,
                    destination.c.adopted,
                    destination.c.template,
                )
                .select_from(destination.join(shelf, destination.c.shelf_id == shelf.c.id))
                .order_by(destination.c.id)
            ).all()
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                template = json.loads(row.template)
            except (TypeError, ValueError):
                template = {}
            items.append(
                {
                    "id": row.id,
                    "shelf_id": row.shelf_id,
                    "shelf_name": row.shelf_name,
                    "target_id": row.target_id,
                    "root_subpath": row.root_subpath,
                    "transport": row.transport,
                    "adopted": bool(row.adopted),
                    "flagged_names": 0,
                    "template": template,
                }
            )
        return items

    # -- remove a destination -------------------------------------------------------------------

    def remove_preview(self, *, destination_id: str, mode: str) -> dict[str, Any]:
        fsops, loaded, journal = self._context()
        try:
            preview = self._remover(fsops, loaded, journal, None).preview(
                destination_id, _mode(mode)
            )
        except RemovalRefusedError as error:
            raise _removal(error) from None
        return {
            "destination_id": preview.destination_id, "mode": preview.mode.value,
            "phrase": preview.phrase,
            "warnings": [{"code": w.code, "count": w.count} for w in preview.warnings],
        }  # fmt: skip

    def remove(
        self, *, destination_id: str, mode: str, typed: str | None, give_up: bool,
        principal: Principal,
    ) -> dict[str, Any]:  # fmt: skip
        fsops, loaded, journal = self._context()
        service = self._remover(fsops, loaded, journal, self._scan_of(destination_id))
        try:
            if give_up:
                report = service.settle(destination_id, resume=False, principal=principal)
            else:
                report = service.apply(
                    destination_id, _mode(mode), principal=principal, typed=typed
                )
        except RemovalRefusedError as error:
            raise _removal(error) from None
        return _removal_report(report)

    # -- detach a target ------------------------------------------------------------------------

    def _full_detacher(
        self, fsops: Fsops, loaded: LoadedRoots, journal: IntentJournal
    ) -> FullDetacher:
        comp = self._comp

        def library_roots() -> set[str]:
            with comp.engine.connect() as conn:
                return roots_holding_library_copies(conn)

        return FullDetacher(
            comp.engine, fsops, journal, clock=comp.clock, roots=self._root_directory(loaded),
            restore=restore_library_settings, library_roots=library_roots,
            locks=ResourceLocks(comp.engine, clock=comp.clock),
            store_path=str(comp.settings.store_dir), run=comp.run,
            fresh_auth_s=comp.settings.fresh_auth_s,
        )  # fmt: skip

    def _account_session(self, target_id: str, password: str | None) -> KavitaAccountSession | None:
        """The admin account's session, when its password came with the command."""
        import sqlalchemy as sa

        from shelfhold.infrastructure.schema import export

        if not password:
            return None
        with self._comp.engine.connect() as conn:
            config = conn.execute(
                sa.select(export.export_target.c.config).where(
                    export.export_target.c.id == target_id
                )
            ).scalar_one()
        url = str(config["url"])
        with self._comp.engine.connect() as conn:
            stored = load_fingerprint(conn, target_id)
        return KavitaAccountSession(
            self._comp.network.integration_fetcher(url),
            url,
            str(config.get("username", "")),
            password,
            stored_fingerprint=stored,
        )

    def full_preview(self, *, target_id: str) -> dict[str, Any]:
        fsops, loaded, journal = self._context()
        try:
            preview = self._full_detacher(fsops, loaded, journal).preview(target_id)
        except DetachRefusedError as error:
            raise _full_refusal(error) from None
        return {
            "target_id": preview.target_id, "phrase": preview.phrase,
            "warnings": [{"code": w.code, "count": w.count} for w in preview.warnings],
        }  # fmt: skip

    def full(
        self, *, target_id: str, typed: str | None, credential: str | None,
        discard_hidden_data: bool, discard_tombstone_journal: bool, key_gone: bool = False,
        give_up: bool, principal: Principal,
    ) -> dict[str, Any]:  # fmt: skip
        fsops, loaded, journal = self._context()
        detacher = self._full_detacher(fsops, loaded, journal)
        known = target_id in self._comp.kavita_targets()
        client = self._comp.kavita_client(target_id) if known else None
        account = self._account_session(target_id, credential) if key_gone and known else None
        try:
            if give_up:
                report = detacher.settle(target_id, resume=False, principal=principal)
            else:
                report = detacher.apply(
                    target_id, principal=principal, typed=typed, client=client,
                    revoker=_Revoker(self._headless), credential=credential,
                    discard_hidden_data=discard_hidden_data,
                    discard_tombstone_journal=discard_tombstone_journal,
                    key_gone=key_gone, account=account,
                )  # fmt: skip
        except DetachRefusedError as error:
            raise _full_refusal(error) from None
        finally:
            if account is not None:
                with contextlib.suppress(Exception):  # the connection is best-effort closed
                    self._comp.run(account.aclose())
        if report.past_no_return:  # nothing was undone, and the report must not say it was
            raise CodedRefusal("detach.refused", "past_no_return")
        self._comp.roots_changed()  # the roots demoted are no longer expected stamped
        return _full_report(report)


def _mode(value: str) -> RemovalMode:
    if value not in _MODES:
        raise InvalidChoiceError("choose `keep_files` or `delete_files`")
    return _MODES[value]


def _removal(error: RemovalRefusedError) -> CodedRefusal:
    if error.reason == "not_allowed":
        return CodedRefusal("forbidden", type(error.verdict).__name__)
    return CodedRefusal("removal.refused", error.reason)


def _full_refusal(error: DetachRefusedError) -> CodedRefusal:
    if error.reason == "not_allowed":
        return CodedRefusal("forbidden", type(error.verdict).__name__)
    return CodedRefusal("detach.refused", error.reason)


def _removal_report(report: RemovalReport) -> dict[str, Any]:
    return {
        "deleted": report.deleted, "forgotten": report.forgotten, "cancelled": report.cancelled,
        "complete": report.complete,
        "left": [{"code": line.code, "path": line.path} for line in report.left],
    }  # fmt: skip


def _full_report(report: DetachReport) -> dict[str, Any]:
    return {
        "target_id": report.target_id, "complete": report.complete,
        "hidden_removed": len(report.hidden_removed), "key_revoked": report.key_revoked,
        "key_gone": report.key_gone,
        "forgotten": report.forgotten, "cancelled": report.cancelled,
        "settings_restored": any(s.exclude_patterns_restored for s in report.settings),
        "settings": [
            {
                "exclude_patterns_restored": s.exclude_patterns_restored,
                "type_unchanged": s.type_unchanged,
            }
            for s in report.settings
        ],
        "leftovers": [{"code": line.code, "count": line.count} for line in report.leftovers],
    }  # fmt: skip
