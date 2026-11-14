"""M3 L26, last pass of the adversarial review of #233: the early purge job takes the lease every
ten minutes, even when no volume is under its mark. Red against the branch as reviewed. See
`docs/decisions/M3.md`, « L26 : contradicteur de la #233 », last pass."""

from pathlib import Path

import pytest

from shelfhold.composition import Gates
from shelfhold.config import Settings
from shelfhold.infrastructure.instance.generation import GenerationCounter
from shelfhold.retention_jobs import early_purge_trash
from tests.l41_env import instance


@pytest.mark.req("EF-BIB-030")
def test_the_early_purge_without_pressure_moves_no_generation(
    settings: Settings, tmp_path: Path
) -> None:
    """No volume under its mark: the job must not touch a root. `early_purge_trash` opens
    `write_context()` before it asks the pressure; the lease it takes (when no other job holds it)
    advances the generation and rewrites the sentinel of every writable root, every ten minutes
    (a disk woken for nothing, ENF-OPS-014)."""
    with instance(settings, tmp_path) as inst:
        inst.configure()
        inst.comp.gates = Gates(disk_pressure=lambda _root: False)
        roots = [e.path for e in inst.comp.load_roots().expected]  # type: ignore[union-attr]
        generations = GenerationCounter(inst.engine)
        before = {root: generations.current(root) for root in roots}

        report = early_purge_trash(inst.comp)

        assert report.removed == 0
        after = {root: generations.current(root) for root in roots}
        assert after == before, "the job moved the generation for nothing"


@pytest.mark.req("EF-BIB-030")
def test_an_export_can_still_scan_after_an_idle_early_purge(
    settings: Settings, tmp_path: Path
) -> None:
    """What the e2e `m1b-family` met: an export job builds its own `Fsops` (its expected
    generation), applies, waits for the scan debounce; the early purge runs in between, takes and
    gives back the lease, and the generation moves under the export. Its scan guard then reads a
    sentinel ahead of what it expects (`restored_behind`): `export.scan_refused root_not_ready`,
    and Kavita never scans the family library."""
    with instance(settings, tmp_path) as inst:
        inst.configure()
        inst.comp.gates = Gates(disk_pressure=lambda _root: False)
        with inst.comp.write_context():  # the export's own apply, then its lease given back
            pass
        export_fsops, _loaded, _tombstones = inst.comp.read_context()
        early_purge_trash(inst.comp)  # idle: no pressure

        export_fsops.require_scan_allowed(str(inst.library / "Kavita"))
