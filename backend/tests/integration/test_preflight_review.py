"""Review of PR #60: the switchover gate, the probe and the waiting state (EF-EXP-044,
EF-EXP-054)."""

import errno
import os
from pathlib import Path

import pytest

from shelfhold.export.application.migration_detect import (
    STEPS,
    SwitchoverStale,
    SwitchoverUnconfirmed,
)
from shelfhold.export.application.pairing import PairingRefusedError
from shelfhold.export.application.preflight import (
    DestinationNotWritableError,
    Owner,
    check_access,
    fix_commands,
)
from shelfhold.export.application.reconcile import Reconciler
from tests.integration.test_preflight import _deny, _ready
from tests.pairing_env import SERIES, paired_env

SUWAYOMI_INFO = (
    b'<?xml version="1.0" encoding="utf-8"?><ComicInfo><Series>Series Beta</Series>'
    b"<LanguageISO>en</LanguageISO><Translator>Team</Translator></ComicInfo>"
)
CHAPTERS = [f"{SERIES} - Ch.{n}.cbz" for n in range(1, 4)]


@pytest.mark.req("EF-EXP-044")
def test_a_series_name_with_an_underscore_is_not_a_suwayomi_feed() -> None:
    with paired_env() as p:
        p.adopt_files([f"Series_Alpha - Ch.{n}.cbz" for n in (1, 2, 3)])
        p.a.adopt()
        assert p.detector.scan(p.destination).series == ()


@pytest.mark.req("EF-EXP-044")
def test_switchover_confirmation_is_tied_to_the_report_and_its_series() -> None:
    with paired_env() as p:
        p.adopt_files(CHAPTERS)
        p.a.add_series(
            "Series Beta", {f"Team_Ch.{n}.cbz": None for n in (1, 2)}, info=SUWAYOMI_INFO
        )
        p.a.adopt()
        report = p.gate.report(p.destination)
        assert [f.series_dir for f in report.series] == ["Series Beta"]
        with pytest.raises(SwitchoverStale):
            p.gate.confirm(p.destination, STEPS, fingerprint="not-the-report-seen")
        p.gate.confirm(p.destination, STEPS, fingerprint=report.fingerprint)
        p.gate.require_clear(p.destination)  # Beta is covered
        p.a.add_series(
            "Series Gamma",
            {f"Crew_Ch.{n}.cbz": None for n in (1, 2)},
            info=SUWAYOMI_INFO.replace(b"Series Beta", b"Series Gamma"),
        )
        p.a.adopt()  # a second series fed directly appears after the confirmation
        with pytest.raises(SwitchoverUnconfirmed) as again:
            p.gate.require_clear(p.destination)
        assert [f.series_dir for f in again.value.report.series] == ["Series Gamma"]


@pytest.mark.req("EF-EXP-044")
def test_download_dir_pointing_at_the_series_folder_is_a_signal() -> None:
    with paired_env() as p:
        p.adopt_files(CHAPTERS)
        p.a.adopt()
        assert p.gate.report(p.destination).series == ()
        p.gate.declare_download_dir(p.destination, str(p.env.root))  # Suwayomi writes here
        (found,) = p.gate.report(p.destination).series
        assert found.signals == ("download_dir",)


@pytest.mark.req("EF-EXP-044")
def test_pipeline_refuses_to_export_a_shelf_fed_directly_until_confirmed() -> None:
    with paired_env() as p:
        p.a.add_series(
            "Series Beta", {f"Team_Ch.{n}.cbz": None for n in (1, 2)}, info=SUWAYOMI_INFO
        )
        p.a.adopt()
        with pytest.raises(SwitchoverUnconfirmed):
            p.env.pipeline.run(p.destination, job_id="01J00000000000000000000010")
        p.gate.confirm(p.destination, STEPS, fingerprint=p.gate.report(p.destination).fingerprint)
        p.env.pipeline.run(p.destination, job_id="01J00000000000000000000011")


@pytest.mark.req("EF-EXP-054")
def test_probe_leftovers_are_swept_and_only_those() -> None:
    with paired_env() as p:
        p.adopt_files(CHAPTERS)
        p.a.adopt()
        stale = p.folder / ".shelfhold-probe-0123456789abcdef"
        stale.write_bytes(b"crash")
        other = p.env.root / ".shelfhold-probe-fedcba9876543210"
        other.write_bytes(b"crash")
        not_ours = p.folder / ".shelfhold-probe-notsixteenhex"
        not_ours.write_bytes(b"theirs")
        folder = p.folder / ".shelfhold-probe-aaaaaaaaaaaaaaaa"
        folder.mkdir()
        assert p.preflight.sweep(p.destination) == 2
        assert not stale.exists() and not other.exists()
        assert not_ours.exists() and folder.is_dir()


@pytest.mark.req("EF-EXP-054")
def test_missing_folder_is_reported_as_such_without_an_acl_command() -> None:
    with paired_env() as p:
        p.adopt_files(CHAPTERS)
        p.upstream(5)
        p.a.adopt()
        probe = p.preflight.probe(str(p.env.root / "nowhere"))
        assert not probe.ok and probe.cause == "folder_missing"
        assert "setfacl" not in probe.explain() and "does not exist" in probe.explain()
        proposal, ref = (lambda x: (x[0], x[0].candidates[0].ref))(
            p.pairing.propose(p.destination, p.library())
        )
        for item in p.folder.iterdir():
            item.unlink()
        p.folder.rmdir()  # the folder of the adopted series is gone
        with pytest.raises(PairingRefusedError) as refused:
            p.pairing.confirm(p.destination, proposal.export_series_id, ref)
        assert refused.value.probe.cause == "folder_missing"


@pytest.mark.req("EF-EXP-054")
def test_pairing_a_series_with_no_folder_on_record_is_refused() -> None:
    with paired_env() as p:
        p.adopt_files(CHAPTERS)
        p.upstream(5)
        p.a.adopt()
        proposal = p.pairing.propose(p.destination, p.library())[0]
        with p.env.engine.begin() as c:
            c.exec_driver_sql("UPDATE export_series SET series_dir = NULL")
        with pytest.raises(PairingRefusedError) as refused:
            p.pairing.confirm(p.destination, proposal.export_series_id, proposal.candidates[0].ref)
        assert refused.value.probe.cause == "folder_missing"


@pytest.mark.req("EF-EXP-054")
def test_a_permission_error_on_the_source_is_not_blamed_on_the_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with paired_env() as p:
        series_id = _ready(p)
        assert p.settle() == 1

        def boom(*args: object, **kwargs: object) -> object:
            raise PermissionError(errno.EACCES, "Permission denied", "/data/.received/x.cbz")

        monkeypatch.setattr(p.env.pipeline._renderer, "render", boom)
        with pytest.raises(PermissionError):
            p.env.pipeline.run(p.destination, job_id="01J00000000000000000000012")
        (row,) = p.rows("SELECT paused_reason FROM export_series WHERE id = :i", i=series_id)
        assert row["paused_reason"] is None and p.events("target.permission_denied") == []


@pytest.mark.req("EF-EXP-054", "EF-EXP-043")
def test_series_resume_probes_a_permission_denied_series_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with paired_env() as p:
        series_id = _ready(p)
        assert p.settle() == 1
        _deny(p, monkeypatch, p.folder)
        p.env.pipeline.run(p.destination, job_id="01J00000000000000000000013")
        rec = Reconciler(p.env.engine, p.env.lib.fsops, clock=p.env.clock, bus=p.bus)
        with pytest.raises(DestinationNotWritableError) as still:
            rec.resume_series(series_id)
        assert still.value.probe.cause == "EACCES"
        (row,) = p.rows("SELECT paused_reason FROM export_series WHERE id = :i", i=series_id)
        assert row["paused_reason"] == "permission_denied"  # still waiting, nothing freed
        monkeypatch.undo()
        assert rec.resume_series(series_id) is True


@pytest.mark.req("EF-EXP-038", "EF-EXP-054")
def test_pair_auto_batch_is_all_or_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    with paired_env() as p:
        p.adopt_files(CHAPTERS)
        p.a.add_series("Series Beta", [f"Series Beta - Ch.{n}.cbz" for n in (1, 2, 3)])
        p.upstream(5)
        p.a.adopt()
        library = p.library()
        report = p.pairing.pair_auto(p.destination, library)
        assert len(report.ready) == 2
        _deny(p, monkeypatch, p.env.root / "Series Beta")
        batch = p.pairing.confirm_batch(p.destination, library, fingerprint=report.fingerprint)
        assert not batch.complete and batch.paired == () and len(batch.refused) == 1
        assert p.rows("SELECT * FROM source_binding WHERE linked_from_adoption") == []  # none


def _read_only_mount() -> str | None:
    for line in Path("/proc/mounts").read_text().splitlines():
        _dev, point, _type, options, *_ = line.split()
        if "ro" in options.split(",") and Path(point).is_dir() and point.startswith("/"):
            try:
                if os.statvfs(point).f_flag & os.ST_RDONLY:
                    return point
            except OSError:
                continue
    return None


@pytest.mark.req("EF-UX-002")
def test_a_read_only_volume_is_told_apart_and_the_fix_is_to_drop_ro() -> None:
    mount = _read_only_mount()
    if mount is None:
        pytest.skip("no read-only mount on this machine")
    refused = check_access(mount + "/shelf-to-make")
    assert refused is not None and refused.cause == "EROFS"
    text = refused.explain()
    assert "read-only" in text and "':ro'" in text
    assert "setfacl" not in text  # rights are not the problem


@pytest.mark.req("EF-UX-002")
def test_the_compose_block_never_names_root() -> None:
    root_owned = fix_commands("/library/manga", (1000, 1000), Owner(0, 0, 0o755))
    assert "user:" not in root_owned and "group_add" not in root_owned
    assert 'user: "0' not in root_owned
    group_only = fix_commands("/library/manga", (1000, 1000), Owner(0, 50, 0o775))
    assert "group_add:" in group_only and '- "50"' in group_only and "user:" not in group_only
    owned = fix_commands("/library/manga", (1000, 1000), Owner(1026, 100, 0o755))
    assert 'user: "1026:100"' in owned
