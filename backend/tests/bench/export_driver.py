"""The process whose memory the peak bench measures: a real export of N chapters.

    python -m tests.bench.export_driver CHAPTERS DECODES [--profile]

It builds the synthetic library of the export tests (real files, a real migrated database), adds
the whole reference catalog with `--profile`, plays the real export pipeline over `CHAPTERS`
archives, then sends `DECODES` images through the real decode worker (a child process, so that
the parent of this one can count it). The last line of stdout is a JSON report. Run by
`harness.export_peak`, which samples `/proc` from outside; never imported by production code.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.imaging import ImageKind, make_thumbnail
from tests.bench.generator import CI_PROFILE, generate_catalog
from tests.export_env import export_env


def _malicious() -> object:
    path = Path(__file__).parents[1] / "fixtures" / "synthetic" / "generate_malicious.py"
    spec = importlib.util.spec_from_file_location("bench_synthetic_images", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _high_water_bytes() -> int:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) * 1024
    return 0


def main(argv: list[str]) -> int:
    chapters, decodes = int(argv[1]), int(argv[2])
    with export_env() as env:
        if "--profile" in argv:
            generate_catalog(env.engine, CI_PROFILE)
        series = env.new_series("Bench")
        for number in range(1, chapters + 1):
            env.add_chapter(series, str(number))
        report = env.pipeline.run(env.destination_id, job_id=new_ulid())
        page = _malicious().png_solid(1_500, 2_100)  # type: ignore[attr-defined]  # 3 Mpx page
        for _ in range(decodes):
            make_thumbnail(page, ImageKind.PAGE, max_edge=400)
    print(json.dumps({"ops": len(report.ops), "own_high_water_bytes": _high_water_bytes()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
