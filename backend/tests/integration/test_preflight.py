"""L30: the permission preflight and the series that wait for a human (EF-EXP-054, EF-EXP-051).

A file Shelfhold cannot write is a state to leave with `target recheck`, never a retry loop.
"""

import errno
import os
import stat
from pathlib import Path

import pytest

from shelfhold.export.application.migration_detect import SwitchoverDetector
from shelfhold.export.application.pairing import PairingRefusedError
from shelfhold.export.application.preflight import DestinationNotWritableError
from tests.adopt_env import tree_state
from tests.pairing_env import SERIES, Paired, paired_env

CHAPTERS = [f"{SERIES} - Ch.{n}.cbz" for n in range(1, 4)]


def _deny(p: Paired, monkeypatch: pytest.MonkeyPatch, folder: Path) -> list[str]:
    """The folder refuses every write (also as root, which ignores the mode): EACCES."""
    refused: list[str] = []
    real = p.env.lib.fsops.write_atomic

    def write_atomic(local: str, *args: object, **kwargs: object) -> object:
        if local.startswith(str(folder)):
            refused.append(local)
            raise PermissionError(errno.EACCES, "Permission denied", local)
        return real(local, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(p.env.lib.fsops, "write_atomic", write_atomic)
    return refused


def _undeny(p: Paired, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()


def _ready(p: Paired) -> str:
    p.adopt_files(CHAPTERS)
    p.upstream(4)
    p.a.adopt()
    (proposal,) = p.pairing.propose(p.destination, p.library())
    p.pairing.confirm(p.destination, proposal.export_series_id, proposal.candidates[0].ref)
    return proposal.export_series_id


@pytest.mark.req("EF-EXP-054")
def test_pairing_refused_when_series_dir_not_writable_with_fix_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with paired_env() as p:
        p.adopt_files(CHAPTERS)
        p.upstream(4)
        p.a.adopt()
        original = stat.S_IMODE(p.folder.stat().st_mode)
        p.folder.chmod(0o555)  # another uid's folder: read and enter, not write
        try:
            _deny(p, monkeypatch, p.folder)
            (proposal,) = p.pairing.propose(p.destination, p.library())
            before = sorted(os.listdir(p.folder))
            with pytest.raises(PairingRefusedError) as refused:
                p.pairing.confirm(
                    p.destination, proposal.export_series_id, proposal.candidates[0].ref
                )
            probe = refused.value.probe
            assert not probe.ok and probe.cause == "EACCES" and probe.failed_step == "write"
            assert probe.owner is not None and probe.owner.mode == 0o555
            assert probe.runs_as == (os.geteuid(), os.getegid())
            message = str(refused.value)
            assert str(p.folder) in message and "EACCES" in message
            assert f"setfacl -R -m u:{os.geteuid()}:rwX" in message  # the exact command to run
            assert "group_add" in message or "user:" in message  # and the compose alternatives
            assert sorted(os.listdir(p.folder)) == before  # no probe file left behind
            assert p.rows("SELECT * FROM source_binding WHERE linked_from_adoption") == []
            assert p.rows("SELECT * FROM series_follow WHERE follow_from_number IS NOT NULL") == []
        finally:
            p.folder.chmod(original)


@pytest.mark.req("EF-EXP-054")
def test_write_eacces_sets_permission_denied_and_recheck_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with paired_env() as p:
        series_id = _ready(p)
        assert p.settle() == 1  # chapter 4 is received
        refused = _deny(p, monkeypatch, p.folder)
        p.env.pipeline.run(p.destination, job_id="01J00000000000000000000002")
        (row,) = p.rows("SELECT paused_reason FROM export_series WHERE id = :i", i=series_id)
        assert row["paused_reason"] == "permission_denied"
        (event,) = p.events("target.permission_denied")
        assert series_id in str(event["payload"])
        calls = len(refused)
        p.env.pipeline.run(p.destination, job_id="01J00000000000000000000003")  # no retry loop
        assert len(refused) == calls and len(p.events("target.permission_denied")) == 1
        still = p.preflight.recheck(p.destination)  # rights not restored: still waiting
        assert still.cleared == () and still.denied == (series_id,)
        assert len(p.events("target.permission_denied")) == 1  # not announced again
        monkeypatch.undo()  # the admin fixed the rights
        done = p.preflight.recheck(p.destination)
        assert done.cleared == (series_id,) and done.denied == ()
        p.env.pipeline.run(p.destination, job_id="01J00000000000000000000004")
        assert (p.folder / f"{SERIES} - Ch.4.cbz").exists()
        assert sorted(f for f in os.listdir(p.folder) if f.startswith(".shelfhold-probe")) == []


@pytest.mark.req("EF-EXP-054")
def test_preflight_checks_the_destination_root_before_any_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with paired_env() as p:
        _ready(p)
        assert p.settle() == 1
        refused = _deny(p, monkeypatch, p.env.root)
        with pytest.raises(DestinationNotWritableError) as caught:
            p.env.pipeline.run(p.destination, job_id="01J00000000000000000000005")
        assert caught.value.probe.cause == "EACCES" and caught.value.probe.failed_step == "write"
        assert refused and all(".shelfhold-probe-" in path for path in refused)  # only the probe
        assert p.rows("SELECT * FROM fs_intent") == []  # nothing was planned or started


@pytest.mark.req("EF-EXP-051", "EF-EXP-044")
def test_download_dir_already_under_library_detected_no_copy() -> None:
    with paired_env() as p:
        p.adopt_files(CHAPTERS)
        p.a.adopt()
        before = p.a.state()
        everything = tree_state(p.env.lib.tmp)
        inside = p.env.lib.manga / "suwayomi-downloads"  # a host folder under the library
        verdict = p.detector.classify_download_dir(p.destination, str(inside))
        assert verdict.kind == "under_library" and not verdict.copy_needed
        assert "Suwayomi" in verdict.action  # change the folder there, the files stay in place
        outside = p.env.lib.tmp / "suwayomi-volume"
        away = p.detector.classify_download_dir(p.destination, str(outside))
        assert away.kind == "outside_library" and away.copy_needed
        assert p.a.state() == before and tree_state(p.env.lib.tmp) == everything  # no copy, no move


@pytest.mark.req("EF-EXP-044")
def test_detector_leaves_our_own_series_alone() -> None:
    with paired_env() as p:
        p.adopt_files(CHAPTERS)  # "<series> - Ch.N", ComicInfo without Suwayomi's fields
        p.a.adopt()
        assert SwitchoverDetector(p.env.engine, p.env.lib.fsops).scan(p.destination).series == ()
