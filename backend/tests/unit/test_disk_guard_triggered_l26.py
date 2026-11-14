"""M3 L26 (EF-BIB-030): the disk guard says whether the volume of a path is under its mark, which
is the only condition of the early purge of the trash (read by fsops, never by the caller)."""

from pathlib import Path

import pytest

from shelfhold.infrastructure.observability.disk_guard import (
    GIB,
    DiskGuard,
    DiskThresholds,
    Usage,
)

pytestmark = pytest.mark.req("EF-BIB-030")

TOTAL = 100 * GIB


def guard(tmp_path: Path, free: list[int]) -> DiskGuard:
    return DiskGuard(
        {"library": tmp_path}, thresholds=DiskThresholds(),
        usage=lambda _p: Usage(TOTAL, free[0]), ttl_s=1000.0,
    )  # fmt: skip


def test_a_volume_under_its_mark_is_triggered_and_stays_so_until_the_resume_mark(
    tmp_path: Path,
) -> None:
    free = [4 * GIB]
    g = guard(tmp_path, free)
    assert g.triggered(tmp_path, fresh=True) is True  # 4 GiB < max(5 %, 10 GiB)
    free[0] = 12 * GIB  # above the pause mark, under the resume mark (15 GiB): still triggered
    assert g.triggered(tmp_path, fresh=True) is True
    free[0] = 16 * GIB
    assert g.triggered(tmp_path, fresh=True) is False


def test_a_measure_is_reused_until_it_is_asked_again(tmp_path: Path) -> None:
    free = [4 * GIB]
    g = guard(tmp_path, free)
    assert g.triggered(tmp_path) is True
    free[0] = 90 * GIB
    assert g.triggered(tmp_path) is True  # the measure is young: not taken again
    assert g.triggered(tmp_path, fresh=True) is False


def test_another_volume_is_not_triggered() -> None:
    free = [1 * GIB]
    g = DiskGuard({"data": Path("/proc")}, usage=lambda _p: Usage(TOTAL, free[0]), ttl_s=1000.0)
    assert g.triggered(Path("/proc"), fresh=True) is True
    assert g.triggered(Path(__file__).parent, fresh=True) is False  # another device


def test_a_path_that_cannot_be_read_is_not_triggered(tmp_path: Path) -> None:
    g = guard(tmp_path, [1 * GIB])
    assert g.triggered(tmp_path / "gone" / "deeper", fresh=True) is False
