"""Lot L00 (M5): import contracts, reader import fence, allowlist comments, traceability, velocity."""

import configparser
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT / "backend"
CONFIG = BACKEND / "importlinter.ini"


def _config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    assert parser.read(CONFIG)
    return parser


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def test_reading_contract_forbids_acquisition_and_export() -> None:
    section = _config()["importlinter:contract:reading-not-acquisition-export"]
    assert section["type"] == "forbidden"
    assert section["source_modules"].strip() == "shelfhold.reading"
    assert set(_lines(section["forbidden_modules"])) == {
        "shelfhold.acquisition",
        "shelfhold.export",
    }


def test_lite_contract_forbids_opds_import() -> None:
    section = _config()["importlinter:contract:lite-not-opds"]
    assert section["type"] == "forbidden"
    assert section["source_modules"].strip() == "shelfhold.interfaces.http_api.lite"
    assert _lines(section["forbidden_modules"]) == ["shelfhold.interfaces.http_api.opds"]


def test_eslint_fences_foliate_js() -> None:
    text = (ROOT / "frontend" / "eslint.config.js").read_text(encoding="utf-8")
    assert "foliate-js" in text
    assert "src/lib/reader/engines/foliate.ts" in text


def test_allowlist_m5_entries_are_commented() -> None:
    text = (ROOT / "tools" / "production-reachability-allowlist.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if 'until = "M5"' in line:
            assert "#" in line, line


def test_ci_traceability_includes_m5_report_only() -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "for ms in M0 M1a M1b M2 M3 M4 M5; do" in text
    assert 'if [ "$ms" = M5 ]; then' in text


def test_velocite_m4_row_filled() -> None:
    text = (ROOT / "docs" / "governance" / "velocite.md").read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| M4 |"))
    assert row.count("|") >= 6
    assert "47 sur 47" in row or "PR" in row
