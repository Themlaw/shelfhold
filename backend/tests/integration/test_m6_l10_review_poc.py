"""M6 L10, relecture adverse : ce que le démarrage des plugins isolés par le cœur laisse passer.

1. Une distribution accordée dont le point d'entrée vise le module d'une autre distribution, jamais
   activée : le cœur demande au compagnon de lancer ce module-là (le chargement en processus le
   refuse, `third_party_candidates` ; `third_party_entries` ne fait pas ce contrôle).
2. Un compagnon lent : le démarrage du cœur attend chaque plugin, l'un après l'autre, avant de
   servir la moindre requête.
3. L'arrêt : les sessions des plugins isolés démarrés au lancement restent ouvertes.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import migrate
from shelfhold.main import create_app
from shelfhold.plugin_host.registry import PluginStatus
from shelfhold.plugin_host.rpc.protocol import Channel
from tests.auth_env import run_cli
from tests.integration.test_m6_example_plugin import (
    _start_plugins_in,
    install_example_from_index,
    install_plugin,
    plugin_wheel,
    start_core,
)
from tests.plugin_guard_env import build_wheel, manifest


@pytest.fixture
def entries_asked(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every `(key, entry)` the core asks the companion for; the companion refuses each one."""
    from shelfhold.plugin_host.adapters.rpc.runner_socket import RunnerUnavailable

    asked: list[tuple[str, str]] = []

    async def refusing(_socket: Any, *, key: str, entry: str, **_: Any) -> Channel:
        asked.append((key, entry))
        raise RunnerUnavailable("the plugin runner is not reachable: FileNotFoundError")

    monkeypatch.setattr("shelfhold.main.connect_runner", refusing)
    return asked


@pytest.mark.req("EF-SRC-019", "ENF-SEC-009")
def test_poc_core_never_starts_the_module_of_a_distribution_that_is_not_enabled(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    tmp_path: Path,
    entries_asked: list[tuple[str, str]],
) -> None:
    """`acme-plug` est activé ; son point d'entrée vise `other_plug:plugin`, le module de
    `other-plug`, installé et jamais activé. Le cœur ne doit pas demander ce module au compagnon."""
    migrate(settings)
    install_plugin(monkeypatch, settings, plugin_wheel(tmp_path, "other-plug", "other"))
    source = f"manifest = {manifest('acme')!r}\n"
    hijack = build_wheel(
        tmp_path, "acme-plug", {"acme_plug/__init__.py": source}, entry=("acme", "other_plug:plugin")
    )
    install_plugin(monkeypatch, settings, hijack)
    assert run_cli(monkeypatch, settings, "plugin", "enable", "acme-plug") == 0

    start_core(settings)
    outside = [(k, e) for (k, e) in entries_asked if e.partition(":")[0].split(".")[0] != "acme_plug"]
    assert outside == [], f"the core asked the companion for another distribution's code: {outside}"


@pytest.mark.req("EF-SRC-019", "EF-SRC-016")
def test_poc_a_slow_companion_does_not_hold_the_core_start(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """Trois plugins accordés, un compagnon qui met 3 s à répondre à chacun : le cœur doit
    commencer à servir sans attendre les plugins (ici, en moins de 3 s)."""
    migrate(settings)
    for name in ("one", "two", "three"):
        install_plugin(monkeypatch, settings, plugin_wheel(tmp_path, name, name))
        assert run_cli(monkeypatch, settings, "plugin", "enable", name) == 0

    async def slow(_socket: Any, *, key: str, entry: str, **_: Any) -> Channel:
        from shelfhold.plugin_host.adapters.rpc.runner_socket import RunnerUnavailable

        await asyncio.sleep(3)
        raise RunnerUnavailable("the plugin runner did not answer: TimeoutError")

    monkeypatch.setattr("shelfhold.main.connect_runner", slow)

    async def scenario() -> float:
        app = create_app(settings)
        began = time.monotonic()
        async with app.router.lifespan_context(app):
            return time.monotonic() - began

    took = asyncio.run(scenario())
    assert took < 3, f"the core served nothing for {took:.1f} s, waiting on the companion"


@pytest.mark.req("EF-SRC-016", "EF-SRC-019")
def test_poc_core_shutdown_closes_the_isolated_sessions_it_opened(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    """Le plugin d'exemple, accordé, est démarré par le cœur dans son propre processus. Une fois
    l'application arrêtée, sa session doit être fermée et son processus terminé."""
    install_example_from_index(monkeypatch, settings, tmp_path)
    seen = _start_plugins_in(monkeypatch, settings)

    async def scenario() -> Any:
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            (record,) = seen["records"]
            assert record.status is PluginStatus.LOADED, (record.status, record.reason)
        await asyncio.sleep(0.5)
        return record.instance.session

    session = asyncio.run(asyncio.wait_for(scenario(), 120))
    assert session.state != "ready", "the plugin's session outlives the core's shutdown"

