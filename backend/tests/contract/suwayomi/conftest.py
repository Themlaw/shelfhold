from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from .harness import Bench


def _bench(mode: str, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Bench]:
    data = tmp_path_factory.mktemp(f"suwayomi-{mode}")
    bench = Bench(mode=mode, data_dir=data, project=f"s5-{mode}-{uuid.uuid4().hex[:6]}")
    bench.seed_local_library()
    try:
        bench.start()
        if mode == "ui_login":
            bench.login()
        yield bench
    finally:
        try:
            bench.stop()
        finally:
            shutil.rmtree(Path(data), ignore_errors=True)


@pytest.fixture(scope="module", params=["basic_auth", "ui_login"])
def bench(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Bench]:
    """The contract is played in both authentication modes (EF-SRC-031)."""
    yield from _bench(request.param, tmp_path_factory)
