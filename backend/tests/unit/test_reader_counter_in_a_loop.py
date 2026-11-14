"""The count of readers of a rename plan is asked from inside the loop of the job that applies it
(EF-EXP-037): the client it asks with is built in a thread of its own, since building one runs a
loop (the plan was refused with `asyncio.run() cannot be called from a running event loop`)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from shelfhold import runtime_jobs
from shelfhold.export.application.rename_plan import ReaderCount


@pytest.mark.req("EF-EXP-037")
def test_the_reader_count_builds_its_client_outside_the_running_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_jobs, "_remote_library_id", lambda _comp, _destination: 7)

    async def count(_client: Any, library: int, folders: list[str]) -> ReaderCount:
        assert (library, folders) == (7, ["a"])
        return ReaderCount(2, exact=True)

    monkeypatch.setattr("shelfhold.export.targets.kavita.readers.count_readers", count)

    def kavita_client(_target: str) -> object:
        asyncio.run(asyncio.sleep(0))  # what the real one does to read its integrations
        return object()

    comp: Any = SimpleNamespace(kavita_client=kavita_client)
    counter = runtime_jobs.reader_counter(comp, SimpleNamespace(id="d", target_id="t"))  # type: ignore[arg-type]

    async def inside_the_job() -> ReaderCount:
        return counter(["a"])

    assert asyncio.run(inside_the_job()) == ReaderCount(2, exact=True)
