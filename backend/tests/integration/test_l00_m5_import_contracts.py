"""Lot L00 (M5): import contracts, reader fence, allowlist comments, traceability, velocity."""

import configparser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
CONFIG = BACKEND / "importlinter.ini"


def _config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    assert parser.read(CONFIG)
    return parser


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


@pytest.mark.req("ENF-QUAL-002")
def test_reading_contract_forbids_acquisition_and_export() -> None:
    section = _config()["importlinter:contract:reading-not-acquisition-export"]
    assert section["type"] == "forbidden"
    assert section["source_modules"].strip() == "shelfhold.reading"
    assert set(_lines(section["forbidden_modules"])) == {
        "shelfhold.acquisition",
        "shelfhold.export",
    }


@pytest.mark.req("ENF-QUAL-002")
def test_lite_contract_forbids_opds_import() -> None:
    section = _config()["importlinter:contract:lite-not-opds"]
    assert section["type"] == "forbidden"
    assert section["source_modules"].strip() == "shelfhold.interfaces.http_api.lite"
    assert _lines(section["forbidden_modules"]) == ["shelfhold.interfaces.http_api.opds"]


@pytest.mark.req("ENF-SEC-004")
def test_eslint_fences_foliate_js() -> None:
    text = (ROOT / "frontend" / "eslint.config.js").read_text(encoding="utf-8")
    assert "foliate-js" in text
    assert "src/lib/reader/engines/foliate.ts" in text


def test_allowlist_m5_entries_are_commented() -> None:
    text = (ROOT / "tools" / "production-reachability-allowlist.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if 'until = "M5"' in line:
            assert "#" in line, line


def test_allowlist_m6_entries_are_commented() -> None:
    text = (ROOT / "tools" / "production-reachability-allowlist.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if 'until = "M6"' in line:
            assert "#" in line, line
            assert "L" in line.split("#", 1)[1], line


@pytest.mark.req("ENF-SEC-007")
def test_ci_traceability_includes_m5_and_blocks() -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "for ms in M0 M1a M1b M2 M3 M4 M5; do" in text
    assert 'if [ "$ms" = M5 ]; then' not in text  # blocking since L25


@pytest.mark.req("ENF-GOV-005")
def test_velocite_m4_row_filled() -> None:
    text = (ROOT / "docs" / "governance" / "velocite.md").read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| M4 |"))
    assert row.count("|") >= 6
    assert "47 sur 47" in row or "PR" in row


@pytest.mark.req("ENF-SEC-007")
def test_ci_traceability_reports_m6_without_blocking() -> None:
    """M6 L00: the matrix of M6 is built and shown in the summary, in a step of its own that
    never fails the job; the loop of the delivered milestones stays M0 to M5 (L28 joins M6)."""
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'python3 tools/traceability.py --milestone M6 --output "$RUNNER_TEMP/M6.md"' in text
    assert "for ms in M0 M1a M1b M2 M3 M4 M5; do" in text
