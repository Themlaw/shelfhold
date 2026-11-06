"""L51: the real application (`create_app`) over a real instance: database, keyring, roots, job
pools, pipeline and file system are the production ones; only Kavita is the simulated server.

The tests reach the product through its real doors: the routes of the application and the
`shelfhold` commands. A kill of the process is simulated in the file-system adapter (`Fsops`), the
way a SIGKILL between the write and its bookkeeping would leave the journal.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

import shelfhold.runtime_jobs as rj
from shelfhold.config import Settings
from shelfhold.infrastructure.fsops import Fsops
from tests.auth_env import AuthEnv, auth_env
from tests.l50_env import Seeded, Series, seeded


class SimulatedKill(BaseException):
    """The process died after the file was placed and before its bookkeeping was done."""


@dataclass
class Live:
    env: Seeded
    api: AuthEnv
    series: Series

    @property
    def client(self) -> TestClient:
        return self.api.client

    def preview(self, **headers: str) -> Any:
        return self.api.post(
            "/api/v1/plans", json={"shelf_destination_id": self.env.destination_id},
            headers=headers,
        )  # fmt: skip

    def apply(self, plan_id: str, etag: str | None, **headers: str) -> Any:
        sent = dict(headers)
        if etag is not None:
            sent["If-Match"] = etag
        return self.api.post(f"/api/v1/plans/{plan_id}/apply", headers=sent)

    def wait_job(self, job_id: str, *, timeout: float = 30.0) -> dict[str, Any]:
        """Poll `GET /jobs/{id}` the way a client does, until the job is over."""
        deadline = time.monotonic() + timeout
        while True:
            body: dict[str, Any] = self.client.get(f"/api/v1/jobs/{job_id}").json()
            if body["status"] in {"succeeded", "failed", "dead_letter", "cancelled"}:
                return body
            assert time.monotonic() < deadline, f"job {job_id} still {body['status']}"
            time.sleep(0.05)

    def rows(self, table: sa.Table) -> list[sa.Row[tuple[object, ...]]]:
        return self.env.rows(table)


@contextmanager
def live(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    *,
    chapters: tuple[str, ...] = ("1", "2"),
    seed: Any = None,
) -> Iterator[Live]:
    """The application is started after `seed(env, series)`, which may leave the disk in any
    state (a kill, a foreign writer) for the boot to find."""
    monkeypatch.setattr(rj, "SCAN_QUIET_S", 0)  # the scans leave at once: no 31 s debounce
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        for number in chapters:
            env.add_chapter(series, number)
        if seed is not None:
            seed(env, series)
        with auth_env(monkeypatch, settings, network=env.inst.network) as api:
            assert api.login().status_code == 200
            yield Live(env, api, series)


@contextmanager
def killed_after_write(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The adapter of the file system places the file, then the process 'dies'."""
    original = Fsops.write_atomic

    def write_then_die(self: Fsops, *args: Any, **kwargs: Any) -> Any:
        original(self, *args, **kwargs)
        raise SimulatedKill

    monkeypatch.setattr(Fsops, "write_atomic", write_then_die)
    try:
        yield
    finally:
        monkeypatch.setattr(Fsops, "write_atomic", original)
