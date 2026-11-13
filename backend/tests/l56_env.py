"""L56: a Kavita library of the admin adopted by `shelfhold adopt`, whose files then live in a
volume mounted read-only in Shelfhold (`adopt_ro`), and the commands that bring them home.

Real files, a real database, the production commands; the only simulated service is Kavita, whose
reading progress is what the account of the key has read (`ChapterDto.pagesRead`)."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from shelfhold.config import Settings
from shelfhold.export.application import repatriate_kavita
from tests.l54_env import REMOTE_ROOT, Bench54, bench54

SERIES = ("Series Alpha", "Series Beta")
CHAPTERS = 3


def chapter_names(series: str) -> list[str]:
    return [f"{series} - Ch.{n}.cbz" for n in range(1, CHAPTERS + 1)]


@dataclass
class Repatriation56:
    b: Bench54
    ro: Path  # the upstream volume, read-only in Shelfhold

    def remote(self, series: str, name: str) -> str:
        return f"{REMOTE_ROOT}/{series}/{name}"

    def cli(self, *argv: str) -> int:
        return self.b.cli("adopt", "--repatriate", *argv)

    def fingerprint(self, capsys: pytest.CaptureFixture[str]) -> str:
        capsys.readouterr()
        assert self.cli("--dry-run") == 0
        return re.split(r"Empreinte de l'aperçu\s:", capsys.readouterr().out)[1].split()[0]

    def unmount(self) -> None:
        """What the admin does last: the old volume goes away."""
        shutil.rmtree(self.ro)


@contextmanager
def repatriation_bench(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> Iterator[Repatriation56]:
    monkeypatch.setattr(repatriate_kavita, "SETTLE_INTERVAL_S", 0.0)
    with bench54(monkeypatch, settings, tmp_path) as b:
        inst = b.env.inst
        ro = tmp_path / "upstream-ro"
        ro.mkdir()
        inst.admin.set(
            "path_map.k",
            json.dumps([
                {"local": str(inst.library), "remote": "/manga", "mode": "rw"},
                {"local": str(ro), "remote": "/upstream/suwayomi", "mode": "adopt_ro"},
            ]),
        )  # fmt: skip
        for index, name in enumerate(SERIES):
            b.kavita_series(name, chapter_names(name))
            for chapter, filename in enumerate(chapter_names(name), start=1):
                inst.kavita.progress[f"{REMOTE_ROOT}/{name}/{filename}"] = chapter + index
        assert b.adopt(capsys) == 0
        # the tree is where an old automation put it: in the volume, not under the library
        shutil.move(str(inst.library / "Kavita"), str(ro / "Kavita"))
        (inst.library / "Kavita").mkdir()
        yield Repatriation56(b, ro)
