"""Review of PR #60: the watermark, the races, the purged chapters and the batch (EF-EXP-038,
EF-EXP-053, EF-BIB-053)."""

import pytest
import sqlalchemy as sa

from shelfhold.export.application.pairing import (
    AlreadyPairedError,
    UpstreamAlreadyFollowedError,
    WatermarkConfirmationRequired,
)
from tests.pairing_env import SERIES, Paired, PurgedPlaces, paired_env

CHAPTERS = [f"{SERIES} - Ch.{n}.cbz" for n in range(1, 51)]


def _first(p: Paired):  # type: ignore[no-untyped-def]
    (proposal,) = p.pairing.propose(p.destination, p.library())
    return proposal, proposal.candidates[0].ref


@pytest.mark.req("EF-EXP-053", "EF-BIB-050")
def test_watermark_is_the_segment_and_number_pair() -> None:
    with paired_env() as p:
        p.adopt_files(CHAPTERS)
        p.upstream(51)
        p.a.adopt()
        with p.env.engine.begin() as c:  # chapter 5 lives in a later numbering segment
            c.execute(
                sa.text(
                    "UPDATE unit SET number_segment='S2', number_segment_ordinal=2 "
                    "WHERE number_norm='5'"
                )
            )
        proposal, ref = _first(p)
        assert (proposal.follow_from_segment, proposal.follow_from) == ("S2", "5")
        done = p.pairing.confirm(p.destination, proposal.export_series_id, ref)
        assert done.follow_from_segment == "S2"
        (row,) = p.rows(
            "SELECT follow_from_segment AS s, follow_from_number AS n FROM series_follow"
        )[-1:]
        assert (row["s"], row["n"]) == ("S2", "5")
        assert p.settle(6) == 0 and p.downloads() == []  # nothing already adopted is asked for


@pytest.mark.req("EF-EXP-038")
def test_two_confirmations_racing_leave_one_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    with paired_env() as p:
        p.adopt_files(CHAPTERS[:3])
        p.upstream(5)
        p.a.adopt()
        proposal, ref = _first(p)
        real = p.preflight.check_series
        fired: list[int] = []

        def check(destination_id: str, series_id: str):  # type: ignore[no-untyped-def]
            result = real(destination_id, series_id)
            if not fired:  # the other admin confirms between our checks and our write
                fired.append(1)
                p.pairing.confirm(destination_id, series_id, ref)
            return result

        monkeypatch.setattr(p.preflight, "check_series", check)
        with pytest.raises(AlreadyPairedError):
            p.pairing.confirm(p.destination, proposal.export_series_id, ref)
        assert (
            p.rows("SELECT count(*) AS n FROM source_binding WHERE linked_from_adoption")[0]["n"]
            == 1
        )


@pytest.mark.req("EF-EXP-038")
def test_one_upstream_series_is_never_followed_for_two_editions() -> None:
    with paired_env() as p:
        p.adopt_files(CHAPTERS[:3])
        p.a.add_series("Series Beta", [f"Series Beta - Ch.{n}.cbz" for n in (1, 2, 3)])
        p.upstream(5)
        p.a.adopt()
        props = p.pairing.propose(p.destination, p.library())
        alpha = next(x for x in props if x.series_name == SERIES)
        beta = next(x for x in props if x.series_name == "Series Beta")
        ref = alpha.candidates[0].ref
        p.pairing.confirm(p.destination, alpha.export_series_id, ref)
        with pytest.raises(UpstreamAlreadyFollowedError):
            p.pairing.confirm(p.destination, beta.export_series_id, ref)
        assert (
            p.rows("SELECT count(*) AS n FROM source_binding WHERE linked_from_adoption")[0]["n"]
            == 1
        )


@pytest.mark.req("EF-BIB-053", "EF-EXP-053")
def test_purged_chapter_above_the_adopted_ones_counts_under_the_watermark() -> None:
    with paired_env(tombstones=PurgedPlaces("50")) as p:
        p.adopt_files(CHAPTERS[:49])  # chapter 50 was purged: its Unit is gone
        p.upstream(51)
        p.a.adopt()
        proposal, ref = _first(p)
        assert proposal.follow_from == "50" and proposal.gaps == 0
        p.pairing.confirm(p.destination, proposal.export_series_id, ref)
        assert p.settle() == 1 and len(p.downloads()) == 1 and "/5510/" in p.downloads()[0]


@pytest.mark.req("EF-EXP-053")
def test_proposal_shows_the_holes_under_the_watermark() -> None:
    with paired_env() as p:
        names = [f"{SERIES} - Ch.{n}.cbz" for n in [*range(1, 11), *range(20, 51)]]
        p.adopt_files(names)
        p.upstream(51)
        p.a.adopt()
        proposal, _ = _first(p)
        assert proposal.follow_from == "50" and proposal.gaps == 9  # 11 to 19


@pytest.mark.req("EF-EXP-053")
def test_set_follow_from_below_the_adopted_chapters_needs_the_backfill_confirmation() -> None:
    with paired_env() as p:
        p.adopt_files([f"{SERIES} - Ch.{n}.cbz" for n in range(1, 131)])
        p.upstream(131)
        p.a.adopt()
        proposal, ref = _first(p)
        with pytest.raises(WatermarkConfirmationRequired) as asked:
            p.pairing.confirm(p.destination, proposal.export_series_id, ref, follow_from="5")
        assert asked.value.units == 125 and asked.value.code == "backfill.confirmation_required"
        assert p.rows("SELECT * FROM source_binding WHERE linked_from_adoption") == []
        done = p.pairing.confirm(
            p.destination, proposal.export_series_id, ref, follow_from="5", confirm_backfill=125
        )
        assert done.paired and done.follow_from == "5"
