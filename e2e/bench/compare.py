#!/usr/bin/env python3
"""Release gate of the reader page-turn bench (ENF-PERF-006, M5 L15).

Compares the report of a fresh run (test-results/bench_reader_page_turn.json) with the last
recorded result (reference.json, versioned because workflow artifacts expire). Two rules, in
order: the spec's budget first — p95 <= 150 ms, and a comparison never loosens a threshold —
then the release rule (docs/spec/03-roadmap-tdd.md §4): a regression of more than 20 % over
the recorded p95 blocks the release. Both runs must come from a comparable environment; when
the bench environment changes, the reference is re-recorded deliberately, like the screens
reference (e2e/README.md). See docs/governance/benchmarks.md.

Usage: compare.py REPORT [REFERENCE]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BUDGET_MS = 150.0
TOLERANCE_PCT = 20.0


def load(path: str | Path, what: str) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"{what} unreadable ({path}): {error}") from error


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(f"usage: {argv[0]} REPORT [REFERENCE]", file=sys.stderr)
        return 2
    report = load(argv[1], "report")
    reference = load(argv[2] if len(argv) == 3 else Path(__file__).parent / "reference.json", "reference")
    p95 = float(report["p95_ms"])
    recorded = float(reference["p95_ms"])
    if p95 > BUDGET_MS:
        print(
            f"ENF-PERF-006: p95 {p95:.1f} ms exceeds the budget {BUDGET_MS:.0f} ms — release blocked",
            file=sys.stderr,
        )
        return 1
    allowed = recorded * (1 + TOLERANCE_PCT / 100)
    if p95 > allowed:
        print(
            f"regression: p95 {p95:.1f} ms is more than {TOLERANCE_PCT:.0f} % over the recorded "
            f"{recorded:.1f} ms (allowed {allowed:.1f} ms) — release blocked (03 §4)",
            file=sys.stderr,
        )
        return 1
    print(f"reader page turn: p95 {p95:.1f} ms (recorded {recorded:.1f} ms, budget {BUDGET_MS:.0f} ms)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
