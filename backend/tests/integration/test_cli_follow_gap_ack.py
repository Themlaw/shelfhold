"""EF-BIB-056: `shelfhold follow gap-ack`, the real command over a real instance."""

from pathlib import Path

import pytest

from shelfhold.config import Settings
from tests.l54_env import Bench54, bench54

pytestmark = pytest.mark.req("EF-BIB-056")


def _with_a_gap(b: Bench54) -> str:
    """Chapters 1 and 2 are followed, then the source lists 1, 2 and 4: 3 is missing."""
    b.upstream({1: "One", 2: "Two"})
    b.follow()
    b.upstream({1: "One", 2: "Two", 4: "Four"})
    b.follow(1)
    return str(b.rows("SELECT edition_id FROM series_gap")[0]["edition_id"])


def test_cli_follow_gap_ack(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        edition = _with_a_gap(b)
        assert [r["number_norm"] for r in b.rows("SELECT number_norm FROM series_gap")] == ["3"]
        capsys.readouterr()
        assert b.cli("follow", "gap-ack", edition, "3") == 0
        assert "acquitté" in capsys.readouterr().out
        assert b.rows("SELECT number_norm FROM series_gap") == []
        (ack,) = b.rows("SELECT track, number_segment, number_norm FROM gap_ack")
        assert ack == {"track": "main", "number_segment": "", "number_norm": "3"}
        b.follow(2)
        assert b.rows("SELECT number_norm FROM series_gap") == []  # silenced for good
        assert b.cli("follow", "gap-ack", edition, "3") == 0  # a replay changes nothing
        assert "déjà acquitté" in capsys.readouterr().out
        assert b.cli("follow", "gap-ack", edition, "3", "--undo") == 0
        assert "repris" in capsys.readouterr().out
        assert b.rows("SELECT * FROM gap_ack") == []
        b.follow(1)
        assert [r["number_norm"] for r in b.rows("SELECT number_norm FROM series_gap")] == ["3"]


def test_cli_follow_gap_ack_refuses_what_is_no_gap(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Only a gap the series shows can be acknowledged: not a chapter that exists, not a
    number nobody listed, not a number that is no number."""
    with bench54(monkeypatch, settings, tmp_path) as b:
        edition = _with_a_gap(b)
        capsys.readouterr()
        assert b.cli("follow", "gap-ack", edition, "2") != 0  # chapter 2 exists
        assert b.cli("follow", "gap-ack", edition, "99") != 0  # nobody missed it
        assert b.cli("follow", "gap-ack", edition, "3.5") != 0  # decimals are never gaps
        assert b.cli("follow", "gap-ack", edition, "x") != 0
        assert b.cli("follow", "gap-ack", edition, "3", "--track", "bogus") != 0
        assert b.cli("follow", "gap-ack", "no-such-edition", "3") != 0
        assert b.rows("SELECT * FROM gap_ack") == []
        assert [r["number_norm"] for r in b.rows("SELECT number_norm FROM series_gap")] == ["3"]


def test_cli_follow_gap_ack_needs_a_number_and_follow_refuses_a_stray_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        capsys.readouterr()
        assert b.cli("follow", "gap-ack", "some-edition") == 2
        assert "numéro" in capsys.readouterr().err
        assert b.cli("follow", "suwayomi:7003", "/series-gamma", "3") == 2
        assert b.rows("SELECT * FROM follow_request") == []  # nothing was asked of the service
