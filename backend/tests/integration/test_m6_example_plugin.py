"""M6 L10: the example third-party plugin, end to end.

The plugin of `docs/plugins/exemple` is built into a wheel, published on a signed index (a key made
in the test), installed by the real CLI from that index, enabled by the reserved action and run
isolated, as the core runs it. Nothing here is a mock of the feature under test: the wheel, the
index, the install, the grant, the runner socket, the plugin's process and the event bus are the
real ones; only the network (a scripted fetcher) and the companion's sandbox observation are played.

The second half is the core's start-up: only the plugins whose isolated activation is granted (and
still holds) are started, through `connect_runner` then `load_isolated`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import tomllib
import zipfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from shelfhold_sdk.fetcher import Response
from shelfhold_sdk.testing import FakeFetcher as ScriptedFetcher

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import migrate
from shelfhold.main import create_app
from shelfhold.plugin_host import install as guard
from shelfhold.plugin_host.adapters.rpc.proxy import RpcPluginProxy
from shelfhold.plugin_host.adapters.rpc.runner_socket import RunnerUnavailable, connect_runner
from shelfhold.plugin_host.adapters.rpc.stdio import closed_environment, spawn_stdio
from shelfhold.plugin_host.registry import PluginOrigin, PluginRecord, PluginRegistry, PluginStatus
from shelfhold.plugin_host.rpc.host_side import RpcSession
from shelfhold.plugin_host.rpc.protocol import Channel, RpcLimits
from shelfhold.plugin_host.runner.selfcheck import Observation
from shelfhold.plugin_host.runner.server import RunnerConfig, RunnerServer
from tests.auth_env import db_rows, run_cli
from tests.integration.test_l09a_plugin_lifecycle import _expires
from tests.plugin_guard_env import CANARY, PHRASE, Wheel, build_wheel, manifest
from tests.plugin_index_env import (
    FakeFetcher,
    IndexKey,
    index_document,
    make_key,
    plugin_entry,
    sign_index,
    trusted_comment,
)

EXAMPLE = Path(__file__).resolve().parents[3] / "docs" / "plugins" / "exemple"
CASSETTE = EXAMPLE / "tests" / "cassettes" / "catalogue.json"
CATALOG_URL = "https://catalogue.exemple.invalid/catalogue.json"
INDEX_URL = "https://index.example.org/index.json"
WHEEL_BASE = "https://index.example.org/wheels/"
CORE_UID, RUNNER_UID = 1000, 1001
CLEAN = Observation(
    uid=RUNNER_UID,
    visible_paths=(),
    interfaces=("lo",),
    outbound_possible=False,
    root_read_only=True,
)


# -- the plugin, as its author ships it ---------------------------------------------------------


def _project() -> dict[str, Any]:
    project: dict[str, Any] = tomllib.loads(
        (EXAMPLE / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    return project


def _entry_of_project() -> tuple[str, str]:
    """The `shelfhold.plugins` entry point the plugin's own `pyproject.toml` declares: what a build
    backend would write into the wheel. A plugin that declares none cannot be started by anyone."""
    groups = _project().get("entry-points", {})
    declared = groups.get("shelfhold.plugins", {})
    assert len(declared) == 1, (
        "the example plugin's pyproject.toml declares no `shelfhold.plugins` entry point: "
        f"{declared!r}"
    )
    ((key, target),) = declared.items()
    return str(key), str(target)


def build_example_wheel(directory: Path) -> Wheel:
    """The wheel of `docs/plugins/exemple`: its own sources, its own name, version and entry
    point, zipped offline (no build backend, no network, no index)."""
    project = _project()
    key, target = _entry_of_project()
    modules = {
        f"plugin_exemple/{path.name}": path.read_text(encoding="utf-8")
        for path in sorted((EXAMPLE / "src" / "plugin_exemple").glob("*.py"))
    }
    assert "plugin_exemple/source.py" in modules
    return build_wheel(
        directory,
        str(project["name"]),
        modules,
        entry=(key, target),
        version=str(project["version"]),
    )


def _serve_index(fetcher: FakeFetcher, signer: IndexKey, wheel: Wheel, key: str) -> None:
    """A signed index that vouches for the wheel, and the wheel itself."""
    wheel_url = WHEEL_BASE + wheel.path.name
    fetcher.wheels[wheel_url] = wheel.path.read_bytes()
    expires = _expires()
    document = index_document(
        version=1,
        expires=expires,
        plugins=[
            plugin_entry(
                key=key,
                version=str(_project()["version"]),
                sha256="sha256:" + wheel.sha256,
                wheel_url=wheel_url,
            )
        ],
    )
    fetcher.serving(
        INDEX_URL, document, sign_index(document, signer, comment=trusted_comment(1, expires))
    )


def install_example_from_index(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> tuple[Wheel, FakeFetcher]:
    """The real CLI: pin the index, install the plugin from it, enable it (the reserved action)."""
    migrate(settings)
    signer = make_key()  # made here, never committed
    key, _ = _entry_of_project()
    wheel = build_example_wheel(tmp_path)
    index = FakeFetcher()
    _serve_index(index, signer, wheel, key)
    monkeypatch.setattr("shelfhold.interfaces.cli.plugin.index_fetcher", lambda _network: index)
    assert (
        run_cli(
            monkeypatch,
            settings,
            "plugin",
            "index",
            "add",
            INDEX_URL,
            "--pubkey",
            signer.public_b64,
        )
        == 0
    )
    assert run_cli(monkeypatch, settings, "plugin", "install", key, "--index", INDEX_URL) == 0
    dist = str(_project()["name"])
    assert run_cli(monkeypatch, settings, "plugin", "enable", dist) == 0
    return wheel, index


# -- the companion and the core's end of its socket ----------------------------------------------


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A short path: a unix socket's address is at most 107 bytes, which pytest's paths exceed."""
    path = Path(tempfile.mkdtemp(prefix="l10", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@contextlib.asynccontextmanager
async def running_companion(short_dir: Path, plugins_root: Path) -> AsyncIterator[RunnerServer]:
    """The companion, in this process, with real subprocesses for the plugins; its sandbox is the
    observation a test makes up (the compose of the e2e stack plays the real one)."""
    server = RunnerServer(
        RunnerConfig(
            socket_path=short_dir / "runner.sock", plugins_root=plugins_root, core_uid=CORE_UID
        ),
        own_uid=RUNNER_UID,
        peer_uid=lambda _sock: CORE_UID,
        observe=lambda: CLEAN,
    )
    await server.start()
    try:
        yield server
    finally:
        await server.close()


def catalogue(body: bytes) -> ScriptedFetcher:
    return ScriptedFetcher({("GET", CATALOG_URL): Response(status=200, headers={}, body=body)})


# -- the example plugin ------------------------------------------------------------------------


@pytest.mark.req("EF-SRC-019", "EF-SRC-020", "ENF-SEC-009")
def test_example_plugin_installs_from_signed_index_and_runs_isolated(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path, short_dir: Path
) -> None:
    """The wheel built from the plugin's sources is installed from a signed index by the real CLI,
    enabled by the reserved action and run by the companion in its own process; what it answers
    comes from its own source (the synthetic catalogue), through the host's Fetcher."""
    wheel, index = install_example_from_index(monkeypatch, settings, tmp_path)
    key, _ = _entry_of_project()
    assert index.calls[0] == INDEX_URL + ".minisig" or INDEX_URL in index.calls
    assert any(call.endswith(wheel.path.name) for call in index.calls)  # fetched from the index

    # the install is recorded as the index's, pinned, and the grant is the isolated one
    row = db_rows(settings, "SELECT version, pinned FROM plugin_install WHERE plugin_id = ?", key)
    assert row and row[0][0] == str(_project()["version"]), row
    layout = guard.Layout.of(settings.data_dir)
    (verdict,) = guard.verdicts(layout, {})
    assert verdict.granted and verdict.mode == guard.MODE_ISOLATED, verdict
    assert (layout.site / "plugin_exemple" / "source.py").is_file()

    host_fetcher = ScriptedFetcher.from_cassette(CASSETTE)
    limits = RpcLimits()

    async def scenario() -> tuple[PluginRecord, list[str]]:
        async with running_companion(short_dir, layout.root) as server:
            registry = PluginRegistry()

            async def connect() -> RpcSession:
                channel = await connect_runner(
                    server.config.socket_path,
                    key=key,
                    entry=_entry_of_project()[1],
                    limits=limits,
                    core_uid=CORE_UID,
                )
                return await RpcSession.open(channel, key=key, limits=limits)

            record = await registry.load_isolated(key, PluginOrigin.THIRD_PARTY, connect)
            titles: list[str] = []
            if record.status is PluginStatus.LOADED:
                page = await record.instance.search(host_fetcher, "libre", {}, None)
                titles = [item.title for item in page.items]
            if record.instance is not None:
                await record.instance.aclose()
            return record, titles

    record, titles = asyncio.run(asyncio.wait_for(scenario(), 90))
    assert record.status is PluginStatus.LOADED, (record.status, record.reason)
    assert isinstance(record.instance, RpcPluginProxy)  # the host holds a proxy, never the code
    assert record.origin is PluginOrigin.THIRD_PARTY
    assert titles == ["Un livre libre"]  # the title of the plugin's own catalogue
    assert host_fetcher.unscripted == []
    assert [url for (_method, url, _h) in host_fetcher.seen] == [CATALOG_URL]  # via the host


# -- the conformance kit ------------------------------------------------------------------------

KIT_SCRIPT = r"""
import asyncio, importlib.metadata as metadata, importlib.util, json, os, sys
from pathlib import Path

mode, site, cassette, core_uid = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
sys.path.insert(0, site)
host_importable = importlib.util.find_spec("shelfhold") is not None
from shelfhold_sdk.testing import FakeFetcher, observe_sandbox, run_source_conformance

(entry,) = [
    e for d in metadata.distributions(path=[site]) for e in d.entry_points
    if e.group == "shelfhold.plugins"
]
factory = entry.load()
import plugin_exemple
source = factory()
fetcher = FakeFetcher.from_cassette(Path(cassette))
isolated = mode == "isolated"
report = asyncio.run(run_source_conformance(
    source, fetcher, reinstall=factory, isolated=isolated,
    sandbox=observe_sandbox() if isolated else None,
    core_uid=core_uid if isolated else None,
))
print(json.dumps({
    "host_importable": host_importable,
    "module_file": plugin_exemple.__file__,
    "uid": os.getuid(),
    "ok": report.ok,
    "checks": [c.name for c in report.checks],
    "failures": [f"{c.name}: {c.detail}" for c in report.failures],
    "unscripted": fetcher.unscripted,
}))
"""


def extract_wheel(wheel: Wheel, destination: Path) -> Path:
    """`pip install --target`, for a pure wheel with nothing to resolve: the files, as they are."""
    destination.mkdir(parents=True)
    with zipfile.ZipFile(wheel.path) as archive:
        archive.extractall(destination)
    return destination


def run_kit(
    mode: str,
    site: Path,
    tmp_path: Path,
    *,
    python_flags: tuple[str, ...],
    env: dict[str, str],
) -> dict[str, Any]:
    script = tmp_path / "kit_script.py"
    script.write_text(KIT_SCRIPT, encoding="utf-8")
    done = subprocess.run(  # noqa: S603 - a child interpreter over a wheel this test built
        [sys.executable, *python_flags, str(script), mode, str(site), str(CASSETTE),
         str(os.getuid() + 1)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        cwd=tmp_path,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    report: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
    return report


@pytest.mark.req("ENF-QUAL-003", "EF-USR-024")
def test_conformance_kit_passes_in_process_and_isolated(tmp_path: Path) -> None:
    """The kit passes on the plugin as its wheel installs it, twice: in process, and in a process
    that has what an isolated plugin has (a closed environment, nothing of Shelfhold's in it, no
    `/data`, a uid other than the core's). The isolated run observes the process it runs in: the
    SDK defines the isolated mode of the kit as exactly that (`observe_sandbox`)."""
    site = extract_wheel(build_example_wheel(tmp_path), tmp_path / "site")
    env = closed_environment({**os.environ, "SHELFHOLD_API_TOKEN": CANARY})
    assert not [name for name in env if name.startswith("SHELFHOLD_")]

    in_process = run_kit("in_process", site, tmp_path, python_flags=("-I",), env=env)
    assert in_process["ok"], in_process["failures"]
    assert in_process["unscripted"] == []
    assert not any(name.startswith("isolation_") for name in in_process["checks"])  # not played

    isolated = run_kit("isolated", site, tmp_path, python_flags=("-I",), env=env)
    assert isolated["ok"], isolated["failures"]
    played = {name for name in isolated["checks"] if name.startswith("isolation_")}
    assert {"isolation_no_data", "isolation_no_cli_socket", "isolation_uid_distinct"} <= played
    assert isolated["module_file"].startswith(str(site))  # the installed wheel, not the source tree


@pytest.mark.req("ENF-QUAL-003")
def test_conformance_kit_runs_from_published_wheel_without_host(tmp_path: Path) -> None:
    """A plugin author has the wheel and the SDK, never the host: the wheel is unpacked in a folder
    of its own and the kit runs in an interpreter where `shelfhold` cannot be imported at all."""
    import shelfhold_sdk

    site = extract_wheel(build_example_wheel(tmp_path), tmp_path / "site")
    # what an author's environment holds: the dependencies, the SDK, the plugin; never the host
    stage = tmp_path / "author"
    stage.mkdir()
    purelib = Path(sysconfig.get_path("purelib"))
    for item in purelib.iterdir():
        if (
            item.name.startswith(("shelfhold", "__editable__", "_shelfhold"))
            or item.suffix == ".pth"
        ):
            continue
        (stage / item.name).symlink_to(item)
    shutil.copytree(
        Path(shelfhold_sdk.__file__).parent,
        stage / "shelfhold_sdk",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    report = _run_without_host(site, stage, tmp_path, closed_environment())
    assert report["host_importable"] is False
    assert report["ok"], report["failures"]
    assert report["module_file"].startswith(str(site))
    assert report["unscripted"] == []


def _run_without_host(
    site: Path, stage: Path, tmp_path: Path, env: dict[str, str]
) -> dict[str, Any]:
    """The kit's script, in `python -S -I`: `sys.path` is the plugin's folder and the author's."""
    script = tmp_path / "kit_without_host.py"
    script.write_text(
        f"import sys\nsys.path[:0] = [{str(stage)!r}]\n" + KIT_SCRIPT, encoding="utf-8"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-S", "-I", str(script), "in_process", str(site), str(CASSETTE), "0"],
        env=env, capture_output=True, text=True, check=False, timeout=120, cwd=tmp_path,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    report: dict[str, Any] = json.loads(done.stdout.strip().splitlines()[-1])
    return report


# -- the namespaced event and the waiting state -----------------------------------------------


def _start_plugins_in(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> dict[str, list[Any]]:
    """What the core's start-up does, observed: every `connect_runner` and `load_isolated` call. The
    companion is replaced by the plugin's own subprocess (`spawn_stdio`, the way the companion
    starts it), so the plugin really runs; the guard and the registry are the real ones."""
    seen: dict[str, list[Any]] = {"connect": [], "load": [], "records": [], "events": []}
    site = guard.Layout.of(settings.data_dir).site

    async def spawn(_socket: Any, *, key: str, entry: str, limits: RpcLimits, **_: Any) -> Channel:
        seen["connect"].append((key, entry))
        argv = [sys.executable, "-I", "-m", "shelfhold.plugin_host.rpc.plugin_side",
                "--path", str(site), "--entry", entry]  # fmt: skip
        return await spawn_stdio(argv, limits=limits)

    original = PluginRegistry.load_isolated

    async def spy(self: PluginRegistry, key: str, origin: PluginOrigin, connect: Any) -> Any:
        seen["load"].append(key)
        record = await original(self, key, origin, connect)
        seen["records"].append(record)
        return record

    import shelfhold.main as main_module

    real_publish = main_module.publish_plugin_event  # type: ignore[attr-defined]

    def publish(bus: Any, connection: Any, key: str, name: str, payload: Any = None) -> Any:
        seen["events"].append((key, name))
        return real_publish(bus, connection, key, name, payload)

    monkeypatch.setattr(main_module, "connect_runner", spawn)
    monkeypatch.setattr(PluginRegistry, "load_isolated", spy)
    monkeypatch.setattr(main_module, "publish_plugin_event", publish)
    return seen


@pytest.mark.req("ENF-OBS-003", "EF-SRC-020", "ENF-QUAL-003")
@pytest.mark.timing
def test_example_plugin_declares_namespaced_event_and_waiting_exit(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """The plugin declares a waiting state with its way out, and when it is in that state it says
    so with an event of its own space (`plugin.<key>.<state>`): the event travels from the plugin's
    process, through the session, to `publish_plugin_event` of the registry (the only door of a
    plugin's event to the bus), and lands on the bus. An empty catalogue is the stale state."""
    from shelfhold_sdk.events import plugin_event_type

    install_example_from_index(monkeypatch, settings, tmp_path)
    key, _ = _entry_of_project()
    seen = _start_plugins_in(monkeypatch, settings)

    async def scenario() -> PluginRecord:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            (record,) = seen["records"]
            assert record.status is PluginStatus.LOADED, (record.status, record.reason)
            await record.instance.search(catalogue(b'{"books": []}'), "", {}, None)
            return record  # type: ignore[no-any-return]

    record = asyncio.run(asyncio.wait_for(scenario(), 120))
    manifest_ = record.manifest
    assert manifest_ is not None and manifest_.waiting_states
    state = manifest_.waiting_states[0]
    assert state.exit in ("retry", "recheck", "reconfigure")
    event_type = plugin_event_type(key, state.name)
    assert event_type == f"plugin.{key}.{state.name}"
    # reached from the plugin, by the registry's door, with the host-built name
    assert seen["events"] == [(key, state.name)], seen["events"]
    stored = db_rows(settings, "SELECT type FROM event WHERE type LIKE 'plugin.%'")
    assert [row[0] for row in stored] == [event_type]


# -- the core's start-up ------------------------------------------------------------------------


def plugin_wheel(
    directory: Path, dist: str, key: str, *, body: str = "", factory: bool = False
) -> Wheel:
    """A small plugin: only its manifest, enough for the guard and the registry."""
    source = f"manifest = {manifest(key)!r}\n\nclass Plugin:\n    manifest = manifest\n\n"
    source += "plugin = Plugin\n" if factory else "plugin = Plugin()\n"
    module = dist.replace("-", "_")
    return build_wheel(
        directory,
        dist,
        {f"{module}/__init__.py": source + body},
        entry=(key, f"{module}:plugin"),
    )


def install_plugin(monkeypatch: pytest.MonkeyPatch, settings: Settings, wheel: Wheel) -> None:
    code = run_cli(
        monkeypatch, settings, "plugin", "install", str(wheel.path), "--sha256", wheel.sha256
    )
    assert code == 0


def start_core(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        assert client.get("/").status_code in (200, 404)  # up, whatever it answers here


@pytest.fixture
def seen_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """`connect_runner` and `load_isolated` as spies: the companion is refused every time (the
    start-up must cope), and each call is counted with the key it was made for."""
    seen: dict[str, list[Any]] = {"connect": [], "load": [], "records": []}
    original = PluginRegistry.load_isolated

    async def refusing(_socket: Any, *, key: str, entry: str, **_: Any) -> Channel:
        seen["connect"].append(key)
        raise RunnerUnavailable("the plugin runner is not reachable: FileNotFoundError")

    async def spy(self: PluginRegistry, key: str, origin: PluginOrigin, connect: Any) -> Any:
        seen["load"].append(key)
        record = await original(self, key, origin, connect)
        seen["records"].append(record)
        return record

    monkeypatch.setattr("shelfhold.main.connect_runner", refusing)
    monkeypatch.setattr(PluginRegistry, "load_isolated", spy)
    return seen


@pytest.mark.req("EF-SRC-019", "ENF-SEC-009")
def test_core_starts_only_granted_isolated_plugins(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    seen_calls: dict[str, list[Any]],
) -> None:
    """Four plugins are installed: one isolated and granted, one isolated and never enabled, one in
    process with every one of its conditions met, and one isolated whose files changed after the
    enable. At start-up the core asks the companion for the first one only."""
    migrate(settings)
    for dist in ("granted", "denied", "inproc", "tampered"):
        install_plugin(monkeypatch, settings, plugin_wheel(tmp_path, dist, dist))
    assert run_cli(monkeypatch, settings, "plugin", "enable", "granted") == 0
    assert run_cli(monkeypatch, settings, "plugin", "enable", "tampered") == 0
    monkeypatch.setenv(guard.ENV_ALLOW, "1")
    assert run_cli(
        monkeypatch, settings, "plugin", "enable", "inproc", "--in-process", stdin=PHRASE + "\n"
    ) == 0  # fmt: skip
    module = guard.Layout.of(settings.data_dir).site / "tampered" / "__init__.py"
    module.write_text(module.read_text(encoding="utf-8") + "# changed after the enable\n")

    judged = {v.name: (v.granted, v.mode) for v in guard.verdicts(
        guard.Layout.of(settings.data_dir), os.environ
    )}  # fmt: skip
    assert judged == {  # the premises: what the guard says of each plugin
        "granted": (True, guard.MODE_ISOLATED),
        "denied": (False, guard.MODE_ISOLATED),
        "inproc": (True, guard.MODE_IN_PROCESS),
        "tampered": (False, guard.MODE_ISOLATED),
    }

    start_core(settings)
    assert seen_calls["connect"] == ["granted"], seen_calls["connect"]
    assert seen_calls["load"] == ["granted"], seen_calls["load"]


@pytest.mark.req("EF-SRC-019", "ENF-SEC-009")
def test_core_starts_the_granted_distribution_not_a_namesake_entry_point(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    seen_calls: dict[str, list[Any]],
) -> None:
    """The grant is the distribution's. A distribution that is not enabled must not start because
    its entry point carries the name of the one that is: `acme-plug` (key `acme`) is granted,
    `evil-plug` is not and names its entry point `acme-plug`."""
    migrate(settings)
    install_plugin(monkeypatch, settings, plugin_wheel(tmp_path, "acme-plug", "acme"))
    install_plugin(monkeypatch, settings, plugin_wheel(tmp_path, "evil-plug", "acme-plug"))
    assert run_cli(monkeypatch, settings, "plugin", "enable", "acme-plug") == 0
    start_core(settings)
    assert seen_calls["connect"] == ["acme"], seen_calls["connect"]
    assert seen_calls["load"] == ["acme"], seen_calls["load"]


@pytest.mark.req("EF-SRC-019", "EF-SRC-016")
def test_core_starts_when_the_companion_is_unavailable_and_marks_the_plugin_in_error(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    seen_calls: dict[str, list[Any]],
) -> None:
    """No companion (`RunnerUnavailable`): the core still starts, answers, and reports the plugin
    in error with the reason; it does not crash and does not retry in a loop."""
    migrate(settings)
    install_plugin(monkeypatch, settings, plugin_wheel(tmp_path, "granted", "granted"))
    assert run_cli(monkeypatch, settings, "plugin", "enable", "granted") == 0
    start_core(settings)
    assert seen_calls["connect"] == ["granted"]  # asked once
    (record,) = seen_calls["records"]
    assert record.status is PluginStatus.ERROR, (record.status, record.reason)
    assert record.reason is not None and "not reachable" in record.reason
