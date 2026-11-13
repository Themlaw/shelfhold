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
import contextlib
from collections.abc import Coroutine
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from shelfhold.export.application.migration_detect import SwitchoverDetector
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
from shelfhold.infrastructure.fsops.probe import probe_folder, readable_probe
from shelfhold.infrastructure.schema import export
from shelfhold.ops.errors import CodedRefusal, InvalidChoiceError, UnknownSubjectError
from shelfhold.ops.events import BusTargetEvents

if TYPE_CHECKING:
    from shelfhold.composition import Composition, WriteContext

__all__ = ["WizardOps"]

_UNREADABLE = "no_library"  # `kavita_read` when no Kavita library holds the folder yet


class WizardOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    # -- what the screens read -------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """The Kavita targets (name, address, expiry of the key: nothing secret) and the
        destinations with whether each is linked to a library and adopted."""
        t, d = export.export_target, export.shelf_destination
        with self._comp.engine.connect() as connection:
            targets = [
                {
                    "id": r.id,
                    "name": r.name,
                    "key_expires_at": (r.config or {}).get("key_expires_at"),
                }
                for r in connection.execute(
                    sa.select(t.c.id, t.c.name, t.c.config).where(t.c.kind == "kavita")
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
        return {"targets": targets, "destinations": destinations, "first_run": not targets}

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

    @staticmethod
    async def _forget(client: KavitaClient, remote: str) -> None:
        with contextlib.suppress(TargetError):  # the probe is gone from the disk either way
            await client.scan_folder(remote)

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
        return {
            "fingerprint": report.fingerprint,
            "library_ref": report.library_ref,
            "series": len(report.series),
            "files": report.files,
            "child_library": report.child_library,
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
