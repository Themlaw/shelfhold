"""ENF-PERF-007: a run that exports one series in bulk writes its mirror once, not per chapter.

The nightly of the contention bench broke on the shape « 500 chapters of one series » (an
aggravation of about x12 for a bound of x3, never loosened) while the brother shape « 500
chapters over 500 series » stayed within it: the provenance mirror of a series was rewritten
after every file placed, and each rewrite covered the whole series file again — a quadratic
cost over the chapters of one series, a linear one over the chapters of many series.

What is pinned here is the complexity, not a duration: for one export run of n chapters of a
single series, the series' mirror file is written a bounded number of times (once: the database
is the authority, the file is one projection of it), and what is written grows linearly in n.
The final content is asserted with it: every chapter has its line, whatever the shape of the
run (EF-EXP-063).
"""

from dataclasses import dataclass
from typing import Any, Final

import pytest

from shelfhold.export.domain.physical_roots import RESERVED_DIR
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.ids import new_ulid
from tests.export_env import export_env

CHAPTERS: Final = 8
LINEAR_MAX_FACTOR: Final = 2.5  # n against 2n: a linear cost doubles, a quadratic one quadruples


@dataclass(frozen=True)
class MirrorTraffic:
    """What one export run wrote to the series' mirror file, and what that file holds at the end."""

    writes: int
    bytes_written: int
    entries: int


def export_one_series(chapters: int) -> MirrorTraffic:
    """One run exporting `chapters` new chapters of a single series, and the traffic to the
    series' mirror file: `Fsops.write_mirror` is the sole writer of the manifest folder, so
    everything it wrote during the run is the projection at work."""
    real = Fsops.write_mirror
    written: list[int] = []

    def counting(self: Fsops, root_path: str, name: str, data: bytes) -> Any:
        written.append(len(data))
        return real(self, root_path, name, data)

    with export_env() as env, pytest.MonkeyPatch.context() as patch:
        series = env.new_series()
        for number in range(1, chapters + 1):
            env.add_chapter(series, str(number))
        patch.setattr(Fsops, "write_mirror", counting)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert len(env.files()) == chapters  # every chapter was placed, none refused
        (path,) = (env.lib.library / RESERVED_DIR / "manifest").rglob("*.jsonl")
        lines = path.read_text().splitlines()
    return MirrorTraffic(len(written), sum(written), len(lines) - 1)  # the first line is the header


@pytest.mark.req("ENF-PERF-007")
def test_one_series_export_writes_its_mirror_once_not_once_per_chapter() -> None:
    """8 chapters of one series, then 16: the same single projection each time, twice as big."""
    small = export_one_series(CHAPTERS)
    large = export_one_series(2 * CHAPTERS)
    assert (small.entries, large.entries) == (CHAPTERS, 2 * CHAPTERS)
    assert (small.writes, large.writes) == (1, 1), (small.writes, large.writes)
    assert large.bytes_written <= LINEAR_MAX_FACTOR * small.bytes_written, (
        f"the mirror of 2n chapters took {large.bytes_written} bytes to write against "
        f"{small.bytes_written} for n: the cost is not linear in the chapters of one series"
    )
