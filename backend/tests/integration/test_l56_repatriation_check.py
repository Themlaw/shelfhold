"""L56: the control before an unmount asks Kavita (EF-EXP-051).

`shelfhold adopt --repatriate` copies the adopted files home (dry-run, then its fingerprint) and
`--check` scans Kavita, then compares the series, the files and the reading progress its API
gives with what it gave before the copy. Only Kavita is simulated.
"""

from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from tests.l56_env import CHAPTERS, SERIES, Repatriation56, repatriation_bench

pytestmark = pytest.mark.req("EF-EXP-051")

FILES = len(SERIES) * CHAPTERS


def _confirmed(r: Repatriation56, capsys: pytest.CaptureFixture[str]) -> str:
    fingerprint = r.fingerprint(capsys)
    assert r.cli("--confirm", fingerprint) == 0
    return capsys.readouterr().out


def test_repatriate_check_scans_kavita_compares_counts_and_progress(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with repatriation_bench(monkeypatch, capsys, settings, tmp_path) as r:
        fake = r.b.env.inst.kavita
        before = dict(fake.progress)
        assert before and all(before.values())
        out = _confirmed(r, capsys)
        assert "Contrôle réussi" in out
        assert len(list((r.b.env.inst.library / "Kavita").rglob("*.cbz"))) == FILES
        r.unmount()
        fake.scans.clear()
        # the volume is gone and Kavita still gives the same series, files and progress
        assert r.cli("--check") == 0
        assert "Contrôle réussi" in capsys.readouterr().out
        assert fake.scans, "the control asks Kavita to scan before it reads"
        assert fake.violations == []
        # Kavita lost the reading progress of one file: the unmount is refused, the file named
        victim = r.remote(SERIES[0], f"{SERIES[0]} - Ch.2.cbz")
        fake.progress[victim] = 0
        assert r.cli("--check") == 3
        captured = capsys.readouterr()
        assert "Contrôle échoué" in captured.out + captured.err
        assert victim in captured.err
        fake.progress[victim] = before[victim]
        # a series Kavita no longer lists is as bad
        gone = next(s for s in fake.series if s["name"] == SERIES[1])
        fake.series.remove(gone)
        assert r.cli("--check") == 3
        assert SERIES[1] in capsys.readouterr().err
        fake.series.append(gone)
        # a file it no longer indexes
        lost = fake.files[gone["id"]].pop()
        assert r.cli("--check") == 3
        assert lost in capsys.readouterr().err
        fake.files[gone["id"]].append(lost)
        assert r.cli("--check") == 0


def test_repatriate_check_refuses_when_kavita_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with repatriation_bench(monkeypatch, capsys, settings, tmp_path) as r:
        _confirmed(r, capsys)
        r.unmount()
        r.b.env.inst.kavita.missing.add(("POST", "/api/series/all-v2"))
        assert r.cli("--check") == 3  # nothing is verified: nothing is declared safe
        assert "Kavita" in capsys.readouterr().err


def test_repatriate_check_without_the_baseline_of_the_apply_is_not_safe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with repatriation_bench(monkeypatch, capsys, settings, tmp_path) as r:
        assert r.cli("--check") == 3  # no apply yet: Kavita has no "before" to be compared with
        assert "avant" in capsys.readouterr().err


def _library_files(r: Repatriation56) -> list[Path]:
    return sorted((r.b.env.inst.library / "Kavita").rglob("*.cbz"))


def _baselines(r: Repatriation56) -> list[str]:
    with r.b.engine.connect() as connection:
        return [
            row[0]
            for row in connection.execute(
                sa.text(
                    "SELECT value FROM app_setting WHERE key LIKE 'repatriate.kavita_baseline.%'"
                )
            )
        ]


def test_a_before_that_sees_no_series_refuses_the_apply_and_copies_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with repatriation_bench(monkeypatch, capsys, settings, tmp_path) as r:
        fake = r.b.env.inst.kavita
        fingerprint = r.fingerprint(capsys)
        indexed = list(fake.series)
        fake.series.clear()  # the account of the key sees nothing (no access, or a scan running)
        assert r.cli("--confirm", fingerprint) != 0
        assert _library_files(r) == [] and _baselines(r) == []
        fake.series.extend(indexed)
        assert (
            r.cli("--confirm", fingerprint) == 0
        )  # Kavita answers: the before is taken, then copy
        assert len(_library_files(r)) == len(SERIES) * CHAPTERS


def test_a_before_that_misses_files_of_the_plan_refuses_the_apply(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with repatriation_bench(monkeypatch, capsys, settings, tmp_path) as r:
        fake = r.b.env.inst.kavita
        fingerprint = r.fingerprint(capsys)
        fake.files[fake.series[0]["id"]].pop()  # Kavita has not indexed one adopted file yet
        assert r.cli("--confirm", fingerprint) != 0
        assert _library_files(r) == [] and _baselines(r) == []


def test_apply_with_kavita_unreachable_copies_nothing_and_the_before_is_never_replaced(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with repatriation_bench(monkeypatch, capsys, settings, tmp_path) as r:
        fake = r.b.env.inst.kavita
        fingerprint = r.fingerprint(capsys)
        fake.missing.add(("POST", "/api/series/all-v2"))
        assert r.cli("--confirm", fingerprint) != 0
        assert _library_files(r) == [] and _baselines(r) == []  # no copy without a before
        fake.missing.clear()
        assert r.cli("--confirm", fingerprint) == 0
        (before,) = _baselines(r)
        r.unmount()
        victim = r.remote(SERIES[0], f"{SERIES[0]} - Ch.2.cbz")
        fake.progress[victim] = 0  # Kavita lost it once the volume went
        # a second apply, run after the damage, must not take the damaged state as the reference
        again = r.fingerprint(capsys)
        assert r.cli("--confirm", again) == 3
        assert _baselines(r) == [before]
        assert victim in capsys.readouterr().err


def test_a_listing_cut_at_its_cap_is_incomplete_not_all_of_kavita(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    from shelfhold.export.targets.kavita import client

    with repatriation_bench(monkeypatch, capsys, settings, tmp_path) as r:
        fingerprint = r.fingerprint(capsys)
        monkeypatch.setattr(client, "SERIES_PAGE", 1)
        monkeypatch.setattr(client, "MAX_SERIES_PAGES", 1)  # two series, one page of one
        assert r.cli("--confirm", fingerprint) != 0
        assert _library_files(r) == [] and _baselines(r) == []
