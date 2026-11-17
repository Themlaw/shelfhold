"""The first-run wizard, part 1: what the screens "Kavita" and "Dossier" do (L39, EF-UX-001,
EF-UX-002).

The Kavita connection itself is `HeadlessAdmin.target_add_kavita` (credentials exchanged for a
named, expiring key, only the key sealed); this class holds the rest: the state the screens read,
the shared-folder probe that Kavita reads, and the adoption of an existing library or the making
of a new one, with the verdict on Suwayomi's download folder.

Nothing here ever touches a Kavita credential: they do not reach this class.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.export.application.migration_detect import SwitchoverDetector
from shelfhold.export.application.pending_scans import remember_scans
from shelfhold.export.application.precreate import PRECREATED
from shelfhold.export.application.snapshot import DestinationContext, load_context
from shelfhold.export.domain.kavita_instance import covering_libraries
from shelfhold.export.domain.physical_roots import discover_physical_roots
from shelfhold.export.targets.kavita.adopt import (
    Adopter,
    AdoptionRefused,
    NoFolderError,
    PreviewStale,
    library_type_named,
)
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.errors import TargetError
from shelfhold.export.targets.kavita.probe_read import kavita_mount_hint, kavita_reads
from shelfhold.i18n import t
from shelfhold.infrastructure.fsops.probe import probe_folder, readable_probe
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.infrastructure.schema import acquisition, export
from shelfhold.infrastructure.secrets import diagnostics
from shelfhold.infrastructure.secrets.errors import SecretsError
from shelfhold.infrastructure.secrets.export import MIN_PASSPHRASE_CHARS, encrypt_export
from shelfhold.ops.errors import (
    CodedRefusal,
    ExitUnavailableError,
    InvalidChoiceError,
    UnknownSubjectError,
)
from shelfhold.ops.events import BusTargetEvents
from shelfhold.ops.status import jurisdiction_report

if TYPE_CHECKING:
    from shelfhold.composition import Composition, WriteContext

__all__ = ["FAMILY_PROPOSED", "WizardOps"]

# The sources the wizard proposes to mark "family" (D13): the free libraries, nothing a third
# party runs (the connected service, a web-novel crawler, an OPDS feed) and nothing unknown.
FAMILY_PROPOSED: tuple[str, ...] = ("gutenberg", "wikisource", "standardebooks")

_UNREADABLE = "no_library"  # `kavita_read` when no Kavita library holds the folder yet


PREVIEW_WAIT_S = 5.0  # a preview that ends within this answers at once; a longer one is a task
_PREVIEWS_KEPT = 16
_PREVIEW_KEEP_S = 3600.0


class _PreviewTask:
    def __init__(self, destination_id: str) -> None:
        self.id = new_ulid()
        self.destination_id = destination_id
        self.finished = threading.Event()
        self.result: dict[str, Any] | None = None
        self.error: BaseException | None = None
        self.ended_at = 0.0


class _PreviewTasks:
    """The previews of an adoption running in the background of this process (M4 L36, #341): the
    probes of a large library take minutes, more than a request may hold. In memory, since a
    preview is read-only and is made again after a restart; the same destination asked while its
    preview runs is the same task (a double click probes nothing twice)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: OrderedDict[str, _PreviewTask] = OrderedDict()

    def start(self, destination_id: str, work: Callable[[], dict[str, Any]]) -> _PreviewTask:
        with self._lock:
            now = time.monotonic()
            for key, old in list(self._tasks.items()):
                expired = old.finished.is_set() and now - old.ended_at > _PREVIEW_KEEP_S
                if expired or (len(self._tasks) >= _PREVIEWS_KEPT and old.finished.is_set()):
                    del self._tasks[key]
            for running in self._tasks.values():
                if running.destination_id == destination_id and not running.finished.is_set():
                    return running
            task = _PreviewTask(destination_id)
            self._tasks[task.id] = task

        def run() -> None:
            try:
                task.result = work()
            except BaseException as error:
                task.error = error
            finally:
                task.ended_at = time.monotonic()
                task.finished.set()

        threading.Thread(target=run, name="adoption-preview", daemon=True).start()
        return task

    def get(self, task_id: str) -> _PreviewTask | None:
        with self._lock:
            return self._tasks.get(task_id)


_PREVIEWS = _PreviewTasks()


class WizardOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    # -- what the screens read -------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """The Kavita targets (name, address, expiry of the key: nothing secret) and the
        destinations with whether each is linked to a library and adopted."""
        target_table, d = export.export_target, export.shelf_destination
        with self._comp.engine.connect() as connection:
            targets = [
                {
                    "id": r.id,
                    "name": r.name,
                    "key_expires_at": (r.config or {}).get("key_expires_at"),
                }
                for r in connection.execute(
                    sa.select(target_table.c.id, target_table.c.name, target_table.c.config).where(
                        target_table.c.kind == "kavita"
                    )
                ).all()
            ]
            linked = {
                r.shelf_destination_id
                for r in connection.execute(
                    sa.select(export.shelf_destination_remote_library.c.shelf_destination_id)
                )
            }
            destinations = [
                {
                    "id": r.id,
                    "target_id": r.target_id,
                    "adopted": bool(r.adopted),
                    "linked": r.id in linked,
                }
                for r in connection.execute(sa.select(d.c.id, d.c.target_id, d.c.adopted))
            ]
            made = set(connection.execute(sa.select(export.shelf.c.name)).scalars())
            family = set(
                connection.execute(
                    sa.select(acquisition.source_config.c.plugin_id).where(
                        acquisition.source_config.c.family_safe.is_(True)
                    )
                ).scalars()
            )
            jurisdiction = jurisdiction_report(connection)
        return {
            "targets": targets,
            "destinations": destinations,
            "first_run": not targets,
            # screens 4 and 5 read the server's own words and facts: the sentence of each shelf
            # it will make, the countries it guessed, whether the keyring was exported
            "shelves": [
                {"name": spec.name, "phrase": spec.phrase, "created": spec.name in made}
                for spec in PRECREATED
            ],
            "sources": [{"key": key, "family": key in family} for key in FAMILY_PROPOSED],
            "keyring_exported": self._keyring_exported(),
            "jurisdiction": {
                "country": str((jurisdiction or {}).get("jurisdiction") or ""),
                "confirmed": bool((jurisdiction or {}).get("confirmed")),
            },
        }

    def _keyring_exported(self) -> bool:
        try:
            return self._comp.keyring().exported_at is not None
        except SecretsError:
            return False  # an unreadable keyring is not an exported one

    # -- the keyring, out of the instance (Q25, EF-INT-015) ---------------------------------------

    def export_keyring(self, passphrase: str) -> str:
        """The whole keyring as an age v1 file under the passphrase (the format of `secrets
        export` and of the backup), recorded as exported. The passphrase and the file are never
        stored, logged nor named in a refusal: the caller gets the text once and keeps it."""
        if len(passphrase) < MIN_PASSPHRASE_CHARS:
            raise InvalidChoiceError(f"the passphrase must have {MIN_PASSPHRASE_CHARS} characters")
        register_secret_value(passphrase)
        store = diagnostics.store_for(self._comp.settings)
        try:
            armored = encrypt_export(self._comp.keyring(), passphrase)
        except SecretsError as error:
            raise ExitUnavailableError(t("secrets.export_failed", detail=error)) from None
        try:  # the screen lets the admin go on only when this is on disk; the running instance
            # holds the keyring's lock for its whole life (as in `import_keyring`), so none here
            store.write(store.load().exported(self._comp.clock.now()))
        except (OSError, SecretsError):
            raise ExitUnavailableError(t("secrets.export_not_recorded")) from None
        return armored

    # -- the sources a child may search (D13) ------------------------------------------------------

    def mark_family(self, source_keys: list[str]) -> list[str]:
        """Exactly these sources, among the ones proposed, are "family" from now on; the others
        proposed are not. A key that is not proposed is refused before anything is written."""
        unknown = [key for key in source_keys if key not in FAMILY_PROPOSED]
        if unknown or len(set(source_keys)) != len(source_keys):
            raise InvalidChoiceError("only the free libraries the wizard proposes can be family")
        table = acquisition.source_config
        now = self._comp.clock.now()
        with self._comp.engine.execution_options(shelfhold_begin="BEGIN IMMEDIATE").begin() as c:
            for key in FAMILY_PROPOSED:
                c.execute(
                    sqlite_insert(table)
                    .values(plugin_id=key)
                    .on_conflict_do_nothing(index_elements=["plugin_id"])
                )
                c.execute(
                    sa.update(table)
                    .where(table.c.plugin_id == key)
                    .values(family_safe=key in source_keys, updated_at=now)
                )
        return list(source_keys)

    # -- the probe ---------------------------------------------------------------------------------

    def probe(self, destination_id: str) -> dict[str, Any]:
        """Can this process write, rename and make a folder in the shared folder (the CLI's
        write probe), and does Kavita read a file dropped there? The file is written by fsops and
        removed when the check ends, whatever it found. A failure is the exact compose block."""
        ctx = self._context(destination_id)
        folder = self._folder(ctx)
        with self._comp.write_context() as w:
            wrote = probe_folder(w.fsops, folder)  # type: ignore[arg-type]
            out: dict[str, Any] = {
                "ok": wrote.ok,
                "write": {"ok": wrote.ok, "cause": wrote.cause, "failed_step": wrote.failed_step},
                "kavita_read": None,
                "yaml_hint": wrote.yaml_hint or None,
            }
            if not wrote.ok:
                return out
            remote = ctx.remote_folder(folder)
            client = self._comp.kavita_client(ctx.target_id)
            with readable_probe(w.fsops, folder) as probe_path:  # type: ignore[arg-type]
                read = self._guarded(self._read(client, ctx, remote, probe_path))
            if read != _UNREADABLE:  # Kavita has indexed the probe: a scan makes it forget it
                self._guarded(self._forget(client, remote))
        out["kavita_read"] = read
        if read is False:
            out["ok"] = False
            out["yaml_hint"] = kavita_mount_hint(remote)
        elif read == _UNREADABLE:
            out["ok"] = True  # the folder is right; Kavita has no library on it yet
        return out

    async def _read(
        self, client: KavitaClient, ctx: DestinationContext, remote: str, probe_path: str
    ) -> bool | str:
        library = await self._library_of(client, ctx, remote)
        if library is None:
            return _UNREADABLE
        remote_probe = f"{remote.rstrip('/')}/{probe_path.rsplit('/', 1)[1]}"
        return await kavita_reads(client, library, remote, remote_probe)

    async def _forget(self, client: KavitaClient, remote: str) -> None:
        try:
            await client.scan_folder(remote)
        except TargetError:  # the probe is gone from the disk, but Kavita still lists it: not lost
            remember_scans(self._comp.engine, [remote])

    async def _library_of(
        self, client: KavitaClient, ctx: DestinationContext, remote: str
    ) -> int | None:
        libraries = await client.list_libraries()
        if ctx.remote_library_id is not None:
            linked = self._linked_ref(ctx)
            if linked is not None and any(str(lib["id"]) == linked for lib in libraries):
                return int(linked)
        covering = covering_libraries(libraries, remote)
        return covering[0] if covering else None

    # -- adoption --------------------------------------------------------------------------------

    def adoption_preview_start(
        self,
        destination_id: str,
        *,
        library_ref: str | None = None,
        download_dir: str | None = None,
        wait_s: float | None = None,
    ) -> dict[str, Any]:
        """The preview as a background task: `done` with the report when it ends within `wait_s`,
        else `running` with the task to ask for (`adoption_preview_status`). An unknown
        destination is refused now."""
        self._context(destination_id)
        task = _PREVIEWS.start(
            destination_id,
            lambda: self.adoption_preview(
                destination_id, library_ref=library_ref, download_dir=download_dir
            ),
        )
        task.finished.wait(PREVIEW_WAIT_S if wait_s is None else wait_s)
        return self._preview_state(task)

    def adoption_preview_status(self, job_id: str) -> dict[str, Any]:
        task = _PREVIEWS.get(job_id)
        if task is None:
            raise UnknownSubjectError("unknown preview")
        return self._preview_state(task)

    @staticmethod
    def _preview_state(task: _PreviewTask) -> dict[str, Any]:
        if not task.finished.is_set():
            return {"status": "running", "job_id": task.id, "preview": None}
        if task.error is not None:
            if isinstance(task.error, CodedRefusal | InvalidChoiceError | UnknownSubjectError):
                raise task.error
            raise CodedRefusal("job.failed", "the preview failed")
        return {"status": "done", "job_id": task.id, "preview": task.result}

    def adoption_preview(
        self,
        destination_id: str,
        *,
        library_ref: str | None = None,
        download_dir: str | None = None,
    ) -> dict[str, Any]:
        """What adopting would do (nothing is written), the verdict on the folder Suwayomi
        downloads to when it is given, and the physical roots of the library (EF-EXP-059)."""
        ctx = self._context(destination_id)
        with self._comp.write_context() as w:
            adopter, client = self._adopter(w, ctx)
            report = self._guarded(self._preview(adopter, client, ctx, library_ref))
            verdict = None
            if download_dir:
                verdict = SwitchoverDetector(
                    self._comp.engine,
                    w.fsops,
                ).classify_download_dir(destination_id, download_dir)
            roots = self._roots(ctx, w)
        reasons = {f.rel_path: f.check for s in report.series for f in s.files if f.check}
        return {
            "fingerprint": report.fingerprint,
            "library_ref": report.library_ref,
            "series": len(report.series),
            "files": report.files,
            "child_library": report.child_library,
            "to_check": [{"path": path, "reason": reasons[path]} for path in report.to_check],
            "download_dir": asdict(verdict) if verdict else None,
            "roots": roots,
        }

    def adopt(
        self, destination_id: str, *, fingerprint: str, library_ref: str | None = None
    ) -> dict[str, Any]:
        """Adopt on exactly what the preview showed (`fingerprint`), and link the library."""
        ctx = self._context(destination_id)
        with self._comp.write_context() as w:
            adopter, client = self._adopter(w, ctx)
            report = self._guarded(self._adopt(adopter, client, ctx, fingerprint, library_ref))
        return {
            "adopted_files": report.adopted_files,
            "already_adopted": report.already_adopted,
            "series": len(report.series),
            "library_ref": report.library_ref,
            "linked": True,
        }

    def create_library(
        self, destination_id: str, *, name: str, library_type: str | None = None
    ) -> dict[str, Any]:
        """A new Kavita library on the folder of the destination, then linked to it."""
        ctx = self._context(destination_id)
        folder = self._folder(ctx)
        remote = ctx.remote_folder(folder)
        with self._comp.write_context() as w:
            adopter, client = self._adopter(w, ctx)

            async def make() -> str:
                made = await client.create_library(name, library_type_named(library_type), [remote])
                ref = str(made["id"])
                await adopter.link_library(client, destination_id, ref)
                return ref

            ref = self._guarded(make())
        return {"library_ref": ref, "linked": True}

    async def _which(
        self, client: KavitaClient, ctx: DestinationContext, library_ref: str | None
    ) -> str:
        """The library to adopt: the one named, else the one the folder is linked to, else the
        one Kavita holds over the folder; with none of them there is nothing to adopt."""
        if library_ref is not None:
            return library_ref
        found = await self._library_of(client, ctx, ctx.remote_folder(self._folder(ctx)))
        if found is None:
            raise InvalidChoiceError("no Kavita library holds this folder: make a new one")
        return str(found)

    async def _preview(
        self, adopter: Adopter, client: KavitaClient, ctx: DestinationContext, ref: str | None
    ) -> Any:
        return await adopter.preview(
            client, ctx.destination_id, library_ref=await self._which(client, ctx, ref)
        )

    async def _adopt(
        self,
        adopter: Adopter,
        client: KavitaClient,
        ctx: DestinationContext,
        fingerprint: str,
        ref: str | None,
    ) -> Any:
        return await adopter.adopt(
            client,
            ctx.destination_id,
            confirm=fingerprint,
            library_ref=await self._which(client, ctx, ref),
        )

    # -- plumbing --------------------------------------------------------------------------------

    def _context(self, destination_id: str) -> DestinationContext:
        try:
            with self._comp.engine.connect() as connection:
                return load_context(connection, destination_id)
        except (sa.exc.NoResultFound, KeyError):
            raise UnknownSubjectError("unknown destination") from None

    @staticmethod
    def _folder(ctx: DestinationContext) -> str:
        if ctx.root_local is None:
            raise InvalidChoiceError("this destination has no folder on this machine")
        return ctx.root_local

    def _linked_ref(self, ctx: DestinationContext) -> str | None:
        with self._comp.engine.connect() as connection:
            ref = connection.execute(
                sa.select(export.remote_library.c.remote_ref).where(
                    export.remote_library.c.id == ctx.remote_library_id
                )
            ).scalar()
        return None if ref is None else str(ref)

    def _adopter(self, w: WriteContext, ctx: DestinationContext) -> tuple[Adopter, KavitaClient]:
        comp = self._comp
        adopter = Adopter(
            comp.engine, w.fsops, locate=w.loaded.locate, clock=comp.clock,
            auditor=AccessAuditor(comp.engine, comp.clock, BusTargetEvents(comp.engine, comp.bus)),
            tombstones=w.tombstones,
        )  # fmt: skip
        return adopter, comp.kavita_client(ctx.target_id)

    @staticmethod
    def _roots(ctx: DestinationContext, w: WriteContext) -> list[dict[str, Any]]:
        if ctx.root_local is None:
            return []
        found = discover_physical_roots(ctx.path_map, ctx.root_local, w.fsops.device_of)
        return [{"path": r.path, "present": r.st_dev is not None} for r in found]

    @staticmethod
    def _guarded[T](work: Coroutine[Any, Any, T]) -> T:
        """Run the coroutine; the refusals of the adoption and of Kavita are catalog codes."""
        try:
            return asyncio.run(work)
        except AdoptionRefused as error:
            raise CodedRefusal(error.code, "accounts below the ceiling read this library") from None
        except PreviewStale:
            raise CodedRefusal("ops.preview_stale", "look at the preview again") from None
        except NoFolderError:
            raise InvalidChoiceError("this destination is not linked to a Kavita library") from None
        except TargetError as error:
            raise CodedRefusal(_target_code(error), type(error).__name__) from None


def _target_code(error: TargetError) -> str:
    code = getattr(error, "code", None)
    return code if isinstance(code, str) and code.startswith("target.") else "target.unreachable"
