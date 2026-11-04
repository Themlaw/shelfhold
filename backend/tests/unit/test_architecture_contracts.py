"""Architecture guardrails (ENF-QUAL-002, ENF-LEG-010): written before the code."""

import ast
import configparser
import shutil
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


# --- L00: single-entry-point and layering contracts, each proven by a violation -------------------

FORBIDDEN_IN = "BROKEN"


def _run_in_copy(tmp_path: Path, files: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run the real importlinter.ini against a copy of the package plus extra modules."""
    shutil.copytree(
        SRC / "shelfhold", tmp_path / "shelfhold", ignore=shutil.ignore_patterns("__pycache__")
    )
    shutil.copy(CONFIG, tmp_path / "importlinter.ini")
    for rel, code in files.items():
        target = tmp_path / "shelfhold" / rel
        for parent in reversed([target.parent, *target.parent.parents]):
            if parent == tmp_path / "shelfhold" or tmp_path / "shelfhold" in parent.parents:
                parent.mkdir(exist_ok=True)
                (parent / "__init__.py").touch()
        target.write_text(code, encoding="utf-8")
    return _run_lint_imports(tmp_path)


def _contract_name(contract: str) -> str:
    return _config()[f"importlinter:contract:{contract}"]["name"]


# (contract, offending module, code, sanctioned module where the same code is allowed or None)
VIOLATIONS = [
    ("xml-single-entry", "infrastructure/other.py", "import xml.etree.ElementTree\n",
     "infrastructure/xml/safe_parse.py"),
    ("xml-single-entry", "library/other.py", "import lxml.etree\n", None),
    ("archives-single-entry", "library/other.py", "import zipfile\n",
     "infrastructure/archives/safe_open.py"),
    ("archives-single-entry", "export/other.py", "import tarfile\n", None),
    ("sanitize-single-entry", "catalog/other.py", "import nh3\n",
     "infrastructure/content_trust/sanitize.py"),
    ("lang-single-producer", "library/other.py", "import langcodes\n", "catalog/lang.py"),
    ("replica-single-count", "export/other.py", "from shelfhold.library import replica\n", None),
    ("fsops-sole-writer", "library/other.py", "import shutil\n", "infrastructure/fsops/io.py"),
    ("suwayomi-confined", "acquisition/other.py",
     "import shelfhold.plugin_host.adapters.meta.suwayomi\n", None),
    ("lncrawl-confined", "export/other.py",
     "import shelfhold.plugin_host.adapters.meta.lncrawl\n", None),
    ("export-not-acquisition", "export/application/other.py",
     "from shelfhold.acquisition import public\n", None),
    ("library-layers", "library/domain/other.py",
     "import shelfhold.library.application\n", None),
    ("acquisition-layers", "acquisition/domain/other.py",
     "import shelfhold.acquisition.adapters\n", None),
    ("export-layers", "export/domain/other.py", "import shelfhold.export.targets\n", None),
    ("identity-layers", "identity/application/other.py",
     "import shelfhold.identity.adapters\n", None),
]  # fmt: skip


@pytest.mark.req("ENF-QUAL-002", "ENF-SEC-014", "ENF-SEC-002", "ENF-SEC-015", "ENF-I18N-005")
@pytest.mark.parametrize(
    ("contract", "module", "code", "sanctioned"),
    VIOLATIONS,
    ids=[f"{v[0]}:{v[1]}" for v in VIOLATIONS],
)
def test_each_contract_breaks_on_a_violation_and_allows_its_sanctioned_module(
    tmp_path: Path, contract: str, module: str, code: str, sanctioned: str | None
) -> None:
    result = _run_in_copy(tmp_path / "bad", {module: code})
    assert result.returncode != 0, result.stdout
    assert f"{_contract_name(contract)} BROKEN" in result.stdout, result.stdout
    if sanctioned:
        ok = _run_in_copy(tmp_path / "ok", {sanctioned: code})
        assert f"{_contract_name(contract)} BROKEN" not in ok.stdout, ok.stdout
        assert ok.returncode == 0, ok.stdout


@pytest.mark.req("ENF-QUAL-002")
def test_every_new_contract_is_declared_in_the_config() -> None:
    declared = {s.split(":", 2)[2] for s in _config().sections() if s.startswith("importlinter:c")}
    assert {v[0] for v in VIOLATIONS} <= declared
