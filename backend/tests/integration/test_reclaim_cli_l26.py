"""M3 L26: the host CLI doors of the reclaim actions, run in process over a real instance (real
purge, real tombstone service, real roots): `trash empty --confirm`, `tombstone list`,
`tombstone lift --seq --confirm`. The list shows what is needed to choose a sequence and nothing
of what was erased (EF-BIB-054)."""

import json
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from tests.auth_env import run_cli
from tests.l50_env import Seeded, seeded

pytestmark = pytest.mark.req("EF-BIB-030", "EF-BIB-054")

TITLE = "Titre Effacé Unique"


def _purge(monkeypatch: pytest.MonkeyPatch, env: Seeded, title: str) -> str:
    unit = env.add_chapter(env.new_series(title), "1")
    argv = ("purge", "--now", "--unit", unit, "--confirm", unit)
    assert run_cli(monkeypatch, env.inst.settings, *argv) == 0
    return unit


def test_trash_empty_from_the_cli_needs_its_word_and_empties_the_bins(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with seeded(settings, tmp_path) as env:
        bin_dir = env.inst.library / ".shelfhold" / "recycle"
        entry = bin_dir / ("a" * 32)
        (entry / "x").mkdir(parents=True)
        (entry / "x" / "page.png").write_bytes(b"png")
        capsys.readouterr()

        assert run_cli(monkeypatch, settings, "trash", "empty") == 2  # no word: nothing sent
        assert entry.exists()
        assert run_cli(monkeypatch, settings, "trash", "empty", "--confirm", "nope") != 0
        assert entry.exists()  # a wrong word, compared by the server: nothing removed

        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "trash", "empty", "--confirm", "empty-trash") == 0

        assert not entry.exists()
        assert "1" in capsys.readouterr().out


def test_tombstone_list_then_lift_from_the_cli_without_ever_naming_the_work(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with seeded(settings, tmp_path) as env:
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "tombstone", "list") == 0
        assert "Aucune" in capsys.readouterr().out  # nothing erased yet

        _purge(monkeypatch, env, TITLE)
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "tombstone", "list") == 0
        shown = capsys.readouterr().out
        lines = [ln for ln in shown.splitlines() if ln.strip()]
        assert lines, shown
        seqs = [int(ln.split()[0]) for ln in lines]
        seq = seqs[0]
        assert TITLE not in shown and TITLE.lower() not in shown.lower()  # no title, no path
        assert str(tmp_path) not in shown

        assert run_cli(monkeypatch, settings, "tombstone", "lift", "--seq", str(seq)) == 2
        assert (
            run_cli(
                monkeypatch,
                settings,
                "tombstone",
                "lift",
                "--seq",
                str(seq),
                "--confirm",
                "lift 999",
            )
            != 0
        )  # the word of another sequence lifts nothing
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "tombstone", "list") == 0
        assert capsys.readouterr().out.split()[0] == str(seq)  # still blocking

        assert (
            run_cli(
                monkeypatch, settings, "tombstone", "lift", "--seq", str(seq),
                "--confirm", f"lift {seq}",
            )
            == 0
        )  # fmt: skip
        for other_seq in seqs[1:]:  # a purge lays several: each is lifted by its own number
            argv = ("tombstone", "lift", "--seq", str(other_seq), "--confirm", f"lift {other_seq}")
            assert run_cli(monkeypatch, settings, *argv) == 0
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "tombstone", "list") == 0
        assert "Aucune" in capsys.readouterr().out  # the lifted ones are not listed any more
        with env.inst.engine.connect() as connection:
            audited = (
                connection.execute(
                    sa.text("SELECT target FROM audit WHERE action = 'tombstone.lifted'")
                )
                .scalars()
                .all()
            )
        assert audited == [f"seq:{n}" for n in seqs]
        assert json.dumps(audited).count(TITLE) == 0
