"""L30: pairing an adopted series with its Suwayomi follow, never silently and never twice
(EF-EXP-038, EF-EXP-053).

The 50 chapters Kavita already has are never asked for again; the 51st lands in the folder of the
adopted series, and Kavita still has one series.
"""

import pytest

from shelfhold.export.application.migration_detect import SwitchoverUnconfirmed
from shelfhold.export.application.pairing import AlreadyPairedError
from tests.adopt_env import tree_state
from tests.kavita_export_env import LIBRARY_ID, REMOTE_ROOT
from tests.pairing_env import SERIES, Paired, paired_env

CHAPTERS = [f"{SERIES} - Ch.{n}.cbz" for n in range(1, 51)]


def _pair(p: Paired) -> str:
    """What the admin does: look at the proposal, then confirm it. Returns the export series."""
    (proposal,) = p.pairing.propose(p.destination, p.library())
    assert proposal.state == "ready"
    (best, *others) = proposal.candidates
    assert best.ref.remote_id == "/series-alpha" and best.score == 1.0 and not others
    p.pairing.confirm(p.destination, proposal.export_series_id, best.ref)
    return proposal.export_series_id


@pytest.mark.req("EF-EXP-038", "EF-EXP-053")
def test_adopted_series_linked_to_suwayomi_no_redownload() -> None:
    with paired_env() as p:
        p.adopt_files(CHAPTERS)
        p.upstream(51)
        p.a.adopt()
        before = tree_state(p.folder)
        # nothing is paired by itself: the adoption alone did not bind the series
        assert (
            p.rows("SELECT count(*) AS n FROM series_follow WHERE follow_from_number = '50'")[0][
                "n"
            ]
            == 0
        )
        _pair(p)
        (follow,) = p.rows("SELECT follow_from_number, follow_from_segment FROM series_follow")[-1:]
        assert (follow["follow_from_number"], follow["follow_from_segment"]) == ("50", "")
        (binding,) = p.rows("SELECT linked_from_adoption, active FROM source_binding")[-1:]
        assert binding["linked_from_adoption"] and binding["active"]
        p.cycle_once()
        assert len(p.downloads()) == 1 and "/5510/" in p.downloads()[0]  # chapter 51 only
        p.env.pipeline.run(p.destination, job_id="01J00000000000000000000001")
        new = sorted(r["rel_path"] for r in p.rows("SELECT rel_path FROM export_record"))
        written = [n for n in new if n not in {f"{SERIES}/{c}" for c in CHAPTERS}]
        assert written == [f"{SERIES}/{SERIES} - Ch.51.cbz"]  # one file, in the adopted folder
        assert tree_state(p.folder).items() >= before.items()  # the 50 are untouched
        assert len(list(p.folder.iterdir())) == 51
        p.a.wired.fake.discover(
            f"{REMOTE_ROOT}/{SERIES}",
            [f"{REMOTE_ROOT}/{SERIES}/{SERIES} - Ch.51.cbz"],
            library_id=LIBRARY_ID,
        )
        assert len(p.a.wired.fake.series) == 1  # Kavita: still one series, no duplicate


@pytest.mark.req("EF-EXP-038")
def test_pairing_is_proposed_never_silent_and_never_twice() -> None:
    with paired_env() as p:
        p.adopt_files(CHAPTERS)
        p.upstream(51)
        p.a.adopt()
        assert (
            p.rows("SELECT count(*) AS n FROM source_binding WHERE linked_from_adoption")[0]["n"]
            == 0
        )
        series_id = _pair(p)
        with pytest.raises(AlreadyPairedError):
            (proposal,) = p.pairing.propose(p.destination, p.library())
            p.pairing.confirm(p.destination, series_id, proposal.candidates[0].ref)


@pytest.mark.req("EF-EXP-053", "EF-EXP-038")
def test_adopted_volume_only_series_is_not_paired_without_a_chapter() -> None:
    with paired_env() as p:
        p.adopt_files([f"{SERIES} - Vol.{n}.cbz" for n in (1, 2)])
        p.upstream(51)
        p.a.adopt()
        (proposal,) = p.pairing.propose(p.destination, p.library())
        assert proposal.state == "coverage_unknown"
        assert "set-follow-from" in proposal.exit_command
        result = p.pairing.confirm(
            p.destination, proposal.export_series_id, proposal.candidates[0].ref
        )
        assert result.state == "coverage_unknown" and not result.paired
        assert p.cycle_once().fetched == 0 and p.downloads() == []
        done = p.pairing.confirm(
            p.destination, proposal.export_series_id, proposal.candidates[0].ref, follow_from="50"
        )
        assert done.paired


@pytest.mark.req("EF-EXP-044")
def test_adoption_detects_direct_suwayomi_feed_and_refuses_follow() -> None:
    with paired_env() as p:
        suwayomi_info = (
            b'<?xml version="1.0" encoding="utf-8"?><ComicInfo><Series>Series Alpha</Series>'
            b"<LanguageISO>en</LanguageISO><Translator>Team</Translator>"
            b"<Web>/series-alpha/ch-001</Web></ComicInfo>"
        )
        p.adopt_files([f"Team_Chapter {n}.cbz" for n in (1, 2, 3)], info=suwayomi_info)
        p.upstream(5)
        p.a.adopt()
        report = p.detector.scan(p.destination)
        (finding,) = report.series
        assert finding.series_dir == SERIES and {"name_motif", "comicinfo"} <= set(finding.signals)
        (proposal,) = p.pairing.propose(p.destination, p.library())
        with pytest.raises(SwitchoverUnconfirmed) as refused:
            p.pairing.confirm(p.destination, proposal.export_series_id, proposal.candidates[0].ref)
        assert refused.value.procedure == "docs/admin/migration-depuis-scripts.md"
        assert p.rows("SELECT * FROM series_follow WHERE follow_from_number IS NOT NULL") == []
        assert p.rows("SELECT * FROM source_binding WHERE linked_from_adoption") == []
        assert p.cycle_once().fetched == 0 and p.downloads() == []  # the follow did not start


@pytest.mark.req("EF-EXP-044")
def test_switchover_confirmation_needs_every_step_then_follow_starts() -> None:
    from shelfhold.export.application.migration_detect import STEPS, SwitchoverIncompleteError

    with paired_env() as p:
        info = (
            b'<?xml version="1.0" encoding="utf-8"?><ComicInfo><Series>Series Alpha</Series>'
            b"<LanguageISO>en</LanguageISO><Translator>Team</Translator></ComicInfo>"
        )
        p.adopt_files([f"Team_Chapter {n}.cbz" for n in (1, 2, 3)], info=info)
        p.upstream(5)
        p.a.adopt()
        with pytest.raises(SwitchoverIncompleteError) as partial:
            p.gate.confirm(p.destination, STEPS[:4])
        assert partial.value.missing == (STEPS[4],)
        p.gate.confirm(p.destination, STEPS)
        (proposal,) = p.pairing.propose(p.destination, p.library())
        done = p.pairing.confirm(
            p.destination, proposal.export_series_id, proposal.candidates[0].ref
        )
        assert done.paired
        p.cycle_once()
        assert len(p.downloads()) == 2  # chapters 4 and 5, none of the three adopted ones
