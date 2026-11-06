from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from .kavita_real import RealKavita

DEFAULT_IMAGE = "docker.io/jvmilazz0/kavita:0.9.1"


@pytest.fixture(scope="module")
def kavita(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RealKavita]:
    """One real Kavita per module, of the image `KAVITA_IMAGE` (the workflow's matrix)."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")
    library = tmp_path_factory.mktemp("kavita-library")
    server = RealKavita(os.environ.get("KAVITA_IMAGE", DEFAULT_IMAGE), Path(library))
    try:
        server.start()
        yield server
    finally:
        try:
            server.stop()
        finally:
            shutil.rmtree(library, ignore_errors=True)
