"""ENF-OPS-005: the RAM and disk budget of the reference deployment is documented."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BUDGET = ROOT / "docs" / "admin" / "budget-ram.md"
SPIKE_S4 = ROOT / "spikes" / "s4" / "RESULTATS.md"


def _s4_figures() -> list[str]:
    """The measured S4 values, as written in the spike (decimal comma), without the units."""
    text = SPIKE_S4.read_text(encoding="utf-8")
    rows = [line for line in text.splitlines() if line.startswith("| ") and "Mo**" in line]
    return [m.group(1) for line in rows for m in re.finditer(r"\*\*([\d,]+) Mo\*\*", line)]


@pytest.mark.req("ENF-OPS-005")
def test_ram_budget_documented_with_reference_profile() -> None:
    text = BUDGET.read_text(encoding="utf-8")
    # The reference profile: Shelfhold + Suwayomi + Kavita, with each service budget.
    for service in ("Shelfhold", "Suwayomi", "Kavita"):
        assert service in text, f"{service} missing from the reference profile"
    assert "profil de référence" in text.lower()
    assert "750" in text  # Suwayomi, at least 750 Mo
    assert "2 Go" in text  # Kavita peaks
    # The figures measured by spike S4 are quoted, not paraphrased.
    figures = _s4_figures()
    assert len(figures) >= 3, "spike S4 no longer lists its measured values"
    for figure in figures:
        assert f"{figure} Mo" in text, f"S4 measure {figure} Mo missing from budget-ram.md"
    # The disk budget per mode is part of the same promise (passage x2, kept copy x2 to x3).
    assert "mode passage" in text.lower()
    assert "copie conservée" in text.lower()
    assert "mem_limit" in text  # the compose limit is explained, not just stated
