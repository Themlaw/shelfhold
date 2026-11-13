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
    "jobs", "events", "plugin_host", "interfaces", "infrastructure", "config", "i18n", "main",
}  # fmt: skip


def _config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    assert parser.read(CONFIG), "importlinter.ini missing"
    return parser


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _imported_modules(path: Path, package: list[str] | None = None) -> list[str]:
    """Absolute dotted names imported by an SDK file, relative imports resolved.

    `package` is the dotted package of the file, as a list; by default it is derived from the
    path under src/, which a file elsewhere (a test probe in tmp_path) cannot do.
    """
    if package is None:
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
    probe = tmp_path / "_probe.py"  # never write into src/: other tests walk it under xdist
    for code in (
        "from shelfhold import catalog",
        "import shelfhold.catalog.domain",
        "from shelfhold.catalog import x",
        "from .. import catalog",
        "from ..catalog import x",
    ):
        probe.write_text(code + "\n", encoding="utf-8")
        assert any(_is_core(n) for n in _imported_modules(probe, ["shelfhold", "sdk"])), code


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
    replica = tmp_path / "shelfhold" / "library" / "replica.py"
    if not replica.exists():  # the registry module of a later lot, so the contract has a target
        replica.touch()
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
     "infrastructure/xml/other.py"),
    ("xml-single-entry", "library/other.py", "import lxml.etree\n", None),
    ("xml-single-entry", "library/other.py", "import defusedxml.ElementTree\n",
     "infrastructure/xml/other.py"),
    ("xml-single-entry", "library/other.py", "import pyexpat\n", None),
    ("xml-single-entry", "library/other.py", "import plistlib\n", None),
    ("xml-single-entry", "library/other.py", "import xmlrpc.client\n", None),
    ("archives-single-entry", "library/other.py", "import zipfile\n",
     "infrastructure/archives/other.py"),
    ("archives-single-entry", "export/other.py", "import tarfile\n", None),
    ("archives-single-entry", "export/other.py", "import zipfile\n",
     "infrastructure/archives/other.py"),
    ("sanitize-single-entry", "catalog/other.py", "import nh3\n",
     "infrastructure/content_trust/sanitize.py"),
    ("sanitize-single-entry", "catalog/other.py", "import nh3\n",
     "infrastructure/content_trust/xhtml_neutralizer.py"),
    ("sanitize-single-entry", "infrastructure/content_trust/epub_scan.py", "import nh3\n",
     "infrastructure/content_trust/sanitize.py"),
    ("sanitize-single-entry", "infrastructure/content_trust/other.py", "import nh3\n", None),
    ("sanitize-single-entry", "catalog/other.py", "import bleach\n", None),
    ("pillow-confined", "library/other.py", "import PIL.Image\nimport pypdf\nimport resvg_py\n",
     "infrastructure/imaging/worker.py"),
    ("pillow-confined", "infrastructure/imaging/other.py", "from PIL import ImageOps\n", None),
    ("pypdf-confined", "library/other.py", "import PIL.Image\nimport pypdf\nimport resvg_py\n",
     "infrastructure/imaging/worker.py"),
    ("pypdf-confined", "infrastructure/content_trust/other.py", "from pypdf import PdfReader\n",
     None),
    ("resvg-confined", "library/other.py", "import PIL.Image\nimport pypdf\nimport resvg_py\n",
     "infrastructure/imaging/worker.py"),
    ("resvg-confined", "infrastructure/packaging/cbz/other.py",
     "from resvg_py import svg_to_bytes\n", None),
    ("http-client-single-entry", "library/other.py", "import httpx2\n",
     "infrastructure/fetcher/other.py"),
    ("http-client-single-entry", "export/other.py", "import requests\n", None),
    ("http-client-single-entry", "export/other.py", "import httpcore2\n", None),
    ("http-client-single-entry", "acquisition/other.py", "import aiohttp\n", None),
    ("http-client-single-entry", "export/other.py", "import socket\n",
     "infrastructure/fetcher/other.py"),
    ("lang-single-producer", "library/other.py", "import langcodes\n", "catalog/lang.py"),
    ("replica-single-count", "export/other.py", "import shelfhold.library.replica\n", None),
    ("fsops-sole-writer", "library/other.py", "import shutil\n", "infrastructure/fsops/io.py"),
    ("suwayomi-confined", "acquisition/other.py",
     "import shelfhold.plugin_host.adapters.meta.suwayomi\n",
     "plugin_host/adapters/meta/suwayomi/client.py"),
    ("lncrawl-confined", "export/other.py",
     "import shelfhold.plugin_host.adapters.meta.lncrawl\n", None),
    ("suwayomi-confined", "plugin_host/adapters/meta/lncrawl/other.py",
     "import shelfhold.plugin_host.adapters.meta.suwayomi\n", None),
    ("suwayomi-confined", "plugin_host/other.py",
     "import shelfhold.plugin_host.adapters.meta.suwayomi\n", None),
    ("export-not-acquisition", "export/application/other.py",
     "from shelfhold.acquisition import public\n", None),
    ("library-layers", "library/domain/other.py",
     "import shelfhold.library.application\n", None),
    ("acquisition-layers", "acquisition/domain/other.py",
     "import shelfhold.acquisition.adapters\n", None),
    ("export-layers", "export/domain/other.py", "import shelfhold.export.targets\n", None),
    ("identity-layers", "identity/application/other.py",
     "import shelfhold.identity.adapters\n", None),
    # M1b L00
    ("webauthn-confined", "library/other.py", "import webauthn\n", "identity/webauthn/other.py"),
    ("webauthn-confined", "identity/adapters/other.py", "import webauthn\n", None),
    ("sablier-confined", "export/other.py", "import shelfhold.integrations.sablier\n",
     "integrations/sablier/client.py"),
    ("sablier-confined", "integrations/apprise/other.py",
     "import shelfhold.integrations.sablier\n", None),
    ("backup-dir-sole-writer", "export/other.py", "import shelfhold.ops.backup.store\n",
     "ops/backup/run.py"),
    ("backup-dir-sole-writer", "ops/status_other.py", "import shelfhold.ops.backup.store\n", None),
    ("third-party-plugins-single-loader", "identity/other.py", "import importlib.metadata\n",
     "plugin_host/loader.py"),
    ("third-party-plugins-single-loader", "export/other.py", "import pkgutil\n", None),
    # M2 L00: some guarded modules do not exist yet; each violation fabricates its own
    ("epub-derivative-single-producer", "infrastructure/content_trust/xhtml_neutralizer.py",
     "import nh3\nimport shelfhold.infrastructure.content_trust.sanitize\n", None),
    ("epub-derivative-single-producer", "infrastructure/packaging/epub/writer.py",
     "import shelfhold.infrastructure.content_trust.sanitize\n",
     "infrastructure/packaging/comicinfo/other.py"),
    ("opf-sole-writer", "infrastructure/content_trust/epub_scan.py",
     "import shelfhold.infrastructure.packaging.epub\n", None),
    ("opf-sole-writer", "infrastructure/packaging/cbz/other.py",
     "import shelfhold.infrastructure.packaging.epub\n", "export/other.py"),
    ("native-adapters-sdk-only", "plugin_host/adapters/native/demo/client.py",
     "import shelfhold.infrastructure.fetcher\n", None),
    ("native-adapters-sdk-only", "plugin_host/adapters/native/demo/other.py",
     "import shelfhold.plugin_host.registry\n", None),
    ("metadata-not-export", "catalog/application/metadata.py",
     "from shelfhold.export import public\n", "interfaces/other.py"),
    # M3 L00: `replica-single-reader` was blind (root source); it now names `schema.replica`
    ("replica-single-reader", "export/synthetic.py",
     "import shelfhold.infrastructure.schema.replica\n", "library/replica.py"),
    ("replica-single-reader", "library/adapters/synthetic.py",
     "from shelfhold.infrastructure.schema import replica\n", "library/replica.py"),
    ("replica-single-reader", "infrastructure/clock.py",
     "import shelfhold.infrastructure.schema.replica\n", None),
    ("fts-sole-reader", "infrastructure/clock.py",
     "import shelfhold.infrastructure.schema.search\n", None),
    # M3 L00: synthetic modules, one violation per new contract
    ("http-no-infrastructure-adapter", "interfaces/http_api/synthetic.py",
     "import shelfhold.infrastructure.fsops\n", "interfaces/cli/synthetic.py"),
    ("http-no-infrastructure-adapter", "interfaces/http_api/synthetic.py",
     "from shelfhold.library.adapters import storage\n", "jobs/synthetic.py"),
    ("http-no-infrastructure-adapter", "interfaces/http_api/synthetic.py",
     "import shelfhold.infrastructure.db\n", "catalog/application/synthetic.py"),
    ("fts-sole-reader", "catalog/application/synthetic.py",
     "import shelfhold.infrastructure.schema.search\n", "catalog/application/search/reader.py"),
    ("fts-sole-reader", "export/synthetic.py",
     "from shelfhold.infrastructure.schema import search\n", None),
    ("identity-not-export-acquisition", "identity/application/synthetic.py",
     "from shelfhold.export import public\n", "jobs/synthetic.py"),
    ("identity-not-export-acquisition", "identity/adapters/synthetic.py",
     "import shelfhold.acquisition.application\n", None),
]  # fmt: skip


@pytest.mark.req(
    "ENF-QUAL-002",
    "ENF-SEC-014",
    "ENF-SEC-002",
    "ENF-SEC-015",
    "ENF-I18N-005",
    "ENF-SEC-001",
    "ENF-SEC-003",
)
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
    assert f"{_contract_name(contract)} BROKEN" in " ".join(result.stdout.split()), result.stdout
    if sanctioned:
        ok = _run_in_copy(tmp_path / "ok", {sanctioned: code})
        assert f"{_contract_name(contract)} BROKEN" not in " ".join(ok.stdout.split()), ok.stdout
        assert ok.returncode == 0, ok.stdout


@pytest.mark.req("ENF-QUAL-002", "EF-SRC-033")
def test_native_adapter_may_import_the_sdk(tmp_path: Path) -> None:
    code = "from shelfhold.sdk import models\n"
    result = _run_in_copy(tmp_path, {"plugin_host/adapters/native/demo/client.py": code})
    assert result.returncode == 0, result.stdout


@pytest.mark.req("ENF-QUAL-002")
def test_contracts_on_later_modules_name_only_modules_that_exist() -> None:
    """import-linter fails on an absent module: the guarded packages exist, empty and documented."""
    for rel in ("infrastructure/packaging/epub", "plugin_host/adapters/native"):
        init = SRC / "shelfhold" / rel / "__init__.py"
        assert init.is_file(), rel
        assert ast.get_docstring(ast.parse(init.read_text("utf-8"))), f"{rel}: undocumented"


@pytest.mark.req("ENF-QUAL-002")
def test_every_contract_is_an_error_contract() -> None:
    parser = _config()
    quiet = [
        s
        for s in parser.sections()
        if s.startswith("importlinter:contract:")
        and parser[s].get("unmatched_ignore_imports_alerting", "error") != "error"
    ]
    assert quiet == ["importlinter:contract:critical-comicbox"]


@pytest.mark.req("ENF-QUAL-002")
def test_no_partial_names_a_closed_milestone() -> None:
    import importlib.util

    tool = BACKEND.parent / "tools" / "traceability.py"
    spec = importlib.util.spec_from_file_location("traceability_l00_partial", tool)
    assert spec is not None
    assert spec.loader is not None
    tr = importlib.util.module_from_spec(spec)
    sys.modules["traceability_l00_partial"] = tr
    spec.loader.exec_module(tr)
    closed = {"M0", "M1a"}
    stale = [p["requirement"] for p in tr.load_partial(BACKEND.parent) if p["milestone"] in closed]
    assert not stale, f"[[partial]] entries that wait for a closed milestone: {stale}"


@pytest.mark.req("ENF-QUAL-002")
def test_no_traceability_exception_is_stale() -> None:
    """An `out_of_required_ci` entry must name a test file that still carries a conditional mark."""
    import importlib.util

    tool = BACKEND.parent / "tools" / "traceability.py"
    spec = importlib.util.spec_from_file_location("traceability_l00", tool)
    assert spec is not None
    assert spec.loader is not None
    tr = importlib.util.module_from_spec(spec)
    sys.modules["traceability_l00"] = tr
    spec.loader.exec_module(tr)
    root = BACKEND.parent
    conditional = {t.path for t in tr.collect_python(root) if t.conditional}
    stale = [e["path"] for e in tr.load_out_of_required_ci(root) if e["path"] not in conditional]
    assert not stale, f"stale out_of_required_ci entries: {stale}"


@pytest.mark.req("ENF-QUAL-002")
def test_every_new_contract_is_declared_in_the_config() -> None:
    declared = {s.split(":", 2)[2] for s in _config().sections() if s.startswith("importlinter:c")}
    assert {v[0] for v in VIOLATIONS} <= declared


# --- EF-EXP-060, review of PR #30: nothing but fsops (and the /data writers) writes files ---

WRITERS_ALLOWED = (
    "shelfhold/infrastructure/fsops/",
    "shelfhold/infrastructure/migrate.py",
    "shelfhold/infrastructure/volumes.py",
    "shelfhold/infrastructure/held_files.py",  # /data/integrity/held: a repair's working copy
    "shelfhold/infrastructure/secrets/atomic.py",  # keyring and export file, owner-only
    "shelfhold/infrastructure/cover_store.py",  # /data/covers: covers the decode worker re-encoded
    "shelfhold/plugin_host/install.py",  # /data/plugins: staging, site-packages, state file
    "shelfhold/infrastructure/local_socket/server.py",  # the CLI socket (and staging dir), /data
    "shelfhold/docgen/release_assets.py",  # release tooling: two files in a directory it is given
    "shelfhold/ops/backup/store.py",  # the backup directory (EF-INT-015), outside /data and /store
    "shelfhold/ops/backup/restore.py",  # a restore's new folder, written from a descriptor on it
    "shelfhold/ops/backup/scratch.py",  # private snapshot folders of /data/tmp, lock file
    "shelfhold/ops/backup/verify.py",  # the spool of the database being checked (a scratch file)
    # the open export (EF-INT-016): the operator's own new folder, never a library root; nothing
    # is overwritten or deleted there
    "shelfhold/platform/export_tree.py",
    # the import folder (EF-SRC-012): the person's own folder, not a library root; its only write
    # is `_quarantine_and_drop` (a classed deletion emitter), after a verified storage
    "shelfhold/library/adapters/import_scan.py",
)
PATH_WRITE_METHODS = {
    "write_text", "write_bytes", "unlink", "rename", "mkdir", "rmdir", "touch",
    "system_unlink", "system_remove_tree", "_system_empty",  # review PR #43: N2 entry points
}  # fmt: skip
LIBRARY_ONLY = ("rmtree", "move", "copyfile", "copy", "copy2", "copytree")  # os.open: TID251
OS_WRITE_FUNCTIONS = (
    "replace", "rename", "renames", "unlink", "remove", "rmdir", "removedirs", "link",
    "symlink", "mkdir", "makedirs", "truncate",
)  # fmt: skip


def _opens_for_writing(node: ast.Call) -> bool:
    mode: ast.expr | None = node.args[1] if len(node.args) > 1 else None
    if isinstance(node.func, ast.Attribute):  # Path.open(mode)
        mode = node.args[0] if node.args else None
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode = keyword.value
    return (
        isinstance(mode, ast.Constant)
        and isinstance(mode.value, str)
        and any(flag in mode.value for flag in "wax+")
    )


# Modules that may call the sanctioned writer, and only through a `self._x` attribute declared with
# one of these types (a receiver *name* proves nothing: `disk = Path(x)` is a name too).
FSOPS_CALLERS = (
    "shelfhold/export/application/deletion.py",  # the empty folders after a deletion (EF-BIB-021)
    "shelfhold/export/application/intents.py",
    "shelfhold/export/application/reclaim.py",  # the trash emptied, the early purge (EF-BIB-030)
    "shelfhold/export/application/recycle_sweep.py",  # the bin retention (ENF-OPS-009)
    "shelfhold/export/application/rename_plan.py",  # the folders a rename plan makes and empties
    "shelfhold/infrastructure/instance/lease.py",
)
WRITE_NAMES = {*PATH_WRITE_METHODS, *OS_WRITE_FUNCTIONS, *LIBRARY_ONLY}
FSOPS_TYPES = {"Fsops", "DiskPort", "WriteSession"}


def _annotation_name(node: ast.expr | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _typed_attributes(tree: ast.AST) -> set[str]:
    """`self._x` attributes of a class whose declared type is a sanctioned writer: annotated
    (`self._x: Fsops = ...`) or assigned from an `__init__` parameter annotated so."""
    found: set[str] = set()
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for init in (
            n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"
        ):
            params = {a.arg: _annotation_name(a.annotation) for a in init.args.args}
            for node in ast.walk(init):
                target: ast.expr | None = None
                kind = ""
                if isinstance(node, ast.AnnAssign):
                    target, kind = node.target, _annotation_name(node.annotation)
                elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
                    target, kind = node.targets[0], params.get(node.value.id, "")
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and kind in FSOPS_TYPES
                ):
                    found.add(target.attr)
    return found


def _goes_through_fsops(func: ast.Attribute, typed: set[str]) -> bool:
    receiver = func.value
    return (
        isinstance(receiver, ast.Attribute)
        and isinstance(receiver.value, ast.Name)
        and receiver.value.id == "self"
        and receiver.attr in typed
    )


def path_write_offences(source: str, module: str = "") -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    tree = ast.parse(source)
    typed = _typed_attributes(tree) if module in FSOPS_CALLERS else set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            if _goes_through_fsops(func, typed):
                continue
            if func.attr in PATH_WRITE_METHODS:
                found.append((node.lineno, func.attr))
            elif func.attr == "replace" and len(node.args) == 1 and not node.keywords:
                found.append((node.lineno, "replace"))  # str.replace takes two arguments
            elif func.attr == "open" and _opens_for_writing(node):
                found.append((node.lineno, "open(write)"))
        elif isinstance(func, ast.Name) and func.id == "open" and _opens_for_writing(node):
            found.append((node.lineno, "open(write)"))
        elif isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) >= 2:
            target, name = node.args[:2]
            if isinstance(target, ast.Name) and target.id in {"os", "shutil", "Path", "pathlib"}:
                literal = isinstance(name, ast.Constant) and isinstance(name.value, str)
                if not literal or name.value in WRITE_NAMES:  # type: ignore[attr-defined]
                    found.append((node.lineno, "getattr"))
    seen = {line for line, _ in found}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.lineno not in seen:
            # A reference that is not a call (`kill = Path.unlink`), or `os.replace`, `shutil.move`
            library = isinstance(node.value, ast.Name) and node.value.id in {"os", "shutil"}
            if node.attr in PATH_WRITE_METHODS and not _goes_through_fsops(node, typed):
                found.append((node.lineno, node.attr))
            elif library and node.attr in {*OS_WRITE_FUNCTIONS, *LIBRARY_ONLY}:
                found.append((node.lineno, f"{node.value.id}.{node.attr}"))  # type: ignore[attr-defined]
        elif isinstance(node, ast.ImportFrom) and (node.module or "") in {"os", "shutil"}:
            if any(a.name in {*OS_WRITE_FUNCTIONS, *LIBRARY_ONLY} for a in node.names):
                found.append((node.lineno, f"from {node.module} import"))
    return found


@pytest.mark.req("EF-EXP-060", "ENF-SEC-012")
def test_only_fsops_and_the_data_writers_write_files() -> None:
    offences = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith(WRITERS_ALLOWED):
            continue
        offences += [
            f"{rel}:{line}:{what}"
            for line, what in path_write_offences(path.read_text(encoding="utf-8"), rel)
        ]
    assert offences == []


@pytest.mark.req("EF-EXP-060")
@pytest.mark.parametrize(
    "code",
    [
        "p.write_text('x')",
        "p.write_bytes(b'x')",
        "p.open('w')",
        "p.open(mode='ab')",
        "open(p, 'x')",
        "p.unlink()",
        "p.rename(q)",
        "p.replace(q)",
        "p.mkdir()",
        "p.rmdir()",
        "p.touch()",
    ],
)
def test_the_path_write_scan_catches_each_write_form(code: str) -> None:
    assert path_write_offences(code)


@pytest.mark.req("EF-EXP-060")
@pytest.mark.parametrize(
    "code", ["s.replace('a', 'b')", "open(p)", "p.open('rb')", "p.read_text()"]
)
def test_the_path_write_scan_lets_reads_and_str_replace_through(code: str) -> None:
    assert path_write_offences(code) == []


@pytest.mark.req("EF-EXP-060", "ENF-SEC-012")
def test_ruff_bans_os_file_mutations_outside_fsops() -> None:
    import tomllib

    config = tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["ruff"]
    lint = config["lint"]
    assert "TID251" in lint["select"] or "TID" in lint["select"]
    banned = lint["flake8-tidy-imports"]["banned-api"]
    assert {f"os.{name}" for name in OS_WRITE_FUNCTIONS} <= set(banned)
    ignores = lint["per-file-ignores"]
    exempt = {pattern for pattern, rules in ignores.items() if "TID251" in rules}
    assert {
        "src/shelfhold/infrastructure/fsops/**",
        "src/shelfhold/infrastructure/migrate.py",
        "src/shelfhold/infrastructure/volumes.py",
    } <= exempt


@pytest.mark.req("EF-EXP-060", "EF-USR-024")
@pytest.mark.parametrize(
    "code",
    [
        "import os\ngetattr(os, 'unl' + 'ink')(p)",
        "import shutil\ngetattr(shutil, 'rmtree')(p)",
        "from shutil import rmtree as rt\nrt(p)",
        "from os import remove\nremove(p)",
        "from pathlib import Path\nkill = Path.unlink\nkill(Path(p))",
        "import os\nos.replace(a, b)",
        "import shutil\nshutil.move(a, b)",
        "fsops.system_remove_tree('trash_retention_purge', p)",
        "fsops.system_unlink('probe_file_cleanup', p)",
    ],
)
def test_the_path_write_scan_catches_the_review_pr43_bypasses(code: str) -> None:
    assert path_write_offences(code)


# --- M1b L03: the headless facade goes through its record functions, not through SQL ---


@pytest.mark.req("EF-INT-024")
def test_headless_admin_issues_no_raw_sql() -> None:
    source = (SRC / "shelfhold" / "headless" / "admin.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    queries = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and ((isinstance(node.value, ast.Name) and node.value.id == "sa") or node.attr == "execute")
    ]
    assert queries == []
    assert "sqlalchemy" not in imported and "sqlite_insert" not in imported


@pytest.mark.req("EF-EXP-054")
def test_probe_file_cleanup_has_one_deletion_site() -> None:
    from shelfhold.identity.domain.emitters import DELETION_SITES

    sites = [s for s, name in DELETION_SITES.items() if name == "probe_file_cleanup"]
    assert not [s for s in sites if "preflight.py" in s]
    assert "shelfhold/export/application/preflight.py" not in FSOPS_CALLERS


# --- M2 L16: a file served at a path is replaced only under an `upgrade` intent (EF-EXP-016) ---

SAME_PATH_STEPS = {
    "same_path_stage", "same_path_swap", "same_path_recycle_old", "same_path_discard",
}  # fmt: skip
SAME_PATH_CALLERS = (
    "shelfhold/export/application/upgrade.py",  # records the intent before the first step
    "shelfhold/export/application/intents.py",  # the replay of an open `upgrade` intent
    "shelfhold/infrastructure/fsops/",  # the steps themselves
)


@pytest.mark.req("EF-EXP-016", "EF-EXP-041")
def test_same_path_replacement_happens_only_under_an_intent() -> None:
    offences = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel.startswith(SAME_PATH_CALLERS):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offences += [
            f"{rel}:{node.lineno}:{node.attr}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in SAME_PATH_STEPS
        ]
    assert offences == []


@pytest.mark.req("EF-EXP-016", "EF-EXP-041")
def test_upgrade_records_its_intent_before_the_first_step() -> None:
    """In `upgrade.py`, every function that takes a step has recorded and marked its intent
    first, in the order of the protocol (`record`, `mark_applying`, then the disk)."""
    source = (SRC / "shelfhold" / "export" / "application" / "upgrade.py").read_text("utf-8")
    tree = ast.parse(source)
    stepping = 0
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef):
            continue
        calls = sorted(
            (n.lineno, n.func.attr)
            for n in ast.walk(func)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        )
        names = [name for _, name in calls]
        forward = SAME_PATH_STEPS - {"same_path_discard"}
        steps = [i for i, name in enumerate(names) if name in forward]
        if not steps:
            continue
        stepping += 1
        assert "record" in names and "mark_applying" in names, func.name
        assert names.index("record") < names.index("mark_applying") < steps[0], func.name
    assert stepping >= 1


@pytest.mark.req("ENF-QUAL-002")
def test_m3_search_packages_exist_and_are_documented() -> None:
    for rel in ("catalog/application/search/__init__.py", "infrastructure/schema/search.py"):
        path = SRC / "shelfhold" / rel
        assert path.is_file(), rel
        assert ast.get_docstring(ast.parse(path.read_text("utf-8"))), f"{rel}: undocumented"


@pytest.mark.req("ENF-QUAL-002")
def test_http_contract_ignores_are_named_with_the_lot_that_settles_them() -> None:
    """Debts found on main are ignored with the lot that removes them, never silently."""
    text = CONFIG.read_text("utf-8")
    block = text.split("[importlinter:contract:http-no-infrastructure-adapter]")[1].split(
        "[importlinter"
    )[0]
    assert "M3 L56" in block
    # M3 L41 settled its debt: the activity queries sit behind `events.activity_feed`
    assert "M3 L41" not in block
    assert "interfaces.http_api.activity ->" not in block


@pytest.mark.req("ENF-GOV-006")
def test_gate_p2_is_recorded_as_a_delegated_decision_without_a_measure() -> None:
    """P2 was settled by the owner's delegation: the full M3 is kept, and the row never claims a
    concierge figure that was not measured."""
    portes = (BACKEND.parent / "docs" / "governance" / "portes.md").read_text("utf-8")
    row = [ln for ln in portes.splitlines() if ln.startswith("| P2 | ")][-1]
    assert "par délégation" in row
    assert "n'a pas été mesuré" in row
    assert "FAMILY_NAV_LITE" in row
    assert "échec" not in row.lower()


@pytest.mark.req("ENF-GOV-006", "EF-EXP-017")
def test_ef_exp_017_is_deferred_to_m4_and_gov_006_stays_partial() -> None:
    import tomllib

    path = BACKEND.parent / "tools" / "traceability-exceptions.toml"
    data = tomllib.loads(path.read_text("utf-8"))
    deferred = [d for d in data["deferred"] if d["requirement"] == "EF-EXP-017"]
    assert len(deferred) == 1
    assert "M4" in deferred[0]["reason"]
    assert "passe 2, B15" in deferred[0]["reason"]
    partial = [p for p in data["partial"] if p["requirement"] == "ENF-GOV-006"]
    assert [p["milestone"] for p in partial] == ["M3"]


@pytest.mark.req("ENF-QUAL-002")
def test_no_forbidden_contract_on_the_root_guards_a_module_inside_it() -> None:
    """A `forbidden` contract whose source is the root `shelfhold` never reports a forbidden module
    that sits under the root: it must list the top-level packages instead."""
    parser = _config()
    blind = [
        s
        for s in parser.sections()
        if s.startswith("importlinter:contract:")
        and parser[s].get("type") == "forbidden"
        and _lines(parser[s]["source_modules"]) == ["shelfhold"]
        and any(m.startswith("shelfhold.") for m in _lines(parser[s]["forbidden_modules"]))
    ]
    assert not blind, blind


@pytest.mark.req("ENF-QUAL-002")
@pytest.mark.parametrize("contract", ["replica-single-reader", "fts-sole-reader"])
def test_registry_contracts_list_every_infrastructure_module(contract: str) -> None:
    """import-linter ignores an import between two modules of one source, so `infrastructure` is
    listed module by module (schema aside); a new module must be added to the list."""
    listed = {
        m.removeprefix("shelfhold.infrastructure.")
        for m in _lines(_config()[f"importlinter:contract:{contract}"]["source_modules"])
        if m.startswith("shelfhold.infrastructure.")
    }
    actual = {
        p.stem if p.is_file() else p.name
        for p in (SRC / "shelfhold" / "infrastructure").iterdir()
        if p.name not in ("__init__.py", "__pycache__", "schema")
    }
    assert listed == actual, listed ^ actual


@pytest.mark.req("ENF-QUAL-002")
def test_search_tables_module_exposes_the_fts_schema_module() -> None:
    from shelfhold.catalog.application.search import tables
    from shelfhold.infrastructure.schema import search

    assert tables.fts_tables is search
    assert tables.__all__ == ["fts_tables"]
