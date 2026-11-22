"""M6 L06 (EF-SRC-019, ENF-SEC-009, EF-EXP-065, D13): the guard under the spec names, both modes.

A third-party plugin installs **disabled** and its default mode is **isolated**; enabling it is a
**reserved action** (a fresh admin session or the host CLI); the in-process mode keeps
``SHELFHOLD_ALLOW_UNSANDBOXED_PLUGINS=1`` and the typed confirmation of M1b; a plugin never
chooses its mode. The UI and the CLI say why a plugin is not loaded; the attestation of
EF-EXP-065 applies as-is to an isolated plugin (the EPUB stays neutralized until the word is
given); the events of an isolated plugin stay in ``plugin.<key>.*``.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from shelfhold_sdk.models import API_VERSION
from shelfhold_sdk.testing.fakes import FakeRangedConnectedService

from shelfhold.config import Settings
from shelfhold.library.adapters.epub_derivative import trust_of
from shelfhold.plugin_host import install as guard
from shelfhold.plugin_host.attestation import publisher_attested
from shelfhold.plugin_host.registry import (
    PluginCandidate,
    PluginOrigin,
    PluginRegistry,
    PluginStatus,
    publish_plugin_event,
)
from shelfhold.plugin_host.rpc.host_side import RpcSession
from shelfhold.plugin_host.rpc.protocol import RpcLimits
from tests.auth_env import (
    CSRF_COOKIE,
    CSRF_HEADER,
    HTTPS,
    PASSWORD,
    auth_env,
    db_rows,
    run_cli,
)
from tests.integration.test_plugin_attestation import ADMIN as ATTEST_ACTOR
from tests.integration.test_plugin_attestation import _third_party_binding
from tests.integration.test_untrusted_epub_pipeline import (
    TRAPPED,
    has_active_content,
    ingest,
    set_binding_trust,
)
from tests.library_env import lib_env
from tests.plugin_guard_env import (
    CANARY,
    PHRASE,
    Cli,
    build_wheel,
    make_cli,
    manifest,
    plugin_wheel,
)
from tests.unit.test_plugin_rpc import MANIFEST, pair, run

ENABLE = {"SHELFHOLD_ALLOW_UNSANDBOXED_PLUGINS": "1"}
KEY = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")


@pytest.fixture
def cli(tmp_path: Path) -> Cli:
    return make_cli(tmp_path)


@pytest.fixture
def probe(tmp_path: Path) -> Path:
    return tmp_path / "probe.jsonl"


def snapshots(probe: Path) -> list[dict[str, object]]:
    if not probe.exists():
        return []
    return [json.loads(line) for line in probe.read_text(encoding="utf-8").splitlines()]


def install(
    cli: Cli, tmp_path: Path, probe: Path, dist: str = "demo-plug", key: str = "demo"
) -> Path:
    wheel = plugin_wheel(tmp_path, dist, key, probe, dist.replace("-", "_"))
    done = cli.run("plugin", "install", str(wheel.path), "--sha256", wheel.sha256)
    assert done.returncode == 0, done.stderr
    return wheel.path


# -- the names of 03, for the two modes (D13) ----------------------------------------------------


@pytest.mark.req("EF-SRC-019")
def test_third_party_plugins_disabled_by_default(cli: Cli, tmp_path: Path, probe: Path) -> None:
    """Installed: not loaded, the reason is shown, nothing of the plugin ran; the recorded mode
    is isolated (D13) and a manifest that presumes to choose its mode is refused, not obeyed."""
    install(cli, tmp_path, probe)
    started = cli.run("plugin", "list", "--load")
    assert "disabled" in started.stdout and "reserved" in started.stdout
    assert snapshots(probe) == []  # its code never ran
    # the recorded mode is the default of D13, whatever the wheel says
    state = json.loads((cli.data / "plugins" / "state.json").read_text(encoding="utf-8"))
    assert state["installed"]["demo-plug"]["mode"] == "isolated"
    # a plugin never chooses its mode: a manifest that asks for the host process is refused
    raw = {**manifest("demo"), "mode": "in_process"}
    registry = PluginRegistry(granted_distributions=["demo-plug"])
    candidate = PluginCandidate(
        "demo", PluginOrigin.THIRD_PARTY, lambda: SimpleNamespace(manifest=raw), dist="demo-plug"
    )
    (record,) = registry.load_all([candidate])
    assert record.status is PluginStatus.INCOMPATIBLE
    assert "mode" in (record.reason or "")
    assert state["installed"]["demo-plug"]["mode"] == "isolated"  # and nothing changed on disk


@pytest.mark.req("EF-SRC-019")
def test_unsandboxed_flag_requires_typed_confirmation(
    cli: Cli, tmp_path: Path, probe: Path
) -> None:
    """The isolated default needs neither the variable nor the phrase; the in-process mode keeps
    both, and going back to isolated ends the in-process grant (one mode at a time)."""
    install(cli, tmp_path, probe)
    # isolated (the default): enabling is the reserved action, no variable, no typed sentence
    assert cli.run("plugin", "enable", "demo-plug").returncode == 0
    assert cli.run("plugin", "disable", "demo-plug").returncode == 0
    assert snapshots(probe) == []
    # the in-process mode keeps the variable of M1b...
    refused = cli.run("plugin", "enable", "demo-plug", "--in-process", typed=PHRASE + "\n")
    assert refused.returncode == 1 and "SHELFHOLD_ALLOW_UNSANDBOXED_PLUGINS" in refused.stderr
    # ...and the typed confirmation
    wrong = cli.run("plugin", "enable", "demo-plug", "--in-process", typed="no\n", **ENABLE)
    assert wrong.returncode == 1
    done = cli.run("plugin", "enable", "demo-plug", "--in-process", typed=PHRASE + "\n", **ENABLE)
    assert done.returncode == 0, done.stderr
    assert "demo\tloaded" in cli.run("plugin", "list", "--load", **ENABLE).stdout
    assert len(snapshots(probe)) == 1
    # without the variable, the confirmation alone loads nothing
    started = cli.run("plugin", "list", "--load")
    assert "demo\tloaded" not in started.stdout and "ALLOW_UNSANDBOXED_PLUGINS" in started.stdout
    # back to isolated: the confirmation of the other mode counts for nothing
    done = cli.run("plugin", "enable", "demo-plug", "--isolated")
    assert done.returncode == 0, done.stderr
    started = cli.run("plugin", "list", "--load", **ENABLE)
    assert "demo\tloaded" not in started.stdout and "isolated" in started.stdout
    assert len(snapshots(probe)) == 1


@pytest.mark.req("ENF-SEC-009")
def test_plugin_site_packages_after_host_conflicting_names_refused(
    cli: Cli, tmp_path: Path, probe: Path
) -> None:
    """The names of the host, of its dependencies, of the standard library and of an installed
    plugin are refused, before and after normalisation; nothing else is installed."""
    install(cli, tmp_path, probe)
    for dist, module in (
        ("shelfhold", "plug_a"),  # the host
        ("Shelfhold.SDK", "plug_b"),  # the SDK, other spelling
        ("HTTPX", "plug_c"),  # a dependency, other spelling
        ("Demo_Plug", "plug_d"),  # the installed plugin, other spelling
        ("fine-name", "httpx"),  # a module of a host dependency
        ("fine-name", "json"),  # the standard library
    ):
        wheel = build_wheel(
            tmp_path, dist, {f"{module}/__init__.py": ""}, entry=("k", f"{module}:x")
        )
        done = cli.run("plugin", "install", str(wheel.path), "--sha256", wheel.sha256)
        assert done.returncode == 1 and "conflict" in done.stderr, (dist, module, done.stderr)
    names = {p.name for p in (cli.data / "plugins" / "site-packages").iterdir()}
    assert names == {"demo_plug", "demo_plug-1.0.dist-info"}


@pytest.mark.req("ENF-SEC-009")
def test_plugin_install_only_binary_require_hashes_no_deps(
    cli: Cli, tmp_path: Path, probe: Path
) -> None:
    """Binary only, hashes required, nothing resolved, no index; and the volume the companion
    mounts: every file the core put down is readable by the runner's uid, writable by nobody
    else, and the recorded state stays the core's own."""
    argv = guard.install_command(["pip", "install"], tmp_path / "t", tmp_path / "r.txt")
    for flag in ("--only-binary", ":all:", "--require-hashes", "--no-deps", "--no-index"):
        assert flag in argv
    install(cli, tmp_path, probe)
    site = cli.data / "plugins" / "site-packages"
    assert (site / "demo_plug" / "__init__.py").exists()
    assert snapshots(probe) == []  # nothing of the plugin ran
    # the companion's volume (D-P1): the runner is another uid, in a read-only mount
    root = cli.data / "plugins"
    assert (root.stat().st_mode & 0o777) == 0o755
    assert (site.stat().st_mode & 0o777) == 0o755
    assert (site / "demo_plug").stat().st_mode & 0o777 == 0o755
    assert (site / "demo_plug" / "__init__.py").stat().st_mode & 0o777 == 0o644
    assert (root / "state.json").stat().st_mode & 0o777 == 0o600  # the core's own
    # a wrong hash is refused, nothing is installed, and the refusal is audited
    other = plugin_wheel(tmp_path, "other-plug", "other", probe, "other_plug")
    bad = cli.run("plugin", "install", str(other.path), "--sha256", "0" * 64)
    assert bad.returncode == 1 and "hash_mismatch" in bad.stderr
    assert not (site / "other_plug").exists()
    assert ("plugin.install_refused", "other_plug-1.0-py3-none-any.whl:hash_mismatch") in (
        cli.audit_actions()
    )


@pytest.mark.req("EF-SRC-019", "ENF-SEC-013")
def test_no_secret_passed_to_plugin(cli: Cli, tmp_path: Path, probe: Path) -> None:
    """The in-process mode (the name of 03): the loader hands the plugin nothing — no argument, no
    settings object — and the environment it is imported in carries no secret-looking name."""
    install(cli, tmp_path, probe)
    done = cli.run("plugin", "enable", "demo-plug", "--in-process", typed=PHRASE + "\n", **ENABLE)
    assert done.returncode == 0, done.stderr
    done = cli.run(
        "plugin", "list", "--load", SHELFHOLD_SECRET_KEY_FILE=str(tmp_path / "key"), **ENABLE
    )
    assert "demo\tloaded" in done.stdout
    (snap,) = snapshots(probe)
    assert CANARY not in json.dumps(snap)  # the token, the key path, every secret-looking name
    environ = snap["environ"]
    assert isinstance(environ, dict)
    assert "SHELFHOLD_API_TOKEN" not in environ and "SHELFHOLD_SECRET_KEY_FILE" not in environ
    assert snap["argv"][-2:] == ["list", "--load"]  # type: ignore[index]
    # the loader hands the plugin nothing: no argument, no settings object
    from shelfhold.plugin_host import registry

    class Entry:
        module = "m"
        name = "k"

        def load(self, *args: object, **kwargs: object) -> object:
            assert (args, kwargs) == ((), {})
            return object()

    load = registry._third_party_loader(tmp_path, Entry(), ())
    saved = list(sys.meta_path)
    try:
        assert load() is not None
    finally:
        sys.meta_path[:] = saved


# -- the isolated case: attestation and events over the wire (EF-EXP-065) ------------------------


def isolated_manifest(**extra: Any) -> dict[str, Any]:
    raw = json.loads(FakeRangedConnectedService().manifest.model_dump_json())
    return {**raw, "api": API_VERSION, **extra}


@pytest.mark.req("EF-EXP-065")
def test_third_party_publisher_trust_requires_admin_attestation() -> None:
    """The M6 wording, applied to a plugin that runs isolated: its declared `publisher` is a
    proposal until the admin attests it, and the EPUB stays neutralized until then."""

    async def go() -> None:
        channel, peer = await pair()
        manifest = isolated_manifest(key="acme", content_trust="publisher")

        async def play() -> None:
            hello = await peer.read()
            await peer.reply(hello, {"rpc_protocol": 1})
            asked = await peer.read()
            await peer.reply(asked, manifest)

        opening = asyncio.ensure_future(play())

        async def connect() -> RpcSession:
            return await RpcSession.open(channel, key="acme", limits=RpcLimits())

        registry = PluginRegistry()
        record = await registry.load_isolated("acme", PluginOrigin.THIRD_PARTY, connect)
        await opening
        try:
            assert record.status is PluginStatus.LOADED, record.reason
            assert record.manifest is not None
            assert record.manifest.content_trust == "untrusted"  # the safe default applies
            assert record.proposed == {"content_trust": "publisher"}  # what it asked for
            registry.apply_attestations({"acme": {"content_trust": "publisher"}})
            attested = registry.get("acme")
            assert attested is not None and attested.manifest is not None
            assert attested.manifest.content_trust == "publisher"
            registry.apply_attestations({})  # the word withdrawn: back to the safe default
            withdrawn = registry.get("acme")
            assert withdrawn is not None and withdrawn.manifest is not None
            assert withdrawn.manifest.content_trust == "untrusted"
        finally:
            if record.instance is not None:
                await record.instance.aclose()

    run(go)
    # and the EPUB it produces stays neutralized until the word is given for its version
    with lib_env() as env:
        attestations = _third_party_binding(env)
        set_binding_trust(env, "publisher")  # even a binding row that says so is not enough
        with env.engine.connect() as connection:
            assert not publisher_attested(connection, "acme")
            assert trust_of(connection, env.chain["variant"]) == "untrusted"
        _result, library = ingest(env, TRAPPED)
        assert not has_active_content(library.read_bytes())  # neutralized
        attestations.attest("acme", "content_trust", "publisher", actor=ATTEST_ACTOR)
        with env.engine.connect() as connection:
            assert publisher_attested(connection, "acme")
            assert trust_of(connection, env.chain["variant"]) == "publisher"


@pytest.mark.req("EF-EXP-065")
def test_isolated_plugin_events_namespaced_over_rpc() -> None:
    """An isolated plugin publishes in `plugin.<key>.*` and nowhere else: the host builds the type
    from the key it loaded the plugin under, and a name that would speak for another plugin or
    for the core is refused, without stopping the session."""

    class Bus:
        def __init__(self) -> None:
            self.events: list[tuple[str, Any]] = []

        def publish(self, connection: Any, event_type: str, payload: Any = None) -> None:
            self.events.append((event_type, payload))

    bus = Bus()

    def sink(key: str, name: str, payload: Any) -> None:
        publish_plugin_event(bus, None, key, name, payload)

    async def go() -> None:
        channel, peer = await pair()

        async def play() -> None:
            hello = await peer.read()
            await peer.reply(hello, {"rpc_protocol": 1})
            asked = await peer.read()
            await peer.reply(asked, MANIFEST)

        opening = asyncio.ensure_future(play())

        async def connect() -> RpcSession:
            return await RpcSession.open(channel, key="rpc-demo", limits=RpcLimits(), on_event=sink)

        registry = PluginRegistry()
        record = await registry.load_isolated("rpc-demo", PluginOrigin.THIRD_PARTY, connect)
        await opening
        assert record.status is PluginStatus.LOADED, record.reason

        async def call_publishing(name: str, payload: Any) -> None:
            calling = asyncio.ensure_future(record.instance.search(None, "q", {}, None))
            request = await peer.read()  # the host call the plugin must name in its own request
            await peer.send(
                {
                    "jsonrpc": "2.0",
                    "id": 900,
                    "method": "events.publish",
                    "params": {"call": request["id"], "name": name, "payload": payload},
                }
            )
            await peer.read()  # the host's answer to that request
            await peer.reply(request, {"items": [], "next": None})
            await calling

        await call_publishing("scanned", {"pages": 3})
        assert bus.events == [("plugin.rpc-demo.scanned", {"pages": 3})]
        # a name that would leave its own space: refused, published nowhere, session alive
        await call_publishing("plugin.other.thing", {"x": 1})
        assert bus.events == [("plugin.rpc-demo.scanned", {"pages": 3})]
        await record.instance.aclose()

    run(go)


# -- enabling an isolated plugin is a reserved action (EF-USR-024, ENF-SEC-010) --------------------


@pytest.mark.req("EF-USR-024", "ENF-SEC-010", "EF-SRC-019")
def test_isolated_plugin_enable_is_reserved_action(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """The host CLI is one of the two credentials of the reserved action; over HTTP it takes a
    fresh admin session — never a key, a member, a child or a stale session — and the in-process
    mode is never enabled over the wire: its typed phrase cannot cross it."""
    # the host CLI: no variable, no typed sentence (the reserved action allows the CLI)
    host_dir = tmp_path / "cli"
    host_dir.mkdir()
    host_cli = make_cli(host_dir)
    probe = tmp_path / "cli-probe.jsonl"
    wheel = plugin_wheel(host_dir, "demo-plug", "demo", probe, "demo_plug")
    done = host_cli.run("plugin", "install", str(wheel.path), "--sha256", wheel.sha256)
    assert done.returncode == 0, done.stderr
    done = host_cli.run("plugin", "enable", "demo-plug")
    assert done.returncode == 0, done.stderr
    assert ("plugin.enable", f"demo-plug@{wheel.sha256}") in host_cli.audit_actions()
    started = host_cli.run("plugin", "list", "--load", **ENABLE)
    assert "demo\tloaded" not in started.stdout  # isolated: never loaded in the host process

    # over HTTP: the same reserved action, every refusal has its case
    wheel = build_wheel(
        tmp_path, "acme-plug", {"acme_plug/__init__.py": "plugin = None\n"},
        entry=("acme", "acme_plug:plugin"),
    )  # fmt: skip
    guard.install_wheel(guard.Layout.of(settings.data_dir), wheel.path, wheel.sha256)
    with auth_env(monkeypatch, settings) as api:
        path = "/api/v1/plugins/acme-plug/enabled"
        anonymous = TestClient(api.app, base_url=HTTPS)
        assert anonymous.post(path).status_code in {401, 403}
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "admin", "keys", "create", "--name", "scripts") == 0
        key = KEY.search(capsys.readouterr().out)
        assert key, "no key was printed"
        by_key = api.client.post(path, headers={"Authorization": f"Bearer {key.group(0)}"})
        assert by_key.status_code == 403 and by_key.json()["code"] == "credential_not_allowed"
        # the UI explains why the plugin is not loaded (EF-SRC-019)
        assert api.login().status_code == 200
        listed = api.client.get("/api/v1/plugins")
        assert listed.status_code == 200, listed.text
        (row,) = listed.json()["items"]
        assert (row["name"], row["mode"], row["status"]) == ("acme-plug", "isolated", "disabled")
        assert "reserved" in (row["reason"] or "")
        # a stale session reauthenticates: the action is reserved to a fresh one
        api.clock.advance(timedelta(hours=1))
        stale = api.post(path)
        assert stale.status_code == 403 and stale.json()["code"] == "reauth_required"
        # a member and a child are refused: reserved means admin
        api.client.cookies.clear()
        assert api.login().status_code == 200
        for username, role in (("member1", "member"), ("kid", "child")):
            made = api.post(
                "/api/v1/profiles", json={"username": username, "password": PASSWORD, "role": role}
            )
            assert made.status_code in {200, 201}, made.text
        for username in ("member1", "kid"):
            other = TestClient(api.app, base_url=HTTPS)
            assert (
                other.post(
                    "/api/v1/auth/login", json={"username": username, "password": PASSWORD}
                ).status_code
                == 200
            )
            refused = other.post(path, headers={CSRF_HEADER: other.cookies.get(CSRF_COOKIE)})
            assert refused.status_code == 403, refused.text
        # a fresh admin session enables it
        api.client.cookies.clear()
        assert api.login().status_code == 200
        done = api.post(path)
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "enabled"
        state = json.loads(
            (settings.data_dir / "plugins" / "state.json").read_text(encoding="utf-8")
        )
        assert "acme-plug" in state["enabled"]
        # the in-process mode is never enabled over HTTP: no typed phrase can cross the wire
        guard.set_mode(guard.Layout.of(settings.data_dir), "acme-plug", guard.MODE_IN_PROCESS)
        api.client.cookies.clear()
        assert api.login().status_code == 200
        refused = api.post(path)
        assert refused.status_code == 409 and refused.json()["code"] == "plugin.not_isolated"
        # the allowed action is audited, with its actor
        actions = [row[0] for row in db_rows(settings, "SELECT action FROM audit")]
        assert "plugin.enable_isolated" in actions
