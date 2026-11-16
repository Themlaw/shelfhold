"""The read-only audit of the accounts of a Komga (EF-EXP-047, ENF-SEC-016, M4 L08b).

Komga keeps accounts apart by library. Shelfhold reads the release, the accounts and the libraries
(`/actuator/info`, `/api/v2/users`, `/api/v1/libraries`: three GETs, **nothing is written to
Komga**) and compares, for the folder of each shelf, what each child's account reaches with the
rating of the shelf (`domain.komga_access`). The partitioning of a Komga counts for a folder only
when this audit proved it (`application.komga_audit_state`): the declaration of the adapter and the
attestation of an admin are never enough.

It fails closed. A server that does not answer, a key that may not list the accounts, an answer
that cannot be read, a release nobody could read or below 1.22.0: nothing is proved, the shelves
below 18+ (all of them, when the reading itself failed) are paused with `target.child_exposure`,
and the refusal of a binding carries the code. Only a complete audit lifts a pause, and only one
this audit posed: a shelf paused by another server's audit stays paused.

What is kept and announced carries codes, references of libraries and shelf identifiers: never a
key, never the name of an account.
"""

from __future__ import annotations

from sqlalchemy import Engine

from shelfhold.export.application.komga_audit import (
    KomgaAuditRecorder,
    KomgaAuditReport,
    Reading,
    cleaned,
)
from shelfhold.export.application.ports import TargetEvents
from shelfhold.export.domain.kavita_compat import KavitaVersion
from shelfhold.export.domain.komga_access import BoundShelf, KomgaLibrary, assess_version
from shelfhold.export.targets.komga.client import KomgaClient
from shelfhold.infrastructure.clock import Clock

__all__ = ["KomgaAuditReport", "KomgaAuditor"]


class KomgaAuditor:
    def __init__(self, engine: Engine, clock: Clock, events: TargetEvents) -> None:
        self._recorder = KomgaAuditRecorder(engine, clock, events)

    async def admit(
        self, client: KomgaClient, target_id: str, *, shelf_ceiling: int | None, remote_root: str
    ) -> KomgaAuditReport:
        """Before linking a shelf to this server: the audit with that shelf in it. A proof is
        recorded only for a folder that came out clean."""
        candidate = BoundShelf(None, cleaned(remote_root), shelf_ceiling)
        return await self._run(client, target_id, candidate)

    async def audit(self, client: KomgaClient, target_id: str) -> KomgaAuditReport:
        """The replay of every connection and every day, over the shelves bound now."""
        return await self._run(client, target_id, None)

    # -- one audit ------------------------------------------------------------------------------

    async def _run(
        self, client: KomgaClient, target_id: str, candidate: BoundShelf | None
    ) -> KomgaAuditReport:
        try:
            reading = await self._read(client)
        except Exception as error:  # the reading AND its interpretation: nothing escapes
            return self._recorder.record(target_id, candidate, None, failed=type(error).__name__)
        return self._recorder.record(target_id, candidate, reading)

    @staticmethod
    async def _read(client: KomgaClient) -> Reading:
        raw = await client.probe()
        verdict = assess_version(raw)
        parsed = KavitaVersion.parse(raw)
        version = str(parsed) if parsed is not None else None
        advisories = tuple(f"{k.advisory} ({k.category})" for k in verdict.known_bad)
        if not verdict.accepted:  # no need to read more: a release that is refused is refused
            return Reading(version, refusal=verdict.reason, advisories=advisories)
        accounts = await client.list_accounts()
        libraries = await client.list_libraries()
        return Reading(
            version, tuple(accounts), tuple(KomgaLibrary(lib.id, lib.root) for lib in libraries)
        )
