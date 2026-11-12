"""M3 L21 (EF-BIB-028, GAP-8-05): the numbering of a team is checked before the election falls
back to it. A gap suspends the fallback, says why and opens a suggestion; the team that holds the
files keeps the Unit. Nothing is ever swapped silently."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
import structlog.testing

from shelfhold.acquisition.application.preference import TeamPreference, record_release
from shelfhold.catalog.public import BindingRef, IncomingUnit
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog
from tests.catalog_env import Harness, at, harness

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
LONG_AGO = NOW - timedelta(days=400)


@pytest.fixture
def h() -> Iterator[Harness]:
    with harness() as inside:
        yield inside


class Series:
    """Nine chapters of Alpha on disk, then chapter 10 offered by Alpha and by Beta. Alpha has
    been silent for more than 6 months: the default rule falls back to Beta."""

    def __init__(self, h: Harness, *, alpha_pages: int | None, beta_pages: int | None) -> None:
        self.h = h
        edition = h.edition()
        self.alpha: BindingRef = h.binding(edition)
        self.beta: BindingRef = h.binding(edition)
        for n in range(1, 10):
            result = self._ingest(self.alpha, "Alpha", n, None)
            self._fetched(result.variant_id, result.unit_id)
        self.alpha_ten = self._ingest(self.alpha, "Alpha", 10, alpha_pages)
        self.beta_ten = self._ingest(self.beta, "Beta", 10, beta_pages)
        self.unit_id = self.alpha_ten.unit_id
        assert self.beta_ten.unit_id == self.unit_id
        alpha_team, beta_team = self.alpha_ten.team_id, self.beta_ten.team_id
        assert alpha_team and beta_team
        self.alpha_team, self.beta_team = alpha_team, beta_team
        record_release(h.connection, alpha_team, LONG_AGO)
        record_release(h.connection, beta_team, NOW - timedelta(days=2))

    def _ingest(self, binding: BindingRef, team: str, number: int, pages: int | None):  # type: ignore[no-untyped-def]
        return self.h.catalog.ingest(
            IncomingUnit(
                binding=binding, remote_unit_id=f"{team}-{number}", raw_number=str(number),
                seen_at=at(number), source_order=number, team_label=team, page_count=pages,
            )
        )  # fmt: skip

    def _fetched(self, variant_id: str, unit_id: str) -> None:
        self.h.connection.execute(
            sa.insert(acquisition.download_request).values(
                id=new_ulid(), target_kind="unit", variant_id=variant_id, unit_id=unit_id,
                source_binding_id=self.alpha.id, state="fetched",
            )
        )  # fmt: skip

    def declare(self, *, alpha: int = 0, beta: int = 0) -> None:
        for team, offset in ((self.alpha_team, alpha), (self.beta_team, beta)):
            self.h.connection.execute(
                sa.text("UPDATE team SET number_offset = :o WHERE id = :t"),
                {"o": offset, "t": team},
            )

    def elect(self):  # type: ignore[no-untyped-def]
        with structlog.testing.capture_logs() as logs:
            election = TeamPreference().elect(self.h.connection, self.unit_id, NOW)
        return election, logs

    def suggestions(self) -> list[dict[str, object]]:
        return self.h.rows(
            "SELECT entity_type, reason, state FROM merge_suggestion ORDER BY reason"
        )


@pytest.mark.req("EF-BIB-028")
def test_team_offset_checked_before_fallback_agreeing_offer_falls_back(h: Harness) -> None:
    """Control: the same pages on both sides and a declared offset the evidence confirms: the
    fallback to Beta goes ahead and is logged, as before."""
    s = Series(h, alpha_pages=20, beta_pages=20)
    s.declare(beta=1)
    election, logs = s.elect()
    assert election is not None and election.variant_id == s.beta_ten.variant_id
    assert [e["reason"] for e in logs if e["event"] == "team.fallback"] == ["majority_inactive"]
    assert not [e for e in logs if e["event"] == "team.offset_suspended"]
    assert s.suggestions() == []


@pytest.mark.req("EF-BIB-028")
def test_team_offset_checked_before_fallback_inconsistent_offset_suspends(h: Harness) -> None:
    """A declared offset that the page counts contradict: the fallback is suspended, the team on
    disk keeps the Unit, and a suggestion says why."""
    s = Series(h, alpha_pages=20, beta_pages=31)
    s.declare(beta=1)
    election, logs = s.elect()
    assert election is not None and election.variant_id == s.alpha_ten.variant_id
    suspended = [e for e in logs if e["event"] == "team.offset_suspended"]
    assert [e["reason"] for e in suspended] == ["offset_inconsistent"]
    assert not [e for e in logs if e["event"] == "team.fallback"]  # silent swap: never
    assert s.suggestions() == [
        {"entity_type": "variant", "reason": "offset_inconsistent", "state": "open"}
    ]


@pytest.mark.req("EF-BIB-028")
def test_no_known_offset_and_a_gap_suspends_the_fallback(h: Harness) -> None:
    s = Series(h, alpha_pages=20, beta_pages=31)  # nobody declared an offset
    election, _logs = s.elect()
    assert election is not None and election.variant_id == s.alpha_ten.variant_id
    assert [r["reason"] for r in s.suggestions()] == ["offset_unexplained"]


@pytest.mark.req("EF-BIB-028")
def test_no_known_offset_and_no_gap_changes_nothing(h: Harness) -> None:
    """Nothing declared and nothing that differs: the numbering check does not get in the way."""
    for pages in ((20, 20), (None, None), (20, None)):
        with harness() as inside:
            s = Series(inside, alpha_pages=pages[0], beta_pages=pages[1])
            election, _logs = s.elect()
            assert election is not None and election.variant_id == s.beta_ten.variant_id


@pytest.mark.req("EF-BIB-028")
def test_a_declared_offset_nothing_confirms_suspends_the_fallback(h: Harness) -> None:
    s = Series(h, alpha_pages=None, beta_pages=None)
    s.declare(beta=3)
    election, _logs = s.elect()
    assert election is not None and election.variant_id == s.alpha_ten.variant_id
    assert [r["reason"] for r in s.suggestions()] == ["offset_unverified"]


@pytest.mark.req("EF-BIB-028")
def test_the_check_is_for_a_fallback_only(h: Harness) -> None:
    """A team that is not inactive keeps the Unit: Beta, whose pages differ, is never looked at,
    no suggestion is opened for a fallback that was never going to happen."""
    s = Series(h, alpha_pages=20, beta_pages=31)
    record_release(h.connection, s.alpha_team, NOW - timedelta(days=1))
    election, logs = s.elect()
    assert election is not None and election.variant_id == s.alpha_ten.variant_id
    assert not [e for e in logs if e["event"] == "team.offset_suspended"]
    assert s.suggestions() == []


@pytest.mark.req("EF-BIB-028")
def test_an_explicit_priority_is_not_a_fallback(h: Harness) -> None:
    """The admin's own list is never overruled by the numbering check."""
    s = Series(h, alpha_pages=20, beta_pages=31)
    h.catalog.set_team_priority(h.scalar("SELECT id FROM edition"), [s.beta_team])  # type: ignore[arg-type]
    election, _logs = s.elect()
    assert election is not None and election.variant_id == s.beta_ten.variant_id
    assert s.suggestions() == []


@pytest.mark.req("EF-BIB-028")
def test_a_suspended_fallback_to_the_only_other_team_leaves_the_unit_unasked(h: Harness) -> None:
    """The majority team does not offer the Unit (majority_absent): with a gap nothing explains,
    there is no reference Variant to point a suggestion at; the fallback is still suspended."""
    s = Series(h, alpha_pages=20, beta_pages=31)
    s.declare(beta=1)
    variant = catalog.unit_variant
    h.connection.execute(
        sa.update(variant).where(variant.c.id == s.alpha_ten.variant_id).values(removed_at=NOW)
    )
    election, logs = s.elect()
    assert election is None  # what is exported so far is left as it is
    assert [e["reason"] for e in logs if e["event"] == "team.offset_suspended"] == [
        "offset_unverified"
    ]
    assert s.suggestions() == []
