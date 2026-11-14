"""M3 requirements whose lot is not delivered yet: the test exists, red on purpose (xfail strict).

Each test states the smallest observable fact that the delivery of the lot makes true. As soon as
the lot lands the test passes, `strict` turns that into a failure and the marker must go, so the
gap cannot be forgotten nor silently closed. The requirements stay "covered outside the required
CI" in the traceability matrix until then (tools/traceability-exceptions.toml).
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.req("ENF-UX-001")
@pytest.mark.xfail(
    strict=True,
    reason="ENF-UX-001: the timed journey of M3 L57 exists but is an expected failure: the "
    "first-run screens do not reach a first book alone (no sign-in screen, no screen binds a "
    "shelf to Kavita, the folder screen is a dead end, 10 gestures against a budget of 6)",
)
def test_first_book_under_10_minutes() -> None:
    spec = ROOT / "e2e" / "tests" / "test_m3_journey.spec.ts"
    assert spec.is_file(), "the timed first-book journey does not exist"
    text = spec.read_text(encoding="utf-8")
    assert "test_first_book_under_10_minutes" in text
    assert "BUDGET_S = 600;" in text, "the 10 minute budget is not asserted"
    assert "BUDGET_ACTIONS = 6;" in text, "the budget of 6 actions is not asserted"
    # the journey is covered when nothing marks it as a known gap any more
    assert "test.fail(true" not in text, "the journey is still an expected failure"


@pytest.mark.req("ENF-GOV-004")
@pytest.mark.xfail(
    strict=True,
    reason="ENF-GOV-004 (M2, deferred by the maintainer on 2026-11-11): no update checker "
    "(SHELFHOLD_UPDATE_CHECK_URL) and no `publish-mirror` workflow yet",
)
def test_update_check_failure_is_silent() -> None:
    sources = "\n".join(
        p.read_text(encoding="utf-8") for p in (ROOT / "backend" / "src").rglob("*.py")
    )
    assert "SHELFHOLD_UPDATE_CHECK_URL" in sources, "the update checker does not exist"
    assert (ROOT / ".github" / "workflows" / "publish-mirror.yml").is_file()
