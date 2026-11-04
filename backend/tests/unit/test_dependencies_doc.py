"""ENF-GOV-002: docs/dependencies.md and the 'critical dependency behind its adapter' contract."""

import configparser
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
DOC = BACKEND.parent / "docs" / "dependencies.md"
CRITICAL = ["Huey", "foliate-js", "lncrawl", "Kavita", "kepubify", "Suwayomi", "comicbox"]
COLUMNS = [
    "Rôle",
    "Facteur bus",
    "Licence",
    "Dernière release",
    "Repli",
    "Signal de déclenchement",
    "Adaptateur",
]


def _rows() -> dict[str, dict[str, str]]:
    lines = [line for line in DOC.read_text(encoding="utf-8").splitlines() if line.startswith("|")]
    header = [cell.strip() for cell in lines[0].strip("|").split("|")]
    assert header[0] == "Dépendance"
    assert header[1:] == COLUMNS
    rows: dict[str, dict[str, str]] = {}
    for line in lines[2:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        rows[cells[0]] = dict(zip(header[1:], cells[1:], strict=True))
    return rows


def test_dependencies_md_lists_every_critical_dependency() -> None:
    rows = _rows()
    for name in CRITICAL:
        assert name in rows, f"{name} missing from docs/dependencies.md"
        for column in COLUMNS:
            assert rows[name][column], f"{name}: empty column {column}"
            assert "TODO" not in rows[name][column]


def test_python_critical_dependencies_are_confined_to_their_adapter() -> None:
    parser = configparser.ConfigParser()
    parser.read(BACKEND / "importlinter.ini")
    confined = {
        module.strip()
        for section in parser.sections()
        if section.startswith("importlinter:contract:critical-")
        for module in parser[section]["forbidden_modules"].splitlines()
        if module.strip()
    }
    assert {"huey", "comicbox"} <= confined


def test_fastapi_is_confined_to_interfaces() -> None:
    parser = configparser.ConfigParser()
    parser.read(BACKEND / "importlinter.ini")
    section = parser["importlinter:contract:fastapi-only-in-interfaces"]
    assert "fastapi" in section["forbidden_modules"]
    assert "starlette" in section["forbidden_modules"]
