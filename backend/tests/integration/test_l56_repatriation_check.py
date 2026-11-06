"""L56: the control before an unmount asks Kavita (EF-EXP-051).

`shelfhold adopt --repatriate` copies the adopted files home (dry-run, then its fingerprint) and
`--check` scans Kavita, then compares the series, the files and the reading progress its API
gives with what it gave before the copy. Only Kavita is simulated.
"""

from pathlib import Path

import pytest

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
