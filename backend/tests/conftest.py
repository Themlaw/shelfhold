"""Shared fixtures: every test works in its own temporary /data and /store."""

from pathlib import Path

import pytest

from shelfhold.config import Settings, load_settings


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {
        "SHELFHOLD_DATA_DIR": str(tmp_path / "data"),
        "SHELFHOLD_STORE_DIR": str(tmp_path / "store"),
    }


@pytest.fixture
def settings(env: dict[str, str]) -> Settings:
    return load_settings(env).values
