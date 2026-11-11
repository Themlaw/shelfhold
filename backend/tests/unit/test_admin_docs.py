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
    assert "conservée" in text.lower()  # the kept-copy mode
    assert "mem_limit" in text  # the compose limit is explained, not just stated


ADMIN = ROOT / "docs" / "admin"
PUBLISHED_DOCS = [*ADMIN.glob("*.md"), ROOT / "docs" / "configuration.md"]


@pytest.mark.req("ENF-PRIV-002")
def test_exposure_doc_covers_discretion_and_the_recourse_procedure() -> None:
    text = (ADMIN / "exposition-publique.md").read_text(encoding="utf-8")
    for part in ("X-Robots-Tag: noindex, nofollow", "Disallow: /", "Open Graph"):
        assert part in text, part  # what the instance does
    assert "Search Console" in text  # declare the domain
    assert "démo" in text.lower() and "domaine de l'instance" in text  # no demo on that domain
    assert "Safe Browsing" in text and "réexamen" in text  # the recourse procedure


@pytest.mark.req("EF-INT-027")
def test_restore_doc_puts_the_schema_assets_in_the_backup_set() -> None:
    text = (ADMIN / "restauration.md").read_text(encoding="utf-8")
    assert "schema.sql" in text and "data-dictionary.md" in text
    assert "jeu de sauvegarde" in text and "sqlite3" in text
    assert "sauvegarde intégrée les inclut" in text  # the integrated backup carries them itself


@pytest.mark.req("ENF-LEG-001", "ENF-PRIV-002")
@pytest.mark.parametrize("page", PUBLISHED_DOCS, ids=lambda p: p.name)
def test_published_docs_name_no_personal_path_or_host(page: Path) -> None:
    text = page.read_text(encoding="utf-8")
    assert not re.search(r"/home/|/Users/|\.ts\.net", text), page.name
    addresses = re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text)
    # loopback, the "any address" form and Kavita version numbers are the only dotted quads
    assert [a for a in addresses if not a.startswith(("127.", "0."))] == [], page.name


@pytest.mark.req("EF-INT-015", "ENF-SEC-005")
def test_the_backup_recipe_sets_a_private_umask_before_it_writes_the_key() -> None:
    text = (ROOT / "docs" / "admin" / "sauvegarde.md").read_text(encoding="utf-8")
    recipe = text[text.index("```sh\numask 077") :]
    assert recipe.index("umask 077") < recipe.index("cle-ephemere.txt")
    assert "locked" in text and "log_n = 18" in text
