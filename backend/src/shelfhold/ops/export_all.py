"""`shelfhold export-all` and `import-all`: the whole library out in the open format, and back in.

Both are reserved actions (`export.all`, `import.all`): the host CLI, or a fresh admin session,
never a key and never a profile that is not an admin. The export carries every profile's reading
history, so it is the admin's alone, and it is audited (who, which session, how much; never a
title and never the folder's name). It refuses to run when a tombstone cannot be read (a root that
is away, a journal that does not open): what a tombstone covers cannot be left out if the
tombstone cannot be looked at. The import refuses an instance that is not empty.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Final

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.composition_errors import KeyringUnavailableError, NoLibraryError
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.identity.public import RESERVED_ACTIONS, Allowed, Principal, authorize_reserved
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import identity, platform
from shelfhold.library.tombstones.screen import CatalogScreenAdapter, TombstoneScreenAdapter
from shelfhold.ops.errors import CodedRefusal, ExitUnavailableError, InvalidChoiceError
from shelfhold.platform.export_all import export_all as write_export
from shelfhold.platform.export_all import file_label
from shelfhold.platform.export_tree import ExportFolderError, ExportTree
from shelfhold.platform.import_all import ImportRefusedError, ImportReport
from shelfhold.platform.import_all import import_all as read_import

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["EXPORT_ACTION", "IMPORT_ACTION", "ExportAllOps"]

EXPORT_ACTION: Final = "export.all"
IMPORT_ACTION: Final = "import.all"


class ExportAllOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def _authorize(self, action: str, principal: Principal) -> None:
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS[action], now=self._comp.clock.now(),
            fresh_auth_s=self._comp.settings.fresh_auth_s,
        )  # fmt: skip
        if not isinstance(verdict, Allowed):  # the socket decided first; this one is the last gate
            raise CodedRefusal("forbidden", type(verdict).__name__)

    def _audit(self, connection: Connection, action: str, target: str, who: Principal) -> None:
        connection.execute(sa.insert(identity.audit).values(
            id=new_ulid(), actor_id=who.actor_id, action=action, target=target,
            session_id=who.session_id, auth_time=who.auth_time,
        ))  # fmt: skip

    # -- export -----------------------------------------------------------------------------

    def export_all(self, folder: str, *, principal: Principal) -> dict[str, Any]:
        self._authorize(EXPORT_ACTION, principal)
        settings = self._comp.settings
        try:
            fsops, loaded, service = self._comp.read_context()
        except (NoLibraryError, KeyringUnavailableError) as error:
            raise ExitUnavailableError(
                "the tombstones cannot be read (no library or no keyring): nothing is exported"
            ) from error
        screen = TombstoneScreenAdapter(service)
        for root in loaded.roots:
            if fsops.root_state(root.path) is not RootState.OK or screen.root_blocked(root.root_id):
                raise CodedRefusal("root.blocked", "a root whose tombstones cannot be read")
        banned = (
            settings.data_dir, settings.library_dir, settings.store_dir,
            *(Path(r.path) for r in loaded.roots),
        )  # fmt: skip
        try:
            tree = ExportTree.create(folder, forbidden=banned)
        except ExportFolderError as error:
            raise InvalidChoiceError(
                f"this folder cannot take an export ({error.reason})"
            ) from error
        with self._comp.engine.connect() as connection:  # one snapshot for the whole export
            report = write_export(
                connection, tree=tree, screen=screen, works=CatalogScreenAdapter(service),
                opener=fsops.open_read, library_dir=settings.library_dir,
                now=self._comp.clock.now(),
            )  # fmt: skip
        with self._comp.engine.begin() as connection:
            self._audit(
                connection,
                EXPORT_ACTION,
                f"rows:{sum(report.rows.values())};files:{report.files}",
                principal,
            )
        return {"folder": str(tree.root), **report.as_dict()}

    # -- import -----------------------------------------------------------------------------

    def import_all(self, folder: str, *, principal: Principal) -> dict[str, Any]:
        self._authorize(IMPORT_ACTION, principal)

        def audit(connection: Connection, report: ImportReport) -> None:
            self._audit(
                connection, IMPORT_ACTION,
                f"rows:{sum(report.rows.values())};files:{report.files}", principal,
            )  # fmt: skip

        try:
            report = read_import(
                self._comp.engine, folder, target=_Target(self._comp), before_commit=audit
            )
        except ImportRefusedError as error:
            raise InvalidChoiceError(f"the import is refused ({error})") from error
        except NoLibraryError as error:
            raise ExitUnavailableError("the library roots are not registered yet") from error
        return report.as_dict()


class _Target:
    """The files of an import go where the instance's own roots are: the store root, and each
    library root under the name the export gave it (its path below `library_dir`)."""

    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def local_path(self, location: str, root: str, rel_path: str) -> str | None:
        settings = self._comp.settings
        store = str(settings.store_dir).rstrip("/")
        with self._comp.engine.connect() as connection:
            paths = [
                str(p).rstrip("/")
                for p in connection.execute(
                    sa.select(platform.physical_root.c.path_entry)
                ).scalars()
            ]
        if location == "store":
            found = store if store in paths else None
        else:
            found = next(
                (p for p in paths if p != store and file_label(p, settings.library_dir) == root),
                None,
            )
        return None if found is None else f"{found}/{rel_path}"

    @contextmanager
    def writer(self) -> Iterator[Callable[[str, BinaryIO, str], None]]:
        with self._comp.write_context() as write:

            def put(local: str, stream: BinaryIO, sha256_hex: str) -> None:
                write.fsops.write_atomic(
                    local, stream, parents=True, replace=False, verify_digest=lambda: sha256_hex
                )

            yield put
