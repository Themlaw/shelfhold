"""M3 requirements whose lot is not delivered yet: the test exists, red on purpose (xfail strict).

Each test states the smallest observable fact that the delivery of the lot makes true. As soon as
the lot lands the test passes, `strict` turns that into a failure and the marker must go, so the
gap cannot be forgotten nor silently closed. The requirements stay "covered outside the required
CI" in the traceability matrix until then (tools/traceability-exceptions.toml).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.req("EF-UX-004")
@pytest.mark.xfail(
    strict=True,
    reason="EF-UX-004: M3 L48 (screen "Connect a device", QR of the Kavita entry points) "
    "is not delivered: no `appareils` route, no local QR generator",
)
def test_connect_device_qr_kavita_endpoints() -> None:
    page = ROOT / "frontend" / "src" / "routes" / "appareils" / "+page.svelte"
    assert page.is_file(), "the "Connect a device" screen does not exist"
    source = page.read_text(encoding="utf-8")
    assert "opds" in source.lower()  # the entry points of Kavita are offered
    package = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    assert any("qr" in name.lower() for name in deps), "no local QR generator (no network)"


@pytest.mark.req("ENF-UX-001")
@pytest.mark.xfail(
    strict=True,
    reason="ENF-UX-001: M3 L57 (timed E2E "first 10 minutes", ≤ 10 min and ≤ 6 actions) is "
    "not delivered: no M3 journey spec",
)
def test_first_book_under_10_minutes() -> None:
    spec = ROOT / "e2e" / "tests" / "test_m3_journey.spec.ts"
    assert spec.is_file(), "the timed first-book journey does not exist"
    text = spec.read_text(encoding="utf-8")
    assert "test_first_book_under_10_minutes" in text
    assert "600" in text, "the 10 minute budget is not asserted"


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
