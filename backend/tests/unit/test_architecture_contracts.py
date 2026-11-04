"""Architecture guardrails (ENF-QUAL-002, ENF-LEG-010): written before the code."""

import ast
import configparser
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
SRC = BACKEND / "src"
SDK = SRC / "shelfhold" / "sdk"
CONFIG = BACKEND / "importlinter.ini"

CORE_TOP_LEVEL = {
    "catalog", "acquisition", "library", "export", "identity", "integrations",
    "jobs", "events", "plugin_host", "interfaces", "infrastructure", "config", "main", "cli",
}  # fmt: skip


def _config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    assert parser.read(CONFIG), "importlinter.ini missing"
    return parser


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _imported_modules(path: Path) -> list[str]:
    """Absolute dotted names imported by an SDK file, relative imports resolved."""
    rel = path.relative_to(SRC).with_suffix("")
    package = list(rel.parts) if path.name == "__init__.py" else list(rel.parts[:-1])
    if path.name == "__init__.py":
        package = package[:-1]
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - (node.level - 1)] if node.level else []
            prefix = ".".join(base + ([node.module] if node.module else [])).strip(".")
            found += [f"{prefix}.{alias.name}".strip(".") for alias in node.names]
            found.append(prefix)
    return found


def _is_core(name: str) -> bool:
    parts = name.split(".")
    return parts[0] == "shelfhold" and len(parts) > 1 and parts[1] in CORE_TOP_LEVEL


@pytest.mark.req("ENF-LEG-010", "ENF-QUAL-002")
def test_sdk_never_imports_core() -> None:
    offenders = [
        f"{p.name}: {name}"
        for p in SDK.rglob("*.py")
        for name in _imported_modules(p)
        if _is_core(name)
    ]
    assert not offenders, offenders


@pytest.mark.req("ENF-LEG-010")
def test_core_import_detector_catches_all_import_forms(tmp_path: Path) -> None:
    sdk = SRC / "shelfhold" / "sdk"
    probe = sdk / "_probe.py"
    for code in (
        "from shelfhold import catalog",
        "import shelfhold.catalog.domain",
        "from shelfhold.catalog import x",
        "from .. import catalog",
        "from ..catalog import x",
    ):
        probe.write_text(code + "\n", encoding="utf-8")
        try:
            assert any(_is_core(n) for n in _imported_modules(probe)), code
        finally:
            probe.unlink()


@pytest.mark.req("ENF-QUAL-002")
def test_importlinter_declares_forbidden_independence_and_layers() -> None:
    parser = _config()
    types = {parser[s]["type"] for s in parser.sections() if s.startswith("importlinter:contract:")}
    assert {"forbidden", "independence", "layers"} <= types


@pytest.mark.req("ENF-LEG-010", "ENF-QUAL-002")
def test_importlinter_sdk_contract_matches_core_top_level() -> None:
    section = _config()["importlinter:contract:sdk-without-core"]
    assert section["type"] == "forbidden"
    assert section["source_modules"].strip() == "shelfhold.sdk"
    assert set(_lines(section["forbidden_modules"])) == {f"shelfhold.{m}" for m in CORE_TOP_LEVEL}


@pytest.mark.req("ENF-QUAL-002")
def test_importlinter_independence_allows_public_modules() -> None:
    section = _config()["importlinter:contract:contexts-independent"]
    assert "shelfhold.** -> shelfhold.*.public" in _lines(section["ignore_imports"])


@pytest.mark.req("ENF-QUAL-002")
def test_importlinter_domain_contract_covers_all_domains() -> None:
    section = _config()["importlinter:contract:domain-pure"]
    assert _lines(section["source_modules"]) == ["shelfhold.*.domain"]


def _run_lint_imports(cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.argv = ['lint-imports', '--config', 'importlinter.ini'];"
            "from importlinter.cli import lint_imports_command as m; m()",
        ],
        cwd=cwd,
        env={"PYTHONPATH": str(cwd), "PATH": ""},
        capture_output=True,
        text=True,
        check=False,
    )


def _fake_package(root: Path, *, violating: bool) -> None:
    pkg = root / "fakepkg"
    (pkg / "sdk").mkdir(parents=True)
    (pkg / "core").mkdir()
    for init in (pkg / "__init__.py", pkg / "sdk" / "__init__.py", pkg / "core" / "__init__.py"):
        init.write_text("", encoding="utf-8")
    (pkg / "sdk" / "mod.py").write_text(
        "from fakepkg import core\n" if violating else "X = 1\n", encoding="utf-8"
    )
    (root / "importlinter.ini").write_text(
        textwrap.dedent(
            """\
            [importlinter]
            root_package = fakepkg

            [importlinter:contract:sdk-without-core]
            name = sdk never imports core
            type = forbidden
            source_modules =
                fakepkg.sdk
            forbidden_modules =
                fakepkg.core
            """
        ),
        encoding="utf-8",
    )


@pytest.mark.req("ENF-QUAL-002")
def test_lint_imports_fails_on_a_violating_package(tmp_path: Path) -> None:
    _fake_package(tmp_path, violating=True)
    result = _run_lint_imports(tmp_path)
    assert result.returncode != 0, result.stdout
    assert "BROKEN" in result.stdout


@pytest.mark.req("ENF-QUAL-002")
def test_lint_imports_passes_on_a_clean_package(tmp_path: Path) -> None:
    _fake_package(tmp_path, violating=False)
    result = _run_lint_imports(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.req("ENF-LEG-010")
def test_sdk_files_have_mit_spdx_header() -> None:
    files = list(SDK.rglob("*.py"))
    assert files, "the SDK must contain at least one module"
    missing = [
        str(p.relative_to(SDK))
        for p in files
        if "SPDX-License-Identifier: MIT" not in p.read_text(encoding="utf-8").split("\n", 3)[0]
    ]
    assert not missing, missing
