"""One reference library for the API benches of a run: building 60 000 Units is not the measure."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from shelfhold.config import load_settings
from tests.bench.perf_env import Reference, reference_app


@pytest.fixture(scope="session")
def reference(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Reference]:
    root: Path = tmp_path_factory.mktemp("reference")
    settings = load_settings(
        {
            "SHELFHOLD_DISK_PAUSE_PERCENT": "0",
            "SHELFHOLD_DISK_PAUSE_GIB": "0",
            "SHELFHOLD_DATA_DIR": str(root / "data"),
            "SHELFHOLD_STORE_DIR": str(root / "store"),
        }
    ).values
    with reference_app(settings) as ref:
        yield ref
