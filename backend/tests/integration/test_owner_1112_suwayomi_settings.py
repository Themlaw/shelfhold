"""Owner decision 2026-11-12: `source add suwayomi` offers to switch off the two global update
filters of Suwayomi, only when the admin confirms (`fix_suwayomi_settings`). It is the only
write Shelfhold makes in the settings of Suwayomi: audited, and nothing else is touched."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.headless.errors import SourceUpdateFiltersBlocking
from tests.fakes.suwayomi import BASE_URL, FakeSuwayomi, FakeSuwayomiFetcher

TWO = ["excludeNotStarted", "excludeUnreadChapters"]


def _server(mode: str = "none") -> FakeSuwayomi:
    server = FakeSuwayomi(auth_mode=mode)
    server.expose_roles = True
    server.exclude_unread = True
    server.exclude_not_started = True
    return server


def _env(settings: Any, server: FakeSuwayomi) -> Any:
    from shelfhold.plugin_host.adapters.meta.suwayomi.probe import HttpSuwayomiProbe
    from tests.fakes.suwayomi.server import FakeSuwayomiFetcher as Fetcher
    from tests.headless_env import SUWAYOMI_URL, headless

    probe = HttpSuwayomiProbe(lambda _url: Fetcher(server, SUWAYOMI_URL))  # type: ignore[arg-type,return-value]
    return headless(settings, suwayomi=probe)


def _actions(env: Any) -> list[str]:
    with env.engine.connect() as connection:
        return [r[0] for r in connection.execute(sa.text("SELECT action FROM audit"))]


@pytest.mark.req("EF-SRC-026")
def test_adapter_disables_only_the_two_filters_with_set_settings() -> None:
    from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService

    server = _server()
    fetcher: Any = FakeSuwayomiFetcher(server)
    service = SuwayomiService(BASE_URL)
    assert asyncio.run(service.disable_update_filters(fetcher)) == []
    assert server.exclude_unread is False and server.exclude_not_started is False
    assert server.settings_writes == [{"excludeUnreadChapters": False, "excludeNotStarted": False}]


@pytest.mark.req("EF-SRC-033")
def test_the_fake_refuses_any_other_setting_write() -> None:
    """Attack: a write that touches another setting (or turns a filter on) is a violation."""
    server = _server()
    fetcher: Any = FakeSuwayomiFetcher(server)
    for body in (
        "mutation { setSettings(input: {settings: {authMode: NONE}}) { settings { authMode } } }",
        "mutation { setSettings(input: {settings: {excludeUnreadChapters: false, "
        'downloadsPath: "/x"}}) { settings { authMode } } }',
        "mutation { setSettings(input: {settings: {excludeUnreadChapters: true, "
        "excludeNotStarted: true}}) { settings { authMode } } }",
    ):
        asyncio.run(_post(fetcher, body))
    assert server.settings_writes == []
    assert len(server.violations) == 3
    assert server.exclude_unread is True


async def _post(fetcher: Any, query: str) -> Any:
    return await fetcher.post(BASE_URL + "/api/graphql", json={"query": query})


@pytest.mark.req("EF-SRC-026")
@pytest.mark.parametrize("mode", ["none", "ui_login"])
def test_source_add_with_confirmation_fixes_audits_and_adds(mode: str, settings: Any) -> None:
    from tests.fakes.suwayomi.server import PASSWORD, USER
    from tests.headless_env import SUWAYOMI_URL

    server = _server(mode)
    secret = f"{USER}:{PASSWORD}" if mode == "ui_login" else None
    with _env(settings, server) as env:
        added = env.admin.source_add_suwayomi(
            url=SUWAYOMI_URL, secret=secret, fix_suwayomi_settings=True
        )
        assert added.created
        assert server.exclude_unread is False and server.exclude_not_started is False
        assert server.settings_writes == [
            {"excludeUnreadChapters": False, "excludeNotStarted": False}
        ]
        assert _actions(env).count("source.suwayomi_settings_fixed") == 1


@pytest.mark.req("EF-SRC-026")
def test_source_add_without_confirmation_never_writes(settings: Any) -> None:
    """Attack: the refusal stays a refusal, shows the option, and Suwayomi is untouched."""
    from tests.headless_env import SUWAYOMI_URL

    server = _server()
    with _env(settings, server) as env:
        with pytest.raises(SourceUpdateFiltersBlocking) as refused:
            env.admin.source_add_suwayomi(url=SUWAYOMI_URL, secret=None)
        assert "--fix-suwayomi-settings" in str(refused.value)
        assert server.settings_writes == []
        assert server.exclude_unread is True and server.exclude_not_started is True
        assert "source.suwayomi_settings_fixed" not in _actions(env)
        assert env.count("source_config") == 0


@pytest.mark.req("EF-SRC-026")
def test_confirmation_with_nothing_to_fix_writes_nothing(settings: Any) -> None:
    from tests.headless_env import SUWAYOMI_URL

    server = FakeSuwayomi(auth_mode="none")
    with _env(settings, server) as env:
        env.admin.source_add_suwayomi(url=SUWAYOMI_URL, secret=None, fix_suwayomi_settings=True)
        assert server.settings_writes == []
        assert "source.suwayomi_settings_fixed" not in _actions(env)


@pytest.mark.req("EF-SRC-026")
def test_a_write_that_does_not_stick_leaves_the_refusal(settings: Any) -> None:
    from tests.headless_env import SUWAYOMI_URL

    server = _server()
    server.ignore_settings_writes = True  # the server accepts the call but keeps the values
    with _env(settings, server) as env:
        with pytest.raises(SourceUpdateFiltersBlocking):
            env.admin.source_add_suwayomi(url=SUWAYOMI_URL, secret=None, fix_suwayomi_settings=True)
        assert env.count("source_config") == 0
        assert "source.suwayomi_settings_fixed" not in _actions(env)


@pytest.mark.req("EF-SRC-026")
def test_cli_option_and_api_flag_carry_the_confirmation() -> None:
    from shelfhold.interfaces.cli.registry import build_parser

    args = build_parser().parse_args(
        ["source", "add", "suwayomi", "--url", "http://s:4567", "--fix-suwayomi-settings"]
    )
    assert args.fix_suwayomi_settings is True
    args = build_parser().parse_args(["source", "add", "suwayomi", "--url", "http://s:4567"])
    assert args.fix_suwayomi_settings is False


# -- the refusals, the other doors and the other modes -------------------------------------------


@pytest.mark.req("EF-SRC-026")
def test_a_refused_or_failed_write_keeps_the_refusal_and_stores_nothing(settings: Any) -> None:
    from shelfhold.headless.errors import InvalidConfigValue, SourceUnreachable
    from tests.headless_env import SUWAYOMI_URL, headless

    def failing(error: Exception) -> Any:
        def disable(**_kw: object) -> list[str]:
            raise error

        return disable

    with headless(settings) as env:
        env.suwayomi.mode = "none"
        env.suwayomi.blocking_filters = list(TWO)
        env.suwayomi.disable_update_filters = failing(ConnectionError())  # type: ignore[method-assign]
        with pytest.raises(SourceUnreachable):
            env.admin.source_add_suwayomi(url=SUWAYOMI_URL, secret=None, fix_suwayomi_settings=True)
        env.suwayomi.disable_update_filters = failing(PermissionError())  # type: ignore[method-assign]
        with pytest.raises(InvalidConfigValue):
            env.admin.source_add_suwayomi(url=SUWAYOMI_URL, secret=None, fix_suwayomi_settings=True)
        env.suwayomi.disable_update_filters = failing(RuntimeError("boom"))  # type: ignore[method-assign]
        with pytest.raises(SourceUpdateFiltersBlocking):  # a server that refuses the write
            env.admin.source_add_suwayomi(url=SUWAYOMI_URL, secret=None, fix_suwayomi_settings=True)
        assert env.count("source_config") == 0
        assert "source.suwayomi_settings_fixed" not in _actions(env)


@pytest.mark.req("EF-SRC-026")
def test_probe_writes_with_basic_auth_and_maps_failures(settings: Any) -> None:
    from shelfhold.infrastructure.fetcher import FetchError
    from shelfhold.plugin_host.adapters.meta.suwayomi.probe import HttpSuwayomiProbe
    from tests.fakes.suwayomi.server import PASSWORD, USER
    from tests.fakes.suwayomi.server import FakeSuwayomiFetcher as Fetcher
    from tests.headless_env import SUWAYOMI_URL

    server = _server("basic_auth")
    probe = HttpSuwayomiProbe(lambda _url: Fetcher(server, SUWAYOMI_URL))  # type: ignore[arg-type,return-value]
    left = probe.disable_update_filters(
        url=SUWAYOMI_URL, mode="basic_auth", username=USER, password=PASSWORD
    )
    assert left == [] and server.settings_writes and server.exclude_unread is False
    # wrong password: the server refuses, nothing is written, the caller hears a refusal
    server = _server("basic_auth")
    probe = HttpSuwayomiProbe(lambda _url: Fetcher(server, SUWAYOMI_URL))  # type: ignore[arg-type,return-value]
    with pytest.raises(PermissionError):
        probe.disable_update_filters(
            url=SUWAYOMI_URL,
            mode="basic_auth",
            username=USER,
            password="wrong-synthetic-pw",  # noqa: S106 - throwaway synthetic value
        )
    assert server.settings_writes == [] and server.exclude_unread is True

    def unreachable(_url: str) -> Any:
        raise FetchError("down")

    with pytest.raises(ConnectionError):
        HttpSuwayomiProbe(unreachable).disable_update_filters(  # type: ignore[arg-type]
            url=SUWAYOMI_URL, mode="none", username=None, password=None
        )


@pytest.mark.req("EF-SRC-026")
def test_http_route_and_cli_carry_the_confirmation(
    monkeypatch: pytest.MonkeyPatch, settings: Any, capsys: Any
) -> None:
    from tests.auth_env import auth_env
    from tests.headless_env import SUWAYOMI_URL, headless

    with (
        headless(settings) as env,
        auth_env(
            monkeypatch, settings, headless_admin=env.admin, public_url="https://shelf.example.org"
        ) as api,
    ):
        assert api.login().status_code == 200
        env.suwayomi.mode = "none"
        env.suwayomi.blocking_filters = list(TWO)
        body = {"kind": "suwayomi", "url": SUWAYOMI_URL}
        refused = api.post("/api/v1/sources", json=body)
        assert refused.status_code == 409 and "update_filters_blocking" in refused.json()["code"]
        assert not any(c.get("fix") for c in env.suwayomi.calls)  # no write without the flag
        done = api.post("/api/v1/sources", json={**body, "fix_suwayomi_settings": True})
        assert done.status_code == 201
        assert any(c.get("fix") for c in env.suwayomi.calls)


@pytest.mark.req("EF-SRC-026")
def test_cli_in_process_sends_the_flag_through_the_socket(
    monkeypatch: pytest.MonkeyPatch, settings: Any, capsys: Any
) -> None:
    from tests.integration.test_headless_coverage import _run, running_server

    with running_server(settings) as env:
        env.suwayomi.mode = "none"
        env.suwayomi.blocking_filters = list(TWO)
        args = ("source", "add", "suwayomi", "--url", "http://suwayomi.internal:4567")
        capsys.readouterr()
        assert _run(monkeypatch, settings, *args) == 1
        assert "--fix-suwayomi-settings" in capsys.readouterr().err
        assert _run(monkeypatch, settings, *args, "--fix-suwayomi-settings") == 0
        assert any(c.get("fix") for c in env.suwayomi.calls)
