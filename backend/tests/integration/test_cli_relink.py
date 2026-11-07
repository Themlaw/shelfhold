"""L09 (EF-INT-014, EF-BIB-029): `shelfhold relink`, the real command over a real instance.

Suwayomi (simulated) followed a series under one source and now lists it under another: the admin
attaches the new binding to the Work that exists. Only the numbers it lacks are listed, and the
next follow cycle asks the service for those and for nothing else.
"""

from pathlib import Path

import pytest

from shelfhold.config import Settings
from tests.l54_env import Bench54, bench54


def migrated(b: Bench54) -> tuple[str, str, str]:
    """Alpha followed with chapters 1-2, then Suwayomi follows Gamma (chapters 1-4) instead."""
    b.upstream({1: "One", 2: "Two"})
    b.follow()
    server = b.env.inst.suwayomi
    (work,) = [r["work_id"] for r in b.rows("SELECT work_id FROM source_binding ORDER BY id")][:1]
    (old,) = [r["id"] for r in b.rows("SELECT id FROM source_binding WHERE work_id = :w", w=work)]
    mangas = server.state["mangas"]
    mangas[0]["inLibrary"] = False
    gamma = mangas[2]
    gamma["inLibrary"] = True
    gamma["chapters"] = [
        {**mangas[0]["chapters"][0], "id": 6000 + n, "url": f"/series-gamma/ch-{n:03d}",
         "name": f"Chapter {n}", "chapterNumber": float(n), "sourceOrder": n - 1}
        for n in (1, 2, 3, 4)
    ]  # fmt: skip
    return work, old, str(gamma["id"])


@pytest.mark.req("EF-INT-014", "EF-BIB-029")
def test_cli_relink(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        work, old, remote = migrated(b)
        asked = len(b.downloaded())
        capsys.readouterr()
        code = b.cli(
            "relink", work, "--source-key", "7003", "--remote-id", remote, "--replaces", old
        )
        shown = capsys.readouterr().out
        assert code == 0
        assert "3" in shown and "4" in shown  # the numbers it lacks
        (binding,) = b.rows(
            "SELECT id, edition_id FROM source_binding WHERE work_id = :w AND active", w=work
        )
        assert binding["id"] != old
        b.follow()
        later = b.downloaded()[asked:]
        assert sorted(later) == sorted(
            f"/api/v1/chapter/{cid}/download" for cid in (6003, 6004)
        ) or len(later) == 2  # only chapters 3 and 4 are asked for
        assert b.rows("SELECT count(*) AS n FROM unit WHERE edition_id = :e",
                      e=binding["edition_id"])[0]["n"] == 4  # fmt: skip
        assert b.rows("SELECT count(*) AS n FROM work")[0]["n"] == 2  # Alpha and Beta, no Gamma
