"""L54: the real doors only. A Kavita library on disk is adopted with `shelfhold adopt` against the
simulated Kavita; the series are paired with `adopt pair`; the follow handler and the export
handler are the production jobs; `status` is the command. Nothing here writes a `unit_coverage`
row or a waiting state: the production code does."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar, Literal

import pytest
import sqlalchemy as sa

import shelfhold.composition as composition
import shelfhold.runtime_jobs as rj
from shelfhold.acquisition.public import Backfill
from shelfhold.config import Settings
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, export
from shelfhold.jobs.ledger import JobLedger
from shelfhold.runtime_jobs import export_handler, follow_handler
from tests.adopt_env import comicinfo
from tests.auth_env import run_cli
from tests.fakes.suwayomi.server import BASE_URL as SUWAYOMI_URL
from tests.fakes.suwayomi.server import PASSWORD, USER
from tests.follow_env import new_chapter
from tests.l41_env import context
from tests.l50_env import Seeded, seeded
from tests.library_env import cbz_bytes

REMOTE_ROOT = "/manga/Kavita"
SERIES = "Series Alpha"  # the series Suwayomi follows


class Pools:
    slots = None
    submitted: ClassVar[list[str]] = []

    def submit(self, kind: str, **_: object) -> None:
        self.submitted.append(kind)


@dataclass
class Bench54:
    env: Seeded
    settings: Settings
    monkeypatch: pytest.MonkeyPatch
    library_ref: str

    @property
    def engine(self) -> sa.Engine:
        return self.env.inst.engine

    def cli(self, *argv: str) -> int:
        return run_cli(self.monkeypatch, self.settings, *argv)

    def kavita_series(
        self, folder: str, files: list[str], *, name: str | None = None, info: bytes | None = None
    ) -> int:
        """Files on disk under the Kavita root, and the series the simulated Kavita indexed."""
        directory = self.env.inst.library / "Kavita" / folder
        directory.mkdir(parents=True, exist_ok=True)
        remote = []
        for index, filename in enumerate(files):
            pages = ((f"{filename}-{index}.png", b"\x89PNG\r\n\x1a\n" + filename.encode()),)
            (directory / filename).write_bytes(cbz_bytes(pages, comicinfo=info))
            remote.append(f"{REMOTE_ROOT}/{folder}/{filename}")
        return self.env.inst.kavita.add_series(
            name or folder, f"{REMOTE_ROOT}/{folder}", remote, library_id=int(self.library_ref)
        )

    def adopt(self, capsys: pytest.CaptureFixture[str]) -> int:
        """What the admin types: the preview, then the confirmation of its fingerprint."""
        base = ("adopt", "--destination", self.env.destination_id, "--library", self.library_ref)
        capsys.readouterr()
        assert self.cli(*base, "--dry-run") == 0
        fingerprint = re.split(r"aperçu\s:", capsys.readouterr().out)[1].split()[0]
        return self.cli(*base, "--confirm", fingerprint)

    def status(self, capsys: pytest.CaptureFixture[str]) -> str:
        capsys.readouterr()
        assert self.cli("status") == 0
        return capsys.readouterr().out

    def series_id(self, name: str) -> str:
        with self.engine.connect() as connection:
            return str(
                connection.execute(
                    sa.select(export.export_series.c.id).where(
                        export.export_series.c.frozen_series == name
                    )
                ).scalar_one()
            )

    def edition_of(self, series_id: str) -> str:
        with self.engine.connect() as connection:
            return str(
                connection.execute(
                    sa.select(export.edition_export_series.c.edition_id).where(
                        export.edition_export_series.c.export_series_id == series_id
                    )
                ).scalar_one()
            )

    def upstream(self, chapters: dict[int, str]) -> None:
        """The simulated Suwayomi lists these chapters (number -> name) of its series."""
        nodes = []
        for number, name in sorted(chapters.items()):
            node = new_chapter(5000 + number * 10, number)
            node["name"] = name
            nodes.append(node)
        self.env.inst.suwayomi.state["mangas"][0]["chapters"] = nodes

    def follow(self, cycles: int = 3) -> None:
        for _ in range(cycles):
            follow_handler(self.env.inst.comp, Pools())(context("follow.cycle"))  # type: ignore[arg-type]

    def export(self) -> None:
        """The export job, registered in the ledger like the scheduler does (the locks of the
        resources it takes belong to a job that exists)."""
        comp = self.env.inst.comp
        ledger = JobLedger(self.engine, comp.clock)
        with write_transaction(self.engine) as connection:
            created = ledger.create(
                connection, kind="export.run", dedupe_key=f"export.run.{new_ulid()}"
            )
        job = context("export.run")
        export_handler(comp)(replace(job, job_id=created.job_id, ledger=ledger))

    def gaps(self, edition_id: str, mode: Literal["gaps", "all"] = "gaps") -> int:
        return Backfill(self.engine, free_bytes=lambda: 10**12).estimate(edition_id, mode).units

    def rows(self, sql: str, **params: object) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            return [dict(r) for r in connection.execute(sa.text(sql), params).mappings()]

    def downloaded(self) -> list[str]:
        return [r.path for r in self.env.inst.suwayomi.requests_to("/api/v1/chapter/")]


@contextmanager
def bench54(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> Iterator[Bench54]:
    monkeypatch.setattr(rj, "SCAN_QUIET_S", 0)  # the scans leave at once: no 31 s debounce
    with seeded(settings, tmp_path) as env:
        inst = env.inst
        inst.admin.source_add_suwayomi(url=SUWAYOMI_URL, secret=f"{USER}:{PASSWORD}")
        monkeypatch.setattr(composition, "_DEFAULT_NETWORK", inst.network)
        with inst.engine.connect() as connection:
            ref = connection.execute(sa.select(export.remote_library.c.remote_ref)).scalar_one()
        yield Bench54(env, settings, monkeypatch, str(ref))


def comic(series: str, *, number: str = "", language: str = "en") -> bytes:
    return comicinfo(series, number=number, language=language)


__all__ = ["REMOTE_ROOT", "SERIES", "Bench54", "acquisition", "bench54", "catalog", "comic"]
