"""M5 closure (L25): decisions, CHANGELOG, roadmap, governance and the overlap review are written.

Texts only: nothing here runs product code. Each check keeps a closure section from being
forgotten or left empty (REGLES-GOAL, « un jalon est terminé uniquement quand »).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body of `heading` (a line `## …` or `### …`) up to the next heading of the same rank."""
    rank = len(heading) - len(heading.lstrip("#"))
    match = re.search(rf"^{re.escape(heading)}(?=\s|$).*$", text, re.M)
    assert match, f"missing section {heading!r}"
    rest = text[match.end() :]
    stop = re.search(rf"^#{{1,{rank}}} ", rest, re.M)
    return rest[: stop.start()] if stop else rest


@pytest.mark.req("ENF-GOV-002", "ENF-GOV-003")
def test_overlap_review_m5_has_sources_and_a_decision() -> None:
    text = _read("docs/governance/chevauchement-M5.md")
    for project in ("Kavita", "Komga", "Suwayomi"):
        assert re.search(rf"\| {project}.*https://github\.com/", text), project
    assert "## Décision" in text
    assert "Q21" in text and "EF-INT-012" in text
    assert re.search(r"^## Relecture du .* pré-mortem$", text, re.M)


@pytest.mark.req("ENF-GOV-003")
def test_q21_is_revised_at_the_exit_of_m5() -> None:
    q21 = _section(_read("docs/spec/06-points-discutables.md"), "## Q21")
    assert "Révisée à la sortie de M5" in q21
    assert "condition réévaluée à la sortie de M5" not in q21


@pytest.mark.req("ENF-GOV-005")
def test_velocity_records_the_m5_milestone() -> None:
    row = re.search(r"^\| M5 \|(.*)$", _read("docs/governance/velocite.md"), re.M)
    assert row, "no M5 row"
    cells = [c.strip() for c in row.group(1).split("|")][:-1]
    assert all(cells), "an empty cell in the M5 row"
    assert "2026-11-17" in cells[0] and "345" in cells[0] and "400" in cells[0]
    assert "médiane" in row.group(1)


@pytest.mark.req("ENF-GOV-006")
def test_portes_journal_has_the_m5_review() -> None:
    assert re.search(
        r"^\| M5 \(revue de jalon\) \| 2026-11-20 \|", _read("docs/governance/portes.md"), re.M
    )


@pytest.mark.req("ENF-GOV-003")
def test_pre_mortem_is_reread_at_m5() -> None:
    text = _read("docs/governance/pre-mortem.md")
    rows = {m.group(1): m.group(0) for m in re.finditer(r"^\| (\d+) \|.*$", text, re.M)}
    for number in ("1", "3", "4", "5", "7", "8"):
        assert "2026-11-20 (M5" in rows[number], number
    # the milestone brings an exposure of its own: one row names it
    assert re.search(r"^\| 9 \|.*(lecteur|contenu).*\|$", text, re.M)


@pytest.mark.req("ENF-GOV-005", "ENF-GOV-003")
def test_roadmap_reports_the_closure_of_m5() -> None:
    text = _read("ROADMAP.md")
    assert re.search(r"^\| M5 \|.*`v1\.2\.0`", text, re.M)
    assert "## Bilan de M5" in text
    bilan = _section(text, "## Bilan de M5")
    for word in ("Livré", "Coupes", "Reporté en M6", "Encore ouvert"):
        assert word in bilan, word
    assert "Komga" in bilan and "Q21" in bilan


@pytest.mark.req("ENF-GOV-005", "ENF-GOV-003")
def test_m5_decisions_close_the_milestone() -> None:
    text = _read("docs/decisions/M5.md")
    closure = _section(text, "## Clôture de M5")
    for heading in (
        "### Contrôle du piège de la checklist (§1)",
        "### Contrôle manuel à faire par le porteur (D-P3)",
        "### Révision de Q21",
        "### Revue de chevauchement",
        "### Contradicteur de jalon M5",
        "### Points discutables du jalon",
        "### Reporté en M6",
    ):
        assert heading in closure, heading
    pitfall = _section(closure, "### Contrôle du piège de la checklist (§1)")
    for requirement in (
        "EF-BIB-015", "EF-BIB-017", "EF-INT-007", "EF-LEC-001", "EF-LEC-002", "EF-LEC-003",
        "EF-LEC-005", "EF-LEC-006", "EF-LEC-007", "EF-LEC-008", "EF-LEC-009", "EF-LEC-010",
        "EF-LEC-011", "EF-LEC-012", "EF-USR-004", "EF-USR-008", "ENF-PERF-006",
        "EF-EXP-058", "EF-LEC-014", "EF-USR-022", "EF-USR-024", "ENF-A11Y-002", "ENF-COMP-002",
        "ENF-SEC-004", "ENF-SEC-013",
    ):  # fmt: skip
        assert requirement in pitfall, requirement


@pytest.mark.req("ENF-DOC-001")
def test_changelog_1_2_0_is_shipped_by_1_3_0() -> None:
    text = _read("CHANGELOG.md")
    head = re.search(r"^## 1\.2\.0 - (.*)$", text, re.M)
    assert head and "incluse dans la 1.3.0" in head.group(1), "1.2.0 is never released alone"
    block = _section(text, "## 1.2.0")
    for word in (
        "/content",
        "kosync",
        "OPDS 2.0",
        "PWA",
        "hors ligne",
        "Continuer la lecture",
        "OpenID Connect",
    ):
        assert word in block, word
    for heading in ("### Ajouté", "### Modifié", "### Sécurité", "### Reporté en M6"):
        assert heading in block, heading
    assert "Aucune migration" not in block or "0100" in block
