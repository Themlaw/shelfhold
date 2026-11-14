"""The read-only audit of the Kavita accounts that children's profiles name (EF-USR-017,
EF-EXP-036, M3 L10): it extends the access audit of M1a (`audit.py`).

Shelfhold reads `GET /api/Users` and the libraries and compares, for each child who has a
declared account, what that account reaches with the age of the child (`kavita_child_accounts`).
**Nothing is written to Kavita**, and no shelf is paused here: the pause of a shelf stays the job
of `AccessAuditor`, which looks from the libraries' side. This audit looks from the child's side
and tells the admin (`kavita.account_audit`) when a gap opens or changes, once, never every day.

The last answer is kept as one `app_setting` row per target, for the Health page (`health_entry`):
counts and the codes of the findings by profile identifier, never an account name. A read that
fails leaves the state `unverified`: a child is never reported "ok" on a read that did not happen.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.export.application.ports import TargetEvents
from shelfhold.export.application.snapshot import load_context
from shelfhold.export.domain.kavita_child_accounts import (
    ALERT,
    OK,
    UNMAPPED,
    ChildAccount,
    ChildReport,
    Finding,
    LibraryClass,
    assess_child,
    library_ceiling,
)
from shelfhold.export.domain.kavita_instance import covering_libraries, library_is_dedicated
from shelfhold.export.targets.kavita.audit import AccessAuditor, account_of
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import platform

__all__ = [
    "ACCOUNT_AUDIT_EVENT",
    "AccountsReport",
    "ChildAccount",
    "ChildAccountsAuditor",
    "ChildReport",
    "Finding",
    "health_entry",
]

ACCOUNT_AUDIT_EVENT = "kavita.account_audit"
_KEY = "kavita.account_audit."
NEVER_AUDITED = "never_audited"
UNVERIFIED = "unverified"


@dataclass
class AccountsReport:
    children: tuple[ChildReport, ...] = ()
    failed: bool = False
    error: str | None = None  # the class of the error, never its text

    @property
    def alerts(self) -> int:
        return sum(1 for child in self.children if child.status == ALERT)


def _inputs(children: Sequence[ChildAccount]) -> str:
    """What the answer was about: profile, age and declared account. Another value later means
    the kept answer no longer speaks of this child."""
    rows = sorted((c.profile_id, c.max_age, c.kavita_username or "") for c in children)
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def _digest(children: Sequence[ChildReport]) -> str:
    """What the admin was told: the findings by profile, so that the same gap is said once."""
    rows = sorted((c.profile_id, f.code, f.library or "") for c in children for f in c.findings)
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest() if rows else ""


class ChildAccountsAuditor:
    def __init__(self, engine: Engine, clock: Clock, events: TargetEvents) -> None:
        self._engine = engine
        self._clock = clock
        self._events = events

    async def audit(
        self, client: KavitaClient, target_id: str, children: Sequence[ChildAccount]
    ) -> AccountsReport:
        """Read the accounts and the libraries (two GETs), compare, keep the answer, say it once.

        Any error leaves the report `failed`: the previous digest is kept, so that the same gap
        is not announced again when Kavita answers once more."""
        try:  # the reading AND the comparison: nothing escapes, and an old "ok" never survives
            members = await client.list_users()
            libraries = await client.list_libraries()
            accounts = [account_of(m) for m in members]
            classes = self._classes(target_id, libraries)
            report = AccountsReport(
                children=tuple(assess_child(child, accounts, classes) for child in children)
            )
            digest = _digest(report.children)
            before = self._state(target_id).get("digest", "")
            self._save(target_id, report, digest=digest, inputs=_inputs(children))
        except Exception as error:
            return self.record_failure(target_id, error)
        if digest and digest != before:
            self._events.emit(
                ACCOUNT_AUDIT_EVENT, target_id=target_id, children=len(report.children),
                alerts=report.alerts,
            )  # fmt: skip
        return report

    def record_failure(self, target_id: str, error: Exception) -> AccountsReport:
        """The audit could not read: `unverified`, the last digest kept (nothing is said again)."""
        report = AccountsReport(failed=True, error=type(error).__name__)
        self._save(target_id, report, digest=None)
        return report

    def _classes(self, target_id: str, libraries: list[dict[str, Any]]) -> list[LibraryClass]:
        """Each library of Kavita and the highest ceiling of the shelves that feed it. The shelf
        is found by the folder (every library over it), never by the cached identifier alone."""
        ceilings: dict[int, list[int | None]] = {}
        with self._engine.connect() as connection:
            for row in AccessAuditor.links(connection, target_id):
                ctx = load_context(connection, row.destination_id)
                root = ctx.remote_folder(ctx.root_local) if ctx.root_local else None
                covering = covering_libraries(libraries, root) if root else []
                by_id = {int(lib["id"]): lib for lib in libraries}
                for library_id in covering or [int(row.remote_ref)]:
                    library = by_id.get(library_id)
                    # a library that holds more than the folder of the shelf is no family library
                    shared = (
                        library is None or root is None or not library_is_dedicated(library, root)
                    )
                    ceilings.setdefault(library_id, []).append(
                        None if shared else ctx.max_age_rating
                    )
        return [
            LibraryClass(int(lib["id"]), library_ceiling(ceilings.get(int(lib["id"]), [])))
            for lib in libraries
        ]

    # -- the kept answer -----------------------------------------------------------------------

    def _state(self, target_id: str) -> dict[str, Any]:
        return _load(self._engine, target_id)

    def _save(
        self, target_id: str, report: AccountsReport, *, digest: str | None,
        inputs: str | None = None,
    ) -> None:  # fmt: skip
        previous = self._state(target_id)
        state: dict[str, Any] = {
            "audited_at": self._clock.now().isoformat(),
            "failed": report.failed,
            "error": report.error,
            "digest": previous.get("digest", "") if digest is None else digest,
            "inputs": previous.get("inputs", "") if inputs is None else inputs,
            "children": [
                {
                    "profile_id": c.profile_id,
                    "status": c.status,
                    "findings": [{"code": f.code, "library": f.library} for f in c.findings],
                }
                for c in report.children
            ]
            if not report.failed
            else previous.get("children", []),
        }
        key = _KEY + target_id
        table = platform.app_setting
        with write_transaction(self._engine) as connection:
            done = connection.execute(
                sa.update(table).where(table.c.key == key).values(value=json.dumps(state))
            )
            if done.rowcount == 0:
                connection.execute(sa.insert(table).values(key=key, value=json.dumps(state)))


def _load(engine: Engine, target_id: str) -> dict[str, Any]:
    table = platform.app_setting
    with engine.connect() as connection:
        raw = connection.execute(
            sa.select(table.c.value).where(table.c.key == _KEY + target_id)
        ).scalar()
    if raw is None:
        return {}
    try:
        loaded = json.loads(raw)
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


MAX_AGE = timedelta(days=2)  # an audit older than this speaks of nothing: it is to redo


def health_entry(
    engine: Engine, target_id: str, *, children: Sequence[ChildAccount] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:  # fmt: skip
    """The entry of the Health page for the accounts audit (L46 shows it): the state, the counts
    and, by profile identifier, the codes found. No account name, no title."""
    state = _load(engine, target_id)
    if not state:
        return {"state": NEVER_AUDITED, "event": ACCOUNT_AUDIT_EVENT, "alerts": 0, "children": []}
    kept = state.get("children", [])
    alerts = sum(1 for c in kept if c.get("status") == ALERT)
    stale = children is not None and state.get("inputs") != _inputs(children)  # changed since
    if now is not None and not stale:
        try:
            stale = now - datetime.fromisoformat(str(state.get("audited_at"))) > MAX_AGE
        except ValueError:
            stale = True
    verdict = UNVERIFIED if state.get("failed") or stale else ALERT if alerts else OK
    return {
        "state": verdict,
        "event": ACCOUNT_AUDIT_EVENT,
        "alerts": alerts,
        "unmapped": sum(1 for c in kept if c.get("status") == UNMAPPED),
        "audited_at": state.get("audited_at"),
        "children": kept,
    }
