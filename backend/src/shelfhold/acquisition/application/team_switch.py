"""The preferred team of an Edition becomes the one exported (EF-BIB-005).

`prefer_team` (catalog) only orders the priority list. What is already served for a Unit of
another team is replaced by the same-path upgrade of the Unit, which runs under an intent with
every guard of its own (a read Unit, a suspect revision, an irreplaceable Unit, a frozen root, an
open intent). The Unit follows the preferred team (`unit_variant.preferred`) only when that
upgrade went through, or when nothing is served for it yet: the flag and the served file never
disagree. A Unit whose preferred team has not fetched its release yet is left to the usual
election.

Two steps, for a person who clicks on the series sheet (M4 L35):

- `preview_team_switch` reads, and writes nothing: the Units that would be switched, the blocked
  ones (with the closed word of what refuses them) and the pending ones. Its `token` is the digest
  of what it said;
- `switch_to_preferred_team` applies it: the plan is read again, compared with the token (a
  preview gone stale is never applied: `StaleSwitchError`), then every Unit goes through the
  upgrader of the follow cycle, one at a time, each its own intent.

A served file adopted in place and one that is part of a volume are never replaced here, and
named (`adopted_original`, `assembled`): `upgrade_unit` leaves them out without a word, which a
report must not read as "done". `TeamSwitchService` keeps the preview, queues the application as a
job (a write job: the lease of the library, the queue's pause) and keeps the report the screen
reads.

Nothing is switched for a Variant that is removed or in a numbering conflict.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.acquisition.application.late_upgrade import Upgrader
from shelfhold.catalog.public import Catalog
from shelfhold.export.public import served_blockers
from shelfhold.identity.public import AuditEntry, Credential, Principal, audit_insert
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, storage

__all__ = [
    "TEAM_SWITCH_JOB_KIND",
    "StaleSwitchError",
    "SwitchPreview",
    "SwitchReport",
    "TeamSwitchRefusedError",
    "TeamSwitchService",
    "preview_team_switch",
    "switch_to_preferred_team",
]

TEAM_SWITCH_JOB_KIND = "team.switch"
NOT_FOUND = "team_switch.not_found"
_DONE = frozenset({"upgraded", "up_to_date"})
_PREVIEW_KEPT = timedelta(days=1)  # a preview nobody applied does not stay
_switch = acquisition.team_switch


@dataclass(frozen=True)
class SwitchReport:
    switched: tuple[str, ...] = ()  # Units now exported from the preferred team
    blocked: dict[str, tuple[str, ...]] = field(default_factory=dict)  # Unit -> outcomes refused
    pending: tuple[str, ...] = ()  # Units whose preferred team has fetched nothing yet


@dataclass(frozen=True)
class SwitchPreview:
    """What the application would do, read and not done."""

    edition_id: str
    team_id: str | None
    to_switch: tuple[str, ...]
    blocked: dict[str, tuple[str, ...]]
    pending: tuple[str, ...]
    token: str
    # Unit -> (variant, content) of the preferred team: what the application upgrades to
    targets: dict[str, tuple[str, str]] = field(default_factory=dict, repr=False)

    def as_json(self) -> dict[str, Any]:
        return {
            "to_switch": list(self.to_switch),
            "blocked": {unit: list(words) for unit, words in self.blocked.items()},
            "pending": list(self.pending),
        }


class StaleSwitchError(Exception):
    """The preview is not what would be done now: nothing was written, ask for another."""


class TeamSwitchRefusedError(Exception):
    """A refusal said by its catalog code; an unknown or foreign resource is always `NOT_FOUND`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# -- the plan ---------------------------------------------------------------------------------


def _token(edition_id: str, wanted: str | None, preview: dict[str, Any], pairs: list[Any]) -> str:
    body = json.dumps([edition_id, wanted, preview, pairs], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def preview_team_switch(connection: Connection, edition_id: str) -> SwitchPreview:
    """Read the Units that are served from another team than the preferred one. Writes nothing."""
    service = Catalog(connection)
    priority = service.team_priority(edition_id)
    wanted = priority[0] if priority else None
    to_switch: list[str] = []
    blocked: dict[str, tuple[str, ...]] = {}
    pending: list[str] = []
    targets: dict[str, tuple[str, str]] = {}
    if wanted is not None:
        u, v = catalog.unit, catalog.unit_variant
        units = connection.execute(
            sa.select(u.c.id)
            .select_from(u.join(v, sa.and_(v.c.unit_id == u.c.id, v.c.preferred)))
            .where(u.c.edition_id == edition_id, sa.func.coalesce(v.c.team_id, "") != wanted)
            .order_by(u.c.number_sort, u.c.id)
        ).scalars()
        for unit_id in list(units):
            live = [
                f for f in service.variant_facts(unit_id) if not f.removed and not f.in_conflict
            ]
            mine = [f for f in live if f.team_id == wanted]
            content = _fetched_content(connection, [f.id for f in mine])
            if content is None:
                if mine:
                    pending.append(unit_id)
                continue
            targets[unit_id] = content
            words = served_blockers(connection, unit_id)
            if words:
                blocked[unit_id] = words
            else:
                to_switch.append(unit_id)
    said = {"to_switch": to_switch, "blocked": {k: list(w) for k, w in blocked.items()},
            "pending": pending}  # fmt: skip
    token = _token(edition_id, wanted, said, sorted([k, *t] for k, t in targets.items()))
    return SwitchPreview(
        edition_id, wanted, tuple(to_switch), blocked, tuple(pending), token, targets
    )


def switch_to_preferred_team(
    target: Connection | Engine,
    edition_id: str,
    *,
    upgrader: Upgrader,
    token: str | None = None,
) -> SwitchReport:
    """Apply the switch. With an `Engine` every step has its own transactions (the upgrade commits
    its own: a read snapshot held across it would be stale); with a `Connection` the caller owns
    the transaction. `token` is the one of the preview the person read: another plan is refused."""
    if isinstance(target, Engine):
        with target.connect() as connection:
            plan = preview_team_switch(connection, edition_id)
    else:
        plan = preview_team_switch(target, edition_id)
    if token is not None and plan.token != token:
        raise StaleSwitchError(edition_id)
    switched: list[str] = []
    blocked = dict(plan.blocked)  # refused by the database: the upgrader is never asked
    for unit_id in plan.to_switch:
        variant_id, content_id = plan.targets[unit_id]
        outcomes = tuple(upgrader.upgrade_unit(unit_id, content_id))
        if all(o in _DONE for o in outcomes):
            _follow(target, unit_id, variant_id)
            switched.append(unit_id)
        else:
            blocked[unit_id] = outcomes
    return SwitchReport(tuple(switched), blocked, plan.pending)


def _follow(target: Connection | Engine, unit_id: str, variant_id: str) -> None:
    """The Unit is exported from the preferred team's Variant from now on."""
    if isinstance(target, Engine):
        with write_transaction(target) as connection:
            Catalog(connection).set_preferred(unit_id, variant_id)
    else:
        Catalog(target).set_preferred(unit_id, variant_id)


def _fetched_content(connection: Connection, variant_ids: list[str]) -> tuple[str, str] | None:
    """The oldest fetched cbz of one of the Variants: `(variant id, content id)`."""
    if not variant_ids:
        return None
    r, c, v = acquisition.download_request, storage.content, catalog.unit_variant
    row = connection.execute(
        sa.select(c.c.variant_id, c.c.id)
        .select_from(c.join(r, r.c.variant_id == c.c.variant_id).join(v, v.c.id == c.c.variant_id))
        .where(c.c.variant_id.in_(variant_ids), c.c.format == "cbz", r.c.state == "fetched")
        .order_by(v.c.first_seen_at, v.c.id, c.c.id)
        .limit(1)
    ).first()
    return None if row is None else (str(row[0]), str(row[1]))


# -- the order: preview kept, job queued, report read ------------------------------------------


def _admin(principal: Principal) -> None:
    """Only the admin's own session or the host's command line: never a key, a token, a member."""
    if principal.is_host:
        return
    if principal.credential is not Credential.SESSION or principal.role != "admin":
        raise TeamSwitchRefusedError("forbidden")


def _account(principal: Principal) -> str | None:
    return principal.user_id if principal.credential is Credential.SESSION else None


class TeamSwitchService:
    def __init__(
        self, engine: Engine, clock: Clock, *, submit: Callable[[str, str], str] | None = None
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._submit = submit  # `(kind, dedupe key) -> job id`; the job itself has none

    def preview(self, principal: Principal, edition_id: str) -> dict[str, Any]:
        _admin(principal)
        now = self._clock.now()
        switch_id = new_ulid()
        with write_transaction(self._engine) as connection:
            if not self._edition_exists(connection, edition_id):
                raise TeamSwitchRefusedError(NOT_FOUND)
            plan = preview_team_switch(connection, edition_id)
            if plan.team_id is None:
                raise TeamSwitchRefusedError("team_switch.no_preferred_team")
            connection.execute(  # a preview nobody applied does not stay
                sa.delete(_switch).where(
                    _switch.c.state == "previewed", _switch.c.created_at < now - _PREVIEW_KEPT
                )
            )
            connection.execute(
                sa.insert(_switch).values(
                    id=switch_id, edition_id=edition_id, team_id=plan.team_id,
                    created_by=_account(principal), state="previewed", token=plan.token,
                    preview=plan.as_json(), created_at=now,
                )
            )  # fmt: skip
            self._audit(connection, principal, "team.switch_previewed", switch_id, now)
            row = connection.execute(sa.select(_switch).where(_switch.c.id == switch_id)).one()
        return _view(row)

    def queue(self, principal: Principal, edition_id: str, switch_id: str) -> str:
        """The order to apply a preview, as a job. The audit line is written now; nothing is
        replaced until the job holds the lease of the library."""
        _admin(principal)
        if self._submit is None:
            raise TeamSwitchRefusedError("team_switch.not_found")
        now = self._clock.now()
        with write_transaction(self._engine) as connection:
            row = self._own(connection, edition_id, switch_id)
            if row.state != "previewed":
                raise TeamSwitchRefusedError(NOT_FOUND)  # applied once: asked again, it is gone
            connection.execute(
                sa.update(_switch).where(_switch.c.id == switch_id).values(state="queued")
            )
            self._audit(connection, principal, "team.switch_queued", switch_id, now)
        try:
            return self._submit(TEAM_SWITCH_JOB_KIND, f"{TEAM_SWITCH_JOB_KIND}:{switch_id}")
        except BaseException:
            with write_transaction(self._engine) as connection:  # never queued: asked again
                connection.execute(
                    sa.update(_switch)
                    .where(_switch.c.id == switch_id, _switch.c.state == "queued")
                    .values(state="previewed")
                )
            raise

    def report(self, principal: Principal, edition_id: str, switch_id: str) -> dict[str, Any]:
        _admin(principal)
        with self._engine.connect() as connection:
            return _view(self._own(connection, edition_id, switch_id))

    def run(self, switch_id: str, *, upgrader: Upgrader) -> SwitchReport:
        """The job: apply what the preview said, once, and keep the report.

        The preview is compared with the plan of now before the first write (a stale one writes
        nothing and says so); the job holds the lease of the library from there on, so no chapter
        arrives in between. A job taken up again after a kill (`applying`) has been through that
        check already: it finds the work done so far (the boot has settled the open intents) and
        ends it. One already `done` does nothing. The report says what is true now: a Unit the
        killed run switched is in it."""
        with self._engine.connect() as connection:
            row = connection.execute(sa.select(_switch).where(_switch.c.id == switch_id)).first()
        if row is None:
            raise TeamSwitchRefusedError(NOT_FOUND)
        if row.state in ("done", "stale"):  # `failed` is taken up again by `jobs retry`
            return SwitchReport()
        if row.state != "applying":
            with self._engine.connect() as connection:
                same = preview_team_switch(connection, row.edition_id).token == row.token
            if not same:
                self._mark(switch_id, "stale")
                raise StaleSwitchError(row.edition_id)
            self._mark(switch_id, "applying")
        try:
            report = switch_to_preferred_team(self._engine, row.edition_id, upgrader=upgrader)
        except BaseException:
            self._mark(switch_id, "failed")
            raise
        report = self._as_done(row, report)
        self._finish(switch_id, report)
        return report

    def _as_done(self, row: sa.Row[Any], report: SwitchReport) -> SwitchReport:
        """The Units the preview named that are served from the preferred team now, whichever run
        switched them."""
        said = tuple(dict(row.preview or {}).get("to_switch", ()))
        v = catalog.unit_variant
        with self._engine.connect() as connection:
            now = {
                str(unit)
                for unit in connection.execute(
                    sa.select(v.c.unit_id).where(
                        v.c.unit_id.in_(said), v.c.preferred, v.c.team_id == row.team_id
                    )
                ).scalars()
            }
        switched = tuple(unit for unit in said if unit in now)
        return SwitchReport(switched, report.blocked, report.pending)

    # -- the pieces ---------------------------------------------------------------------------

    @staticmethod
    def _edition_exists(connection: Connection, edition_id: str) -> bool:
        return (
            connection.execute(
                sa.select(catalog.edition.c.id).where(catalog.edition.c.id == edition_id)
            ).first()
            is not None
        )

    @staticmethod
    def _own(connection: Connection, edition_id: str, switch_id: str) -> sa.Row[Any]:
        """The switch of this Edition: a switch of another, or none, is the same answer."""
        row = connection.execute(
            sa.select(_switch).where(_switch.c.id == switch_id, _switch.c.edition_id == edition_id)
        ).first()
        if row is None:
            raise TeamSwitchRefusedError(NOT_FOUND)
        return row

    def _mark(self, switch_id: str, state: str) -> None:
        with write_transaction(self._engine) as connection:
            connection.execute(
                sa.update(_switch).where(_switch.c.id == switch_id).values(state=state)
            )

    def _finish(self, switch_id: str, report: SwitchReport) -> None:
        said = {
            "switched": list(report.switched),
            "blocked": {unit: list(words) for unit, words in report.blocked.items()},
            "pending": list(report.pending),
        }
        with write_transaction(self._engine) as connection:
            connection.execute(
                sa.update(_switch)
                .where(_switch.c.id == switch_id)
                .values(state="done", report=said, applied_at=self._clock.now())
            )

    @staticmethod
    def _audit(
        connection: Connection, principal: Principal, action: str, switch_id: str, now: Any
    ) -> None:
        connection.execute(
            audit_insert(
                AuditEntry(
                    actor_id=principal.actor_id, action=action, at=now,
                    target=f"team_switch:{switch_id}", session_id=principal.session_id,
                    auth_time=principal.auth_time,
                )
            )
        )  # fmt: skip


def _view(row: sa.Row[Any]) -> dict[str, Any]:
    preview = dict(row.preview or {})
    report = dict(row.report) if row.report is not None else None
    return {
        "id": row.id,
        "edition_id": row.edition_id,
        "team_id": row.team_id,
        "state": row.state,
        "to_switch": list(preview.get("to_switch", [])) if report is None else [],
        "blocked": dict(report["blocked"])
        if report is not None
        else dict(preview.get("blocked", {})),
        "pending": list(report["pending"])
        if report is not None
        else list(preview.get("pending", [])),
        "switched": list(report["switched"]) if report is not None else [],
        "created_at": row.created_at,
    }
