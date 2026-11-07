"""L55 (ENF-OPS-021, ENF-SEC-009): nothing is loaded from outside."""

import ast
from pathlib import Path

import pytest

from shelfhold.catalog.public import default_registry
from shelfhold.library.tombstones import CITABLE_NORMALIZERS, missing_citable
from tests.tools_loader import REPO, load_tool

pytestmark = pytest.mark.req("ENF-OPS-021")

SOURCE = Path(__file__).resolve().parents[2] / "src" / "shelfhold"


def test_production_registry_declares_no_number_version_beyond_the_stored_one() -> None:
    """The spec defines no `number@2`: the code ships `number@1` only, which is what ingestion
    writes, so `normalize migrate` has nothing to propose."""
    registry = default_registry()
    assert str(registry.current("number")) == "number@1"
    assert not any(n.endswith("@2") for n in CITABLE_NORMALIZERS)
    assert missing_citable(registry) == []


_EXEMPT = {"plugin_host/registry.py", "interfaces/cli/registry.py"}  # CLI: own package
_GROUPS = {"shelfhold.normalizers", "shelfhold.plugins"}
_DYNAMIC = {
    "entry_points",
    "import_module",
    "__import__",
    "iter_modules",
    "spec_from_file_location",
}


def _dynamic_loads(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and node.value in _GROUPS:
            found.append(f"{path.name}: group {node.value!r}")
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name in _DYNAMIC:
                found.append(f"{path.name}: {name}")
    return found


@pytest.mark.req("ENF-SEC-009")
def test_no_production_module_loads_code_from_entry_points_or_by_name() -> None:
    """A distribution on `sys.path` must never run code at startup (ENF-SEC-009): no
    `shelfhold.*` entry-point group is read, and nothing is imported from a computed name."""
    offenders = [
        hit
        for path in sorted(SOURCE.rglob("*.py"))
        if "sdk/testing" not in path.as_posix()
        and path.relative_to(SOURCE).as_posix() not in _EXEMPT
        for hit in _dynamic_loads(path)
    ]
    assert offenders == []


@pytest.mark.req("ENF-SEC-009")
def test_the_exempt_discovery_module_is_reached_only_through_the_start_up_guard() -> None:
    """`plugin_host/registry.py` reads the `shelfhold.plugins` group, from the third-party
    directory alone. Since M1b L07 it is live, and one module starts it: `plugin_host.public`
    (`start_plugins`, called by the server start-up and `plugin list --load`). No other
    production module may name the loader or the registry's discovery functions."""
    tool = load_tool("reachability")
    live = tool.reachable(tool.build_graph(REPO / "backend" / "src"))
    assert any(s.startswith("shelfhold.plugin_host.registry") for s in live)
    callers = {
        path.relative_to(SOURCE).as_posix()
        for path in SOURCE.rglob("*.py")
        if any(
            name in path.read_text(encoding="utf-8")
            for name in ("third_party_candidates", "_third_party_loader")
        )
    }
    assert callers == {"plugin_host/registry.py", "plugin_host/public.py"}


@pytest.mark.req("ENF-SEC-009")
def test_the_loader_check_flags_an_entry_point_group(tmp_path: Path) -> None:
    bad = tmp_path / "loader.py"
    bad.write_text(
        "from importlib.metadata import entry_points\n"
        "for ep in entry_points(group='shelfhold.plugins'):\n    ep.load()\n"
    )
    assert sorted(_dynamic_loads(bad)) == [
        "loader.py: entry_points",
        "loader.py: group 'shelfhold.plugins'",
    ]


@pytest.mark.req("ENF-SEC-009")
def test_composition_builds_only_native_services() -> None:
    """The one connected service the composition root builds is the first-party Suwayomi
    adapter, imported by name from `shelfhold`; no third-party module is imported there."""
    tree = ast.parse((SOURCE / "composition.py").read_text(encoding="utf-8"))
    services = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id.endswith("Service")
    }
    imported = {
        alias.name: node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    for name in services:
        assert (imported.get(name) or "").startswith("shelfhold."), name
    assert "SuwayomiService" in services
    assert _dynamic_loads(SOURCE / "composition.py") == []
