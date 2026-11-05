"""An export environment wired to the simulated Kavita (reinstall, arrival, audit tests)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from shelfhold.export.targets.kavita import AdminKey, KavitaClient
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.export_env import ExportEnv
from tests.fakes.kavita import API_KEY, BASE_URL, FakeKavita, build_fetcher
from tests.kavita_env import RecordingEvents

LIBRARY_ID = 7  # the first library of the simulated server, folder `/manga`
REMOTE_ROOT = "/manga/Kavita"  # path map `/manga` + the root_subpath of the destination


@dataclass
class Wired:
    env: ExportEnv
    fake: FakeKavita
    events: RecordingEvents
    client: KavitaClient

    def rebuild_client(self) -> KavitaClient:
        """A client with no memory (a restart): the health is read from the database."""
        self.client = KavitaClient(
            build_fetcher(self.fake), BASE_URL, AdminKey(API_KEY), events=self.events,
            clock=self.env.clock,
        )  # fmt: skip
        return self.client

    def written(
        self, series_title: str = "Dr Stone", chapters: tuple[str, ...] = ("2",)
    ) -> list[str]:
        """Export chapters through the real pipeline; the relative paths of the records."""
        series = self.env.new_series(series_title)
        for number in chapters:
            self.env.add_chapter(series, number)
        self.env.pipeline.run(self.env.destination_id, job_id=new_ulid())
        return sorted(r.rel_path for r in self.env.rows(export.export_record))

    def remote(self, rel_path: str) -> str:
        return f"{REMOTE_ROOT}/{rel_path}"


def wire(env: ExportEnv, *, version: str = "0.9.1.4", **fake_options: Any) -> Wired:
    fake = FakeKavita(version=version, **fake_options)
    events = RecordingEvents()
    with env.engine.begin() as connection:  # the remote id of the library is the one of Kavita
        connection.execute(sa.update(export.remote_library).values(remote_ref=str(LIBRARY_ID)))
    client = KavitaClient(
        build_fetcher(fake), BASE_URL, AdminKey(API_KEY), events=events, clock=env.clock
    )
    return Wired(env, fake, events, client)
