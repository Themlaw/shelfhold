"""Garde-fous d'architecture (ENF-QUAL-002, ENF-LEG-004) : écrits avant le code."""

import ast
import configparser
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
SDK = BACKEND / "src" / "shelfhold" / "sdk"
CONFIG = BACKEND / "importlinter.ini"

CORE_TOP_LEVEL = {
    "catalog", "acquisition", "library", "export", "identity", "integrations",
    "jobs", "events", "plugin_host", "interfaces", "infrastructure", "config", "main", "cli",
}  # fmt: skip


def _contract_types() -> dict[str, str]:
    parser = configparser.ConfigParser()
    assert parser.read(CONFIG), "importlinter.ini manquant"
    return {
        section: parser[section]["type"]
        for section in parser.sections()
        if section.startswith("importlinter:contract:")
    }


def test_sdk_never_imports_core() -> None:
    offenders: list[str] = []
    for path in SDK.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                parts = name.split(".")
                if parts[0] == "shelfhold" and len(parts) > 1 and parts[1] in CORE_TOP_LEVEL:
                    offenders.append(f"{path.name}: {name}")
    assert not offenders, offenders


def test_importlinter_declares_forbidden_independence_and_layers() -> None:
    types = set(_contract_types().values())
    assert {"forbidden", "independence", "layers"} <= types


def test_importlinter_declares_sdk_without_core_contract() -> None:
    parser = configparser.ConfigParser()
    parser.read(CONFIG)
    section = parser["importlinter:contract:sdk-without-core"]
    assert section["type"] == "forbidden"
    assert section["source_modules"].strip() == "shelfhold.sdk"


def test_sdk_files_have_mit_spdx_header() -> None:
    files = [p for p in SDK.rglob("*.py")]
    assert files, "le SDK doit contenir au moins un module"
    missing = [
        str(p.relative_to(SDK))
        for p in files
        if "SPDX-License-Identifier: MIT" not in p.read_text(encoding="utf-8").split("\n", 3)[0]
    ]
    assert not missing, missing
