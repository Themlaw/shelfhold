"""Pairing an adopted series with the same series followed in Suwayomi (EF-EXP-038, EF-EXP-053).

An adopted series and its upstream twin are paired *by proposal*: the title (normalized as Kavita
does) and the source are shown, the admin confirms, nothing is bound silently. A paired series
follows the new chapters only: the binding carries the watermark `follow_from` (the highest
chapter already adopted), so nothing at or below it is asked of Suwayomi, and the next chapter
goes into the folder and under the identity of the Kavita series that exists, never into a second
one. A series with no chapter file (volumes only) is never followed by itself (`coverage_unknown`,
way out: `adopt set-follow-from`).

Before binding, the series folder is probed for write access (EF-EXP-054), and a shelf that looks
fed directly by Suwayomi waits for the confirmed switchover procedure (EF-EXP-044).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Final, Literal

import sqlalchemy as sa
from sqlalchemy import Connection, Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.export.application.migration_detect import SwitchoverGate
from shelfhold.export.application.preflight import PermissionPreflight, ProbeResult
from shelfhold.export.domain.naming import normalize_series_name
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, export
from shelfhold.sdk.refs import SeriesRef

__all__ = [
    "AlreadyPairedError",
    "AutoPairReport",
    "BatchStaleError",
    "Candidate",
    "Pairing",
    "PairingRefusedError",
    "PairingResult",
    "Proposal",
]

State = Literal["ready", "ambiguous", "no_match", "coverage_unknown", "already_paired"]
PROPOSE_FROM: Final = 0.75  # below this score a title is not even proposed
AUTO_MIN_SCORE: Final = 0.9  # `--pair-auto --min-score`
MAX_NUMBER: Final = Decimal(10) ** 7
PLUGIN_ID: Final = "suwayomi"
SET_FOLLOW_FROM: Final = "shelfhold adopt set-follow-from <series> <chapter>"


class AlreadyPairedError(Exception):
    """The series already follows an upstream series: a second pairing would be a duplicate."""

    code = "pairing.already_paired"


class PairingRefusedError(Exception):
    """The folder of the series cannot be written: nothing is bound (EF-EXP-054)."""

    code = "pairing.not_writable"

    def __init__(self, probe: ProbeResult) -> None:
        super().__init__(probe.explain())
        self.probe = probe


class BatchStaleError(Exception):
    """The batch confirmed is not the one proposed any more (`--pair-auto`)."""

    code = "pairing.batch_stale"


@dataclass(frozen=True)
class Candidate:
    ref: SeriesRef
    score: float  # 1.0: the same normalized title


@dataclass(frozen=True)
class Proposal:
    export_series_id: str
    edition_id: str
    series_name: str
    candidates: tuple[Candidate, ...]  # best first; the source key tells two twins apart
    state: State
    follow_from: str | None  # the watermark that pairing would pose, in chapter space
    exit_command: str = ""


@dataclass(frozen=True)
class PairingResult:
    state: Literal["paired", "coverage_unknown"]
    export_series_id: str
    edition_id: str
    binding_id: str | None = None
    follow_from: str | None = None
    exit_command: str = ""

    @property
    def paired(self) -> bool:
        return self.state == "paired"


@dataclass(frozen=True)
class AutoPairReport:
    fingerprint: str
    ready: tuple[Proposal, ...]  # unambiguous at or above the minimum score: one confirmation
    held: tuple[Proposal, ...]  # everything else, to be decided one by one


@dataclass(frozen=True)
class _Adopted:
    series_id: str
    edition_id: str
    work_id: str
    name: str
    names: tuple[str, ...]


def score_of(wanted: Sequence[str], title: str) -> float:
    other = normalize_series_name(title)
    if not other:
        return 0.0
    best = 0.0
    for name in wanted:
        if name == other:
            return 1.0
        best = max(best, SequenceMatcher(None, name, other).ratio())
    return round(best, 4)


class Pairing:
    def __init__(
        self, engine: Engine, preflight: PermissionPreflight, gate: SwitchoverGate, *, clock: Clock
    ) -> None:
        self._engine = engine
        self._preflight = preflight
        self._gate = gate
        self._clock = clock

    # -- proposals ----------------------------------------------------------------------------

    def propose(
        self, destination_id: str, library: Sequence[SeriesRef], *, min_score: float = PROPOSE_FROM
    ) -> list[Proposal]:
        """One proposal per adopted series of the destination; nothing is written."""
        with self._engine.connect() as connection:
            adopted = self._adopted(connection, destination_id)
            taken = self._taken(connection)
            paired = self._paired_editions(connection)
            proposals = []
            for item in adopted:
                if item.edition_id in paired:
                    proposals.append(
                        Proposal(
                            item.series_id, item.edition_id, item.name, (), "already_paired", None
                        )
                    )
                    continue
                free = [r for r in library if (r.source_key, r.remote_id) not in taken]
                scored = sorted(
                    (Candidate(r, score_of(item.names, r.title)) for r in free),
                    key=lambda c: (-c.score, c.ref.source_key, c.ref.remote_id),
                )
                near = tuple(c for c in scored if c.score >= min_score)
                mark = self._follow_from(connection, item.edition_id)
                proposals.append(self._proposal(item, near, mark))
        return proposals

    @staticmethod
    def _proposal(item: _Adopted, near: tuple[Candidate, ...], mark: str | None) -> Proposal:
        if not near:
            state: State = "no_match"
        elif len(near) > 1 and near[0].score == near[1].score:
            state = "ambiguous"  # the same title in two places: the admin picks the source
        elif mark is None:
            state = "coverage_unknown"
        else:
            state = "ready"
        exit_command = SET_FOLLOW_FROM if state == "coverage_unknown" else ""
        return Proposal(item.series_id, item.edition_id, item.name, near, state, mark, exit_command)

    @staticmethod
    def _adopted(connection: Connection, destination_id: str) -> list[_Adopted]:
        series, link = export.export_series, export.edition_export_series
        edition, work = catalog.edition, catalog.work
        rows = connection.execute(
            sa.select(
                series.c.id, series.c.name_raw, series.c.frozen_series,
                series.c.frozen_localized_series, edition.c.id.label("edition_id"),
                work.c.id.label("work_id"), work.c.title,
            )
            .select_from(
                series.join(
                    link,
                    sa.and_(
                        link.c.export_series_id == series.c.id,
                        link.c.remote_library_id == series.c.remote_library_id,
                    ),
                )
                .join(edition, edition.c.id == link.c.edition_id)
                .join(work, work.c.id == edition.c.work_id)
            )
            .where(series.c.shelf_destination_id == destination_id, series.c.frozen_at.isnot(None))
            .order_by(series.c.id)
        )  # fmt: skip
        found = []
        for row in rows:
            texts = (row.frozen_series, row.frozen_localized_series, row.name_raw, row.title)
            names = tuple(dict.fromkeys(n for t in texts if t and (n := normalize_series_name(t))))
            found.append(_Adopted(row.id, row.edition_id, row.work_id, row.name_raw, names))
        return found

    @staticmethod
    def _taken(connection: Connection) -> set[tuple[str, str]]:
        """Upstream series that an active binding already follows: never offered twice."""
        item, binding = acquisition.source_item, acquisition.source_binding
        return {
            (r.source_key, r.remote_id)
            for r in connection.execute(
                sa.select(item.c.source_key, item.c.remote_id)
                .select_from(binding.join(item, item.c.id == binding.c.source_item_id))
                .where(binding.c.active, item.c.plugin_id == PLUGIN_ID)
            )
        }

    @staticmethod
    def _paired_editions(connection: Connection) -> set[str]:
        binding = acquisition.source_binding
        return set(
            connection.execute(
                sa.select(binding.c.edition_id).where(
                    binding.c.active, binding.c.linked_from_adoption
                )
            ).scalars()
        )

    @staticmethod
    def _follow_from(connection: Connection, edition_id: str) -> str | None:
        """The highest chapter the adopted files carry, in chapter space: a volume of unknown
        range counts for none (EF-BIB-044). `None`: no chapter file, no automatic follow."""
        unit = catalog.unit
        best: tuple[int, Decimal] | None = None
        rows = connection.execute(
            sa.select(unit.c.number_segment_ordinal, unit.c.number_norm).where(
                unit.c.edition_id == edition_id, unit.c.track == "main", unit.c.state == "adopted"
            )
        )
        for row in rows:
            try:
                key = (row.number_segment_ordinal, Decimal(row.number_norm))
            except InvalidOperation:
                continue
            best = key if best is None or key > best else best
        return format(best[1], "f") if best else None

    # -- confirming ---------------------------------------------------------------------------

    def confirm(
        self,
        destination_id: str,
        export_series_id: str,
        ref: SeriesRef,
        *,
        follow_from: str | None = None,
    ) -> PairingResult:
        """The admin accepts a proposal (or `set-follow-from` gives the watermark by hand)."""
        with self._engine.connect() as connection:
            item = next(
                (
                    a
                    for a in self._adopted(connection, destination_id)
                    if a.series_id == export_series_id
                ),
                None,
            )
            if item is None:
                raise LookupError(f"{export_series_id} is not an adopted series of the destination")
            paired = item.edition_id in self._paired_editions(connection)
            mark = (
                _checked(follow_from)
                if follow_from is not None
                else self._follow_from(connection, item.edition_id)
            )
        if paired:
            raise AlreadyPairedError(item.name)
        self._gate.require_clear(destination_id)  # EF-EXP-044: before anything else is decided
        if mark is None:
            return PairingResult(
                "coverage_unknown", item.series_id, item.edition_id, None, None, SET_FOLLOW_FROM
            )
        probe = self._preflight.check_series(destination_id, export_series_id)
        if probe is not None and not probe.ok:
            raise PairingRefusedError(probe)
        with self._engine.begin() as connection:
            binding_id = self._bind(connection, item, ref, mark, self._clock.now())
        return PairingResult("paired", item.series_id, item.edition_id, binding_id, mark)

    @staticmethod
    def _bind(
        connection: Connection, item: _Adopted, ref: SeriesRef, mark: str, now: datetime
    ) -> str:
        source = acquisition.source_item
        found = connection.execute(
            sa.select(source.c.id).where(
                source.c.plugin_id == PLUGIN_ID,
                source.c.source_key == ref.source_key,
                source.c.remote_id == ref.remote_id,
            )
        ).scalar()
        item_id = str(found) if found else new_ulid()
        if not found:
            connection.execute(
                sa.insert(source).values(
                    id=item_id, plugin_id=PLUGIN_ID, source_key=ref.source_key,
                    remote_id=ref.remote_id, memo=ref.memo, memo_version=ref.memo_version,
                    last_seen=now,
                )
            )  # fmt: skip
        binding_id = new_ulid()
        connection.execute(
            sa.insert(acquisition.source_binding).values(
                id=binding_id, work_id=item.work_id, edition_id=item.edition_id,
                source_item_id=item_id, priority=1, active=True, linked_from_adoption=True,
            )
        )  # fmt: skip
        follow = acquisition.series_follow
        values = {
            "follow_from_segment": "",
            "follow_from_number": mark,
            "follow_from_norm_by": "number@1",
            "updated_at": now,
        }
        connection.execute(
            sqlite_insert(follow)
            .values(edition_id=item.edition_id, work_id=item.work_id, **values)
            .on_conflict_do_update(index_elements=["edition_id"], set_=values)
        )
        return binding_id

    # -- in bulk (`--pair-auto`, volet coupable d'EF-EXP-038) -----------------------------------

    def pair_auto(
        self,
        destination_id: str,
        library: Sequence[SeriesRef],
        *,
        min_score: float = AUTO_MIN_SCORE,
    ) -> AutoPairReport:
        """What a batch would pair, with a report; nothing is bound before `confirm_batch`."""
        proposals = self.propose(destination_id, library)
        ready = tuple(
            p for p in proposals if p.state == "ready" and p.candidates[0].score >= min_score
        )
        held = tuple(p for p in proposals if p not in ready)
        return AutoPairReport(_fingerprint(ready), ready, held)

    def confirm_batch(
        self,
        destination_id: str,
        library: Sequence[SeriesRef],
        *,
        fingerprint: str,
        min_score: float = AUTO_MIN_SCORE,
    ) -> list[PairingResult | PairingRefusedError]:
        """Confirm the batch seen, once. Another batch (the library moved) is refused whole;
        a folder that cannot be written refuses its own series only."""
        report = self.pair_auto(destination_id, library, min_score=min_score)
        if report.fingerprint != fingerprint:
            raise BatchStaleError(destination_id)
        self._gate.require_clear(destination_id)
        results: list[PairingResult | PairingRefusedError] = []
        for proposal in report.ready:
            try:
                results.append(
                    self.confirm(
                        destination_id, proposal.export_series_id, proposal.candidates[0].ref
                    )
                )
            except PairingRefusedError as refused:
                results.append(refused)
        return results


def _fingerprint(ready: Sequence[Proposal]) -> str:
    digest = hashlib.sha256()
    for p in sorted(ready, key=lambda p: p.export_series_id):
        ref = p.candidates[0].ref
        digest.update(f"{p.export_series_id}|{ref.source_key}|{ref.remote_id}\n".encode())
    return digest.hexdigest()


def _checked(raw: str) -> str:
    """A watermark given by hand: a finite chapter number, in the range the naming accepts."""
    try:
        number = Decimal(raw.strip())
    except InvalidOperation:
        raise ValueError(f"not a chapter number: {raw!r}") from None
    if not number.is_finite() or number < 0 or number >= MAX_NUMBER:
        raise ValueError(f"not a chapter number: {raw!r}")
    return format(number, "f")
