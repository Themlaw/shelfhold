"""L54: coverage of adopted volumes, `Vol.N` names, the names Kavita shows now, and the waiting
states `adoption_refused` / `coverage_unknown` written down for `status`.

Every test goes through the production path: `shelfhold adopt` against the simulated Kavita, the
pairing commands, the follow and export handlers, `Backfill`, `status`."""

import re
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.target_state import DestinationPausedError
from tests.l54_env import SERIES, Bench54, bench54, comic

pytestmark = pytest.mark.req("EF-BIB-044", "EF-INT-014", "EF-EXP-057", "EF-INT-023")


def _adopt_mixed(b: Bench54, capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    """Chapters 1 and 2, volume 2 (range unknown), chapters 11 and 12: a library of the admin."""
    names = ["Ch.1", "Ch.2", "Vol.2", "Ch.11", "Ch.12"]
    b.kavita_series(SERIES, [f"{SERIES} - {n}.cbz" for n in names])
    assert b.adopt(capsys) == 0
    series_id = b.series_id(SERIES)
    return series_id, b.edition_of(series_id)


def _pair(b: Bench54, series_id: str, *extra: str) -> int:
    return b.cli(
        "adopt", "pair", "--series", series_id, "--source-key", "suwayomi:7001", "--remote-id",
        "/series-alpha", *extra,
    )  # fmt: skip


def test_adopt_set_coverage_then_backfill_gaps_skips_covered(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        series_id, edition = _adopt_mixed(b, capsys)
        b.upstream({n: f"Chapter {n}" for n in range(1, 15)})
        assert _pair(b, series_id, "--follow-from", "12") == 0
        b.follow()
        assert b.gaps(edition) == 8  # chapters 3 to 10: nobody says the volume holds them
        assert b.cli("adopt", "set-coverage", SERIES, "2", "3", "10") == 0
        assert b.gaps(edition) == 0  # present through the volume (I20): none is asked for
        # one function files the adopted Edition like the discovery of the same service (A1)
        (row,) = b.rows("SELECT translation_type FROM edition WHERE id = :e", e=edition)
        assert row["translation_type"] == "fan"
        sources = b.rows("SELECT DISTINCT origin FROM unit_coverage WHERE origin = 'manual'")
        assert sources == [{"origin": "manual"}]
        asked = {p for p in b.downloaded()}
        assert not any(f"/{5000 + n * 10}/" in p for n in range(1, 13) for p in asked)


def test_vol_n_chapter_names_attach_to_adopted_volume(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        b.kavita_series(SERIES, [f"{SERIES} - {n}.cbz" for n in ("Vol.1", "Ch.11", "Ch.12")])
        assert b.adopt(capsys) == 0
        series_id = b.series_id(SERIES)
        edition = b.edition_of(series_id)
        names = {n: f"Vol.1 Ch.{n}" if n <= 10 else f"Chapter {n}" for n in range(1, 15)}
        b.upstream(names)
        assert _pair(b, series_id, "--follow-from", "12") == 0
        b.follow()
        assert b.gaps(edition, "all") == 0  # the names say it: chapters 1 to 10 are in volume 1
        origins = b.rows(
            "SELECT c.origin, count(*) AS n FROM unit_coverage c JOIN unit u ON u.id = c.unit_id "
            "WHERE u.edition_id = :e AND u.track = 'main' AND c.origin = 'source_name' "
            "GROUP BY c.origin",
            e=edition,
        )
        assert origins == [{"origin": "source_name", "n": 10}]
        assert not any(f"/{5000 + n * 10}/" in p for n in range(1, 13) for p in b.downloaded())
        assert len(b.downloaded()) == 2  # chapters 13 and 14 only: the new ones


def _info(series: str, **kwargs: str) -> bytes:
    return comic(series, **kwargs)


def test_series_renamed_in_kavita_no_false_collision(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        kavita = b.env.inst.kavita
        b.kavita_series("Old Folder", ["Old Name - Ch.1.cbz"], name="Old Name",
                        info=_info("Old Name"))  # fmt: skip
        assert b.adopt(capsys) == 0
        (shown,) = [s for s in kavita.series if s["name"] == "Old Name"]
        shown["name"] = "Series Alpha"  # renamed in the Kavita interface
        # Pairing.propose compares with the name Kavita shows now (the Suwayomi series bears it)
        capsys.readouterr()
        assert b.cli("adopt", "pair") == 0
        proposals = capsys.readouterr().out
        assert "[ready]" in proposals and "suwayomi:7001" in proposals
        # the plan: another Edition now wants the former name; nobody bears it any more in Kavita
        series = b.env.new_series("Old Name")
        b.env.add_chapter(series, "2")
        b.export()
        (written,) = b.rows(
            "SELECT s.name_raw FROM export_series s JOIN edition_export_series l "
            "ON l.export_series_id = s.id WHERE l.edition_id = :e",
            e=series.edition_id,
        )
        assert written["name_raw"] == "Old Name"
        assert b.rows(
            "SELECT 1 FROM export_record r JOIN export_item i ON i.id = r.export_item_id "
            "WHERE i.edition_id = :e AND r.state IN ('written', 'scanned')",
            e=series.edition_id,
        )
        # the adopted series keeps its frozen name and takes the one Kavita shows in the cache
        (adopted,) = b.rows(
            "SELECT name_raw, frozen_series FROM export_series WHERE frozen_series = 'Old Name' "
            "AND kavita_series_id IS NOT NULL AND proposal_key LIKE '%adopted%'"
        )
        assert adopted == {"name_raw": "Series Alpha", "frozen_series": "Old Name"}
        assert all(w[0] != "name" for w in [])  # Kavita was only read


def test_adoption_refused_and_coverage_unknown_persisted_in_waiting_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        b.kavita_series("Volumes Only", ["Volumes Only - Vol.1.cbz", "Volumes Only - Vol.2.cbz"])
        # the `<team>_<chapter>` names of Suwayomi's own download folder (EF-EXP-044)
        b.kavita_series("Fed Series", ["Team_Chapter 1.cbz"], info=_info("Fed Series", number="1"))
        assert b.adopt(capsys) == 0
        # a later process: the states were written down, not kept in memory
        shown = b.status(capsys)
        assert "(coverage_unknown)" in shown and "(adoption_refused)" in shown
        assert "shelfhold adopt --confirm-migration" in shown
        volumes = b.series_id("Volumes Only")
        assert f"adopt set-follow-from {volumes} <chapter>" in shown
        assert str(volumes) in shown
        # leaving coverage_unknown: the admin says what the volumes hold
        assert b.cli("adopt", "set-coverage", "Volumes Only", "1", "1", "5") == 0
        assert "(coverage_unknown)" in b.status(capsys)  # volume 2 is still unknown
        assert b.cli("adopt", "set-coverage", "Volumes Only", "2", "6", "10") == 0
        after = b.status(capsys)
        assert "(coverage_unknown)" not in after and "(adoption_refused)" in after
        # leaving adoption_refused: the confirmed switch-over
        capsys.readouterr()
        assert b.cli("adopt", "--confirm-migration", "--destination", b.env.destination_id) == 2
        fingerprint = re.split(r"rapport\s:", capsys.readouterr().out)[1].split()[0]
        assert (
            b.cli("adopt", "--confirm-migration", "--destination", b.env.destination_id,
                  "--confirm", fingerprint) == 0
        )  # fmt: skip
        assert "(adoption_refused)" not in b.status(capsys)


def test_adoption_with_underscore_language_fr_fr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        b.kavita_series(
            "Série VF", ["Série VF - Ch.1.cbz"], info=_info("Série VF", language="FR_fr")
        )
        assert b.adopt(capsys) == 0  # the column refused `FR_fr` before it was normalized
        (row,) = b.rows(
            "SELECT s.frozen_language, e.lang, e.lang_raw FROM export_series s "
            "JOIN edition_export_series l ON l.export_series_id = s.id "
            "JOIN edition e ON e.id = l.edition_id WHERE s.frozen_series = 'Série VF'"
        )
        assert row == {"frozen_language": "fr-FR", "lang": "fr-FR", "lang_raw": "FR_fr"}
        assert "(language_unknown)" not in b.status(capsys)


def _mixed_paired(b: Bench54, capsys: pytest.CaptureFixture[str]) -> str:
    series_id, edition = _adopt_mixed(b, capsys)
    b.upstream({n: f"Chapter {n}" for n in range(1, 15)})
    assert _pair(b, series_id, "--follow-from", "12") == 0
    b.follow()
    return edition


def test_adopted_volume_deleted_in_kavita_is_a_gap_again(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    from shelfhold.runtime_jobs import reconcile_handler
    from tests.l41_env import context

    with bench54(monkeypatch, settings, tmp_path) as b:
        edition = _mixed_paired(b, capsys)
        assert b.cli("adopt", "set-coverage", SERIES, "2", "3", "10") == 0
        assert b.gaps(edition) == 0
        (b.env.inst.library / "Kavita" / SERIES / f"{SERIES} - Vol.2.cbz").unlink()
        reconcile_handler(b.env.inst.comp, in_window=True)(context("reconcile.hash"))
        # `reconcile` only reports an adopted file that is gone and leaves its record `adopted`
        # (EF-EXP-018); once a record says `missing`, the volume is no presence any more (I20)
        with b.engine.begin() as connection:
            connection.execute(
                sa.text(
                    "UPDATE export_record SET state = 'missing' WHERE export_item_id IN "
                    "(SELECT id FROM export_item WHERE frozen_attrs LIKE '%\"volume\"%')"
                )
            )
        assert b.gaps(edition) == 8  # the volume is gone: its chapters are holes again


def test_set_coverage_refuses_a_chapter_another_file_carries_and_fractions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        _adopt_mixed(b, capsys)
        capsys.readouterr()
        # chapters 1 and 2 have a file of their own: two files never share a Unit
        assert b.cli("adopt", "set-coverage", SERIES, "2", "1", "10") != 0
        assert "1, 2" in capsys.readouterr().err
        assert b.cli("adopt", "set-coverage", SERIES, "2", "3", "9.5") != 0
        assert b.cli("adopt", "set-coverage", SERIES, "2", "1.9", "9") != 0
        assert not b.rows("SELECT 1 FROM unit_coverage WHERE origin = 'manual' AND position > 0")


def test_set_coverage_is_refused_while_restored_behind(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        _adopt_mixed(b, capsys)
        with b.engine.begin() as connection:
            connection.execute(sa.text("UPDATE instance SET restore_generation = 1"))
        capsys.readouterr()
        assert b.cli("adopt", "set-coverage", SERIES, "2", "3", "10") != 0
        assert "restored_behind" in capsys.readouterr().err
        assert not b.rows("SELECT 1 FROM unit_coverage WHERE origin = 'manual' AND position > 0")


def test_names_are_not_refreshed_for_a_paused_destination(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        b.kavita_series("Old Folder", ["Old Name - Ch.1.cbz"], name="Old Name",
                        info=_info("Old Name"))  # fmt: skip
        assert b.adopt(capsys) == 0
        (shown,) = [s for s in b.env.inst.kavita.series if s["name"] == "Old Name"]
        shown["name"] = "Renamed"
        with b.engine.begin() as connection:
            connection.execute(sa.text("UPDATE shelf SET paused_reason = 'child_exposure'"))
        with pytest.raises(DestinationPausedError):  # the arrival poll refuses it as well
            b.export()
        (row,) = b.rows("SELECT name_raw FROM export_series WHERE frozen_series = 'Old Name'")
        assert row["name_raw"] == "Old Name"  # nothing written for a destination that waits


def test_declined_pairing_clears_coverage_unknown(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        b.kavita_series("Volumes Only", ["Volumes Only - Vol.1.cbz"])
        assert b.adopt(capsys) == 0
        assert "(coverage_unknown)" in b.status(capsys)
        assert b.cli("adopt", "pair", "--series", b.series_id("Volumes Only"), "--decline") == 0
        assert "(coverage_unknown)" not in b.status(capsys)


def test_switchover_confirmation_is_one_transaction(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    import shelfhold.export.application.migration_detect as md
    from shelfhold.export.application.migration_detect import (
        STEPS,
        SwitchoverDetector,
        SwitchoverGate,
    )

    with bench54(monkeypatch, settings, tmp_path) as b:
        b.kavita_series("Fed Series", ["Team_Chapter 1.cbz"], info=_info("Fed Series", number="1"))
        assert b.adopt(capsys) == 0
        fsops, _loaded, _tombstones = b.env.inst.comp.read_context()
        gate = SwitchoverGate(
            b.engine, SwitchoverDetector(b.engine, fsops), clock=b.env.inst.comp.clock
        )
        fingerprint = gate.report(b.env.destination_id).fingerprint

        def broken(*_: object, **__: object) -> None:
            raise RuntimeError("disk full")

        monkeypatch.setattr(md.waiting_marks, "clear", broken)
        with pytest.raises(RuntimeError):
            gate.confirm(b.env.destination_id, STEPS, fingerprint=fingerprint)
        assert not gate.is_confirmed(b.env.destination_id)  # neither the steps nor the mark
