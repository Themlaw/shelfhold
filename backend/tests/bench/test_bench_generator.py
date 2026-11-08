"""ENF-PERF-008 (culpable): the reference profile is generated, always the same, and its manifest
is the one versioned in `.github/bench-reference.json`, which every release attaches."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import acquisition, catalog, identity
from tests.bench.generator import (
    CI_PROFILE,
    FULL_PROFILE,
    generate_catalog,
    generate_files,
    reference_manifest,
)
from tests.bench.harness import load_reference


@pytest.mark.req("ENF-PERF-008")
def test_reference_profile_has_the_volume_of_the_spec() -> None:
    for profile in (CI_PROFILE, FULL_PROFILE):  # same rows, only the files differ
        assert (profile.works, profile.units, profile.pages) == (2_000, 60_000, 3_000_000)
        assert (profile.followed, profile.users) == (500, 6)
    assert (
        abs(FULL_PROFILE.total_bytes - 2 * 1000**4) <= FULL_PROFILE.units
    )  # 2 TB, to the byte per Unit
    assert CI_PROFILE.total_bytes < 4 * 1024**3  # "a few GiB", sparse


@pytest.mark.req("ENF-PERF-008")
def test_generator_writes_the_same_catalog_every_time(settings: Settings, tmp_path: Path) -> None:
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    try:
        first = generate_catalog(engine, CI_PROFILE)
        with engine.connect() as connection:
            counts = {
                "work": connection.scalar(sa.select(sa.func.count()).select_from(catalog.work)),
                "unit": connection.scalar(sa.select(sa.func.count()).select_from(catalog.unit)),
                "follow": connection.scalar(
                    sa.select(sa.func.count()).select_from(acquisition.series_follow)
                ),
                "user": connection.scalar(sa.select(sa.func.count()).select_from(identity.user)),
            }
    finally:
        engine.dispose()
    assert counts == {"work": 2_000, "unit": 60_000, "follow": 500, "user": 6}
    assert reference_manifest(CI_PROFILE) == asdict(first)  # a second database, same digest


@pytest.mark.req("ENF-PERF-008")
def test_the_versioned_reference_records_the_generated_profile() -> None:
    reference = load_reference()
    assert reference["status"] == "reference"
    assert reference["profile"] == "ci"
    assert reference["manifest"] == reference_manifest(CI_PROFILE)


@pytest.mark.req("ENF-PERF-008")
def test_ci_profile_files_are_sparse(tmp_path: Path) -> None:
    written = generate_files(tmp_path, CI_PROFILE, limit=300)
    files = sorted(p for p in tmp_path.rglob("*.cbz"))
    assert written == len(files) == 300
    assert {p.stat().st_size for p in files} == {CI_PROFILE.unit_bytes}
    on_disk = sum(p.stat().st_blocks * 512 for p in files)
    assert on_disk < CI_PROFILE.unit_bytes * 300 // 20  # holes, not data
