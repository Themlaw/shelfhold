from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from .kavita_real import RealKavita
from .komga_real import RealKomga

DEFAULT_KOMGA_IMAGE = "docker.io/gotson/komga:1.30.1"
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


@pytest.fixture(scope="module")
def komga(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RealKomga]:
    """One real Komga per module, of the image `KOMGA_IMAGE` (the workflow's matrix)."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")
    library = tmp_path_factory.mktemp("komga-library")
    server = RealKomga(os.environ.get("KOMGA_IMAGE", DEFAULT_KOMGA_IMAGE), Path(library))
    try:
        server.start()
        yield server
    finally:
        try:
            server.stop()
        finally:
            shutil.rmtree(library, ignore_errors=True)
