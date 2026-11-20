"""L02 (M6): `shelfhold-sdk` 2.0 as its own distribution (ENF-QUAL-003 volet M6, EF-SRC-001
passe 11, D10, D11) — workspace member, kit as a pytest plugin, deprecated Kavita members
removed, `api` 2 with the `api` 1 shim.

Red first: each test below fails until the lot lands.
"""

import importlib.metadata
import json
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[3]
SDK = REPO / "sdk"

KAVITA_ONLY = (
    "parse_name_call",
    "ensure_library_call",
    "resolve_remote_calls",
    "event_subscription",
    "progress_calls",
)


@pytest.mark.req("ENF-QUAL-003", "EF-SRC-001")
def test_sdk_runtime_dependencies_closed_list() -> None:
    """The published distribution depends on pydantic alone (D10): never on the host."""
    data = tomllib.loads((SDK / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    assert project["name"] == "shelfhold-sdk"
    assert project["version"] == "2.0.0"
    assert project["license"] == "MIT"
    deps = project["dependencies"]
    assert len(deps) == 1 and deps[0].startswith("pydantic"), deps
    assert (SDK / "LICENSE").is_file()  # the MIT license ships with the distribution


@pytest.mark.req("ENF-QUAL-003")
def test_kit_registered_as_pytest_plugin() -> None:
    """`shelfhold_sdk.testing` registers as a `pytest11` entry point of `shelfhold-sdk`."""
    found = []
    for dist in importlib.metadata.distributions():
        if (dist.metadata["Name"] or "").lower().replace("_", "-") == "shelfhold-sdk":
            found += [ep for ep in dist.entry_points if ep.group == "pytest11"]
    assert found, "shelfhold-sdk declares no pytest11 entry point"
    assert any(ep.value == "shelfhold_sdk.testing.pytest_plugin" for ep in found), found
    module = found[0].load()
    for fixture in ("offline_fetcher", "check_conformance", "check_destination"):
        assert hasattr(module, fixture), fixture  # the fixtures a plugin author gets


@pytest.mark.req("EF-SRC-001")
def test_sdk_2_has_no_deprecated_kavita_member() -> None:
    """SDK 2.0 removes the Kavita-only members deprecated since 1.1 (ADR-0024, SDK 2.0)."""
    import shelfhold_sdk.export_target as export_target
    from shelfhold_sdk.models import API_VERSION, SDK_VERSION

    assert API_VERSION == 2
    assert SDK_VERSION == "2.0"
    for name in KAVITA_ONLY:
        assert not hasattr(export_target.ExportTarget, name), name
    assert not hasattr(export_target, "KAVITA_ONLY")


@pytest.mark.req("EF-SRC-001")
def test_api_1_manifest_still_loads_through_shim() -> None:
    """D11: `api` 2 loads natively, `api` 1 through the shim (12 months, until 2027-11)."""
    from shelfhold_sdk.models import API_VERSION
    from shelfhold_sdk.testing.fakes import FakeRangedConnectedService

    from shelfhold.plugin_host.registry import (
        PluginCandidate,
        PluginOrigin,
        PluginRegistry,
        PluginStatus,
        shim_api_1,
    )

    assert API_VERSION == 2
    raw = json.loads(FakeRangedConnectedService().manifest.model_dump_json())
    raw["api"] = 1
    raw["key"] = "shimmed"
    migrated = shim_api_1(raw)
    assert migrated["api"] == 2

    candidate = PluginCandidate(
        key="shimmed",
        origin=PluginOrigin.FIRST_PARTY,
        load=lambda: SimpleNamespace(manifest=raw),
    )
    registry = PluginRegistry()  # default api_version is the SDK's API_VERSION
    (record,) = registry.load_all([candidate])
    assert record.status is PluginStatus.LOADED_SHIM
    assert record.manifest is not None
    assert record.manifest.api == 2
    assert record.manifest.key == "shimmed"


@pytest.mark.req("ENF-QUAL-003", "EF-SRC-001")
def test_sdk_wheel_installs_without_host_and_runs_kit(tmp_path: Path) -> None:
    """The wheel carries no host package, depends on pydantic only, and the kit runs from it
    in a fresh interpreter that never had the host."""
    out = tmp_path / "dist"
    build = subprocess.run(  # noqa: S603
        ["uv", "build", "--project", str(SDK), "--wheel", "--out-dir", str(out)],  # noqa: S607
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    wheels = sorted(out.glob("shelfhold_sdk-2.0.0-*.whl"))
    assert len(wheels) == 1, list(out.iterdir())
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        tops = {name.split("/")[0] for name in names}
        assert "shelfhold_sdk" in tops
        assert "shelfhold" not in tops, "the wheel must not ship the host package"
        assert "shelfhold_sdk/py.typed" in names, "the wheel must ship the PEP 561 marker"
        metadata_name = next(n for n in names if n.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_name).decode("utf-8")
    requires = [
        line.split(":", 1)[1].strip()
        for line in metadata.splitlines()
        if line.startswith("Requires-Dist:")
    ]
    assert len(requires) == 1 and requires[0].startswith("pydantic"), requires

    venv = tmp_path / "venv"
    subprocess.run(  # noqa: S603
        [sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True
    )
    pip = str(venv / "bin" / "pip")
    subprocess.run(  # noqa: S603
        [pip, "install", str(wheels[0]), "pytest"],
        check=True,
        capture_output=True,
    )

    probe = tmp_path / "probe.py"
    probe.write_text(
        "import asyncio, importlib.util\n"
        "assert importlib.util.find_spec('shelfhold') is None, 'the host must not be installed'\n"
        "from shelfhold_sdk.testing import FakeRangedConnectedService, fake_ranged_fetcher\n"
        "from shelfhold_sdk.testing.kit import run_source_conformance\n"
        "service = FakeRangedConnectedService()\n"
        "report = asyncio.run(\n"
        "    run_source_conformance(service, fake_ranged_fetcher(),\n"
        "                             reinstall=lambda: FakeRangedConnectedService())\n"
        ")\n"
        "assert report.ok, report.failures\n"
        "print('KIT-OK')\n",
        encoding="utf-8",
    )
    run = subprocess.run(  # noqa: S603
        [str(venv / "bin" / "python"), str(probe)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0 and "KIT-OK" in run.stdout, run.stdout + run.stderr

    plugin_test = tmp_path / "test_kit_plugin.py"
    plugin_test.write_text(
        "def test_the_kit_fixtures_are_injected(offline_fetcher, check_conformance):\n"
        "    assert offline_fetcher is not None and callable(check_conformance)\n",
        encoding="utf-8",
    )
    pytest_run = subprocess.run(  # noqa: S603
        [str(venv / "bin" / "python"), "-m", "pytest", "-q", str(plugin_test)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert pytest_run.returncode == 0, pytest_run.stdout + pytest_run.stderr


# -- the host side of the contract ---------------------------------------------------------------


@pytest.mark.req("EF-SRC-001")
def test_host_depends_on_the_sdk_of_the_same_repo_at_the_exact_version() -> None:
    """D10: the host depends on `shelfhold-sdk` of this repository, exact version pinned."""
    backend = tomllib.loads((REPO / "backend" / "pyproject.toml").read_text(encoding="utf-8"))
    deps = backend["project"]["dependencies"]
    sdk_deps = [d for d in deps if d.split(">=")[0].split("=")[0].strip() == "shelfhold-sdk"]
    assert sdk_deps == ["shelfhold-sdk==2.0.0"], deps
    sources = backend.get("tool", {}).get("uv", {}).get("sources", {})
    assert sources.get("shelfhold-sdk", {}).get("workspace") is True, sources
    root = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert "sdk" in root["tool"]["uv"]["workspace"]["members"]


@pytest.mark.req("EF-SRC-001")
def test_reserved_names_refuse_a_plugin_that_would_shadow_the_sdk() -> None:
    """A third-party wheel may not ship a top-level `shelfhold_sdk` module."""
    from shelfhold.plugin_host.install import RESERVED_DISTRIBUTIONS, RESERVED_MODULES

    assert "shelfhold-sdk" in RESERVED_DISTRIBUTIONS
    assert "shelfhold_sdk" in RESERVED_MODULES


@pytest.mark.req("EF-SRC-001")
def test_the_1_x_series_still_imports_the_sdk_under_its_old_name() -> None:
    """D10: `shelfhold.sdk` re-exports `shelfhold_sdk` during the whole 1.x series of the app."""
    import shelfhold_sdk

    import shelfhold.sdk

    assert shelfhold.sdk.models is shelfhold_sdk.models
    from shelfhold.sdk import models as old_models

    assert old_models is shelfhold_sdk.models
    import shelfhold.sdk.export_target as old_export_target
    import shelfhold.sdk.testing.kit as old_kit

    assert old_export_target is shelfhold_sdk.export_target
    assert old_kit is shelfhold_sdk.testing.kit
