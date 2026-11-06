# ruff: noqa: S106 (synthetic secrets)
"""The headless commands end to end in process (L38 coverage): the real CLI against a running
socket server (dispatcher, gateway, `config`/`target`/`source`/`shelf`), the direct channel, the
production wiring and the gateway against replies it must not trust."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.headless import HeadlessAdmin, wiring
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.fetcher import DnsBlocked, GuardedFetcher, IntegrationFetcher
from shelfhold.infrastructure.local_socket import SOCKET_NAME, Peer, RpcError
from shelfhold.infrastructure.migrate import migrate
from shelfhold.interfaces.local_cli import IdentityDispatcher, RemoteRefusal, SocketHeadless
from tests.auth_env import run_cli
from tests.headless_env import (
    KAVITA_PASSWORD,
    KAVITA_URL,
    SUWAYOMI_PASSWORD,
    SUWAYOMI_URL,
    Headless,
    headless,
    write_secret,
)
from tests.local_socket_env import serving

pytestmark = pytest.mark.req("EF-INT-024")


@contextmanager
def running_server(settings: Settings) -> Iterator[Headless]:
    """The server's socket in the data folder, where the CLI looks for it."""
    with headless(settings) as env:
        auth = local_auth(env.engine, settings)
        with serving(settings.data_dir / SOCKET_NAME, IdentityDispatcher(auth, headless=env.admin)):
            yield env


def _run(mp: pytest.MonkeyPatch, settings: Settings, *argv: str, stdin: str | None = None) -> int:
    return run_cli(mp, settings, *argv, stdin=stdin)


def test_every_command_through_the_running_server(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    pw = write_secret(tmp_path, "kavita.pw", KAVITA_PASSWORD + "\n")
    sec = write_secret(tmp_path, "suwayomi.secret", f"admin:{SUWAYOMI_PASSWORD}\n")
    with running_server(settings) as env:
        out = lambda: capsys.readouterr()  # noqa: E731
        assert _run(monkeypatch, settings, "config", "set", "source.check_mode", "piloted") == 0
        assert _run(monkeypatch, settings, "config", "get", "source.check_mode") == 0
        assert out().out.strip().endswith("piloted")
        assert _run(monkeypatch, settings, "config", "get", "public_url") == 0
        assert _run(monkeypatch, settings, "config", "list") == 0
        assert "source.check_mode = piloted" in out().out
        assert _run(monkeypatch, settings, "config", "list", "--json") == 0
        assert '"key": "public_url"' in out().out
        # a refusal of the server comes back as its catalog code, exit 1
        assert _run(monkeypatch, settings, "config", "get", "no.such") == 1
        assert "config.unknown_key" in out().err
        assert _run(monkeypatch, settings, "config", "set", "source.check_mode", "x") == 1
        assert _run(monkeypatch, settings, "config", "set", "source.check_mode") == 1  # no value
        assert (
            _run(
                monkeypatch,
                settings,
                "config",
                "set",
                "source.check_mode",
                "a",
                "--value-file",
                "-",
            )
            == 1
        )
        out()

        # a secret key: the value comes from stdin, never from argv
        assert _run(monkeypatch, settings, "config", "set", "notify.urls", "--value-file", "-",
                    stdin="ops=ntfy://ntfy.example.org/topic\n") == 0  # fmt: skip
        assert _run(monkeypatch, settings, "config", "set", "notify.urls", "--value-file",
                    str(tmp_path / "missing")) == 1  # fmt: skip
        assert _run(monkeypatch, settings, "config", "get", "notify.urls") == 0
        assert "ops" in out().out

        base = ["target", "add", "kavita", "--url", KAVITA_URL, "--user", "shelfhold",
                "--password-file", str(pw), "--accept-admin-risk"]  # fmt: skip
        assert _run(monkeypatch, settings, *base, "--path-map", "not json") == 1
        assert _run(monkeypatch, settings, *base, "--path-map",
                    '[{"local": "/library/manga", "remote": "/manga"}]') == 0  # fmt: skip
        assert "expire" in out().out
        assert _run(monkeypatch, settings, *base) == 0  # again: nothing to do
        assert "déjà présente" in out().out
        assert len(env.kavita.calls) == 1
        assert _run(monkeypatch, settings, *base[:-1], "--accept-admin-risk",
                    "--password-file", str(tmp_path / "missing")) == 1  # fmt: skip

        assert _run(monkeypatch, settings, "source", "add", "suwayomi", "--url", SUWAYOMI_URL,
                    "--secret-file", str(sec)) == 0  # fmt: skip
        assert _run(monkeypatch, settings, "source", "add", "suwayomi", "--url", SUWAYOMI_URL,
                    "--secret-file", str(sec)) == 0  # fmt: skip
        assert "mise à jour" in out().out
        assert _run(monkeypatch, settings, "source", "add", "suwayomi", "--url", SUWAYOMI_URL,
                    "--secret-file", str(tmp_path / "missing")) == 1  # fmt: skip
        env.suwayomi.mode = "ui_login"
        assert _run(monkeypatch, settings, "source", "add", "suwayomi", "--url", SUWAYOMI_URL) == 1
        assert "source.auth_mode_unsupported" in out().err

        assert _run(monkeypatch, settings, "shelf", "create", "Kids", "--max-age", "12",
                    "--target", "kavita") == 1  # fmt: skip
        assert "shelf.parental_control_unavailable" in out().err
        shelf = ["shelf", "create", "Manga", "--max-age", "18+", "--target", "kavita",
                 "--subpath", "manga", "--phrase", "Mangas"]  # fmt: skip
        assert _run(monkeypatch, settings, *shelf) == 0
        assert _run(monkeypatch, settings, *shelf) == 0
        assert "déjà présente" in out().out
        assert env.count("shelf_destination") == 1

        # apply: dry run, then for real, then nothing left to do; secrets travel by the socket
        file = tmp_path / "shelfhold.toml"
        file.write_text(
            'public_url = "https://shelf.example.org"\n'
            f'[[targets]]\nkind = "kavita"\nname = "k2"\nurl = "{KAVITA_URL}"\nuser = "u2"\n'
            f'password_file = "{pw}"\naccept_admin_risk = true\n',
            encoding="utf-8",
        )
        assert _run(monkeypatch, settings, "config", "apply", "-f", str(file), "--dry-run") == 0
        assert "target k2" in out().out and env.count("export_target") == 1
        assert _run(monkeypatch, settings, "config", "apply", "-f", str(file)) == 0
        assert env.count("export_target") == 2
        assert _run(monkeypatch, settings, "config", "apply", "-f", str(file)) == 0
        assert "k2" in out().out and env.count("export_target") == 2
        assert _run(monkeypatch, settings, "config", "apply", "-f", str(tmp_path / "no.toml")) == 1
        file.write_text('unknown = "x"\n', encoding="utf-8")
        assert _run(monkeypatch, settings, "config", "apply", "-f", str(file)) == 1
        assert "config.file_invalid" in out().err
        file.write_text(
            '[[sources]]\nkind = "suwayomi"\nurl = "http://s:1"\nsecret_file = "gone"\n'
        )
        assert _run(monkeypatch, settings, "config", "apply", "-f", str(file)) == 1


def test_direct_channel_commands_and_failures(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    # no database yet: the commands say so
    monkeypatch.setattr(wiring, "open_headless", lambda _s: pytest.fail("no database"))
    migrate(settings)
    settings.database_path.unlink()
    assert _run(monkeypatch, settings, "config", "list") == 1
    assert capsys.readouterr().err
    with headless(settings) as env:
        monkeypatch.setattr(wiring, "open_headless", lambda _s: env.admin)
        assert _run(monkeypatch, settings, "config", "set", "source.check_mode", "piloted") == 0
        assert _run(monkeypatch, settings, "config", "set", "public_url", "ftp://x") == 1
        assert (
            _run(monkeypatch, settings, "config", "set", "notify.urls", "a=b") == 1
        )  # argv refused
        err = capsys.readouterr().err
        assert "--value-file" in err
        sec = write_secret(tmp_path, "s", "admin:" + SUWAYOMI_PASSWORD + "\n")
        assert _run(monkeypatch, settings, "source", "add", "suwayomi", "--url", SUWAYOMI_URL,
                    "--secret-file", str(sec)) == 0  # fmt: skip
        # a secret file readable by everyone is reported
        sec.chmod(0o644)
        assert _run(monkeypatch, settings, "source", "add", "suwayomi", "--url", SUWAYOMI_URL,
                    "--secret-file", str(sec)) == 0  # fmt: skip
        assert "chmod 600" in capsys.readouterr().err
        assert (
            _run(
                monkeypatch, settings, "shelf", "create", "S", "--max-age", "X", "--target", "nope"
            )
            == 1
        )
        # a lock held with no socket: no other way, the commands refuse
        from shelfhold.infrastructure.instance import InstanceLock

        lock = InstanceLock(settings.data_dir)
        lock.acquire()
        try:
            assert _run(monkeypatch, settings, "config", "list") == 1
        finally:
            lock.release()


def test_wiring_builds_the_production_pieces(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrate(settings)
    admin = wiring.open_headless(settings)
    assert isinstance(admin, HeadlessAdmin)
    assert admin.get("source.check_mode").value == "always_on"
    # a declared address gets a lan_target; one the resolver blocks stays on the guarded fetcher
    assert isinstance(wiring.integration_fetcher("http://192.168.1.20:5000"), IntegrationFetcher)

    class Blocked:
        async def resolve(self, host: str, port: int) -> list[str]:
            raise DnsBlocked(host)

    monkeypatch.setattr(wiring, "SystemResolver", Blocked)
    public = wiring.integration_fetcher("https://kavita.example.org")
    assert isinstance(public, GuardedFetcher) and not isinstance(public, IntegrationFetcher)
    channels = wiring._Channels()
    with pytest.raises(ValueError, match="refused"):
        channels.validate("http://example.org/hook")


def _handler(reply: object) -> Any:
    async def handle(_peer: Peer, _method: str, _params: dict[str, Any]) -> object:
        if isinstance(reply, RpcError):
            raise reply
        return reply

    return handle


def test_the_gateway_distrusts_malformed_replies_and_keeps_refusal_codes(tmp_path: Path) -> None:
    sock = tmp_path / "s.sock"
    with serving(sock, _handler("garbage")):
        client = SocketHeadless(sock)
        for call in (
            lambda: client.get("k"),
            client.list,
            lambda: client.apply_text("", base_dir=tmp_path),
            lambda: client.target_add_kavita(
                name="k", url="u", username="u", password="p", accept_admin_risk=True
            ),
            lambda: client.source_add_suwayomi(url="u", secret=None),
            lambda: client.shelf_create(name="n", max_age="X", target="t"),
        ):
            with pytest.raises(ValueError, match="malformed_reply"):
                call()
    with serving(sock, _handler(RpcError(-32000, "no", {"code": "target.conflict"}))):
        with pytest.raises(RemoteRefusal) as refused:
            SocketHeadless(sock).set("k", "v")
        assert refused.value.code == "target.conflict"
    with serving(sock, _handler(RpcError(-32000, "no", {}))):
        from shelfhold.infrastructure.local_socket import RpcFailure

        with pytest.raises(RpcFailure):
            SocketHeadless(sock).get("k")
    with serving(
        sock,
        _handler(
            {
                "changes": [
                    {
                        "kind": "config",
                        "subject": "a",
                        "action": "create",
                        "before": None,
                        "after": "x",
                    }
                ]
            }
        ),
    ):
        plan = SocketHeadless(sock).apply_text("", base_dir=tmp_path, dry_run=True)
        assert [c.subject for c in plan.changes] == ["a"]


def test_facade_refusals_and_apply_variants(settings: Settings, tmp_path: Path) -> None:
    from shelfhold.headless import (
        ConfigFileError,
        InvalidConfigValue,
        ReadOnlyConfigKey,
        ShelfConflict,
        TargetConflict,
        parse_desired,
    )
    from shelfhold.headless.errors import SourceUnreachable

    with headless(settings) as env:
        a = env.admin
        # config values and keys
        with pytest.raises(InvalidConfigValue):
            a.set("notify.urls", "no-separator")
        with pytest.raises(InvalidConfigValue):
            a.set("notify.urls", "Bad Name=ntfy://x/y")
        with pytest.raises(InvalidConfigValue):
            a.set("path_map.kavita", "not json")
        with pytest.raises(InvalidConfigValue):
            a.set("path_map.kavita", '{"a": 1}')
        with pytest.raises(ReadOnlyConfigKey):
            a.set("maintenance.window", "x")
        # target and source add variants
        a.target_add_kavita(name="k", url=KAVITA_URL, username="u", password=KAVITA_PASSWORD,
                            accept_admin_risk=True)  # fmt: skip
        with pytest.raises(TargetConflict):
            a.target_add_kavita(name="k", url="http://other.internal:5000", username="u",
                                password=KAVITA_PASSWORD, accept_admin_risk=True)  # fmt: skip
        again = a.target_add_kavita(name="k", url=KAVITA_URL, username="u", password="x",
                                    accept_admin_risk=True,
                                    path_map=[{"local": "/library/a", "remote": "/a"}])  # fmt: skip
        assert not again.created and a.get("path_map.k").value[0]["local"] == "/library/a"
        with pytest.raises(InvalidConfigValue):
            a.source_add_suwayomi(url=SUWAYOMI_URL, secret="no-colon")
        env.suwayomi.mode = "basic_auth"
        with pytest.raises(InvalidConfigValue):
            a.source_add_suwayomi(url=SUWAYOMI_URL, secret=None)  # this server needs a login
        env.suwayomi.mode = "none"
        assert a.source_add_suwayomi(url=SUWAYOMI_URL, secret=None).auth_mode == "none"
        env.suwayomi.mode = "basic_auth"

        def down(**_kw: object) -> str:
            raise ConnectionError

        env.suwayomi.auth_mode = down  # type: ignore[method-assign]
        with pytest.raises(SourceUnreachable):
            a.source_add_suwayomi(url=SUWAYOMI_URL, secret=None)
        # shelves
        with pytest.raises(InvalidConfigValue):
            a.shelf_create(name="S", max_age="adults", target="k")
        a.shelf_create(name="S", max_age="18+", target="k", subpath="s")
        with pytest.raises(ShelfConflict):
            a.shelf_create(name="S", max_age="X", target="k", subpath="s")
        with pytest.raises(ShelfConflict):
            a.shelf_create(name="S", max_age="18+", target="k", subpath="other")

        # apply: a notification, a changed path_map, a changed Suwayomi address, conflicts
        pw = write_secret(tmp_path, "pw", KAVITA_PASSWORD)
        sec = write_secret(tmp_path, "sec", "admin:" + SUWAYOMI_PASSWORD)
        env.suwayomi.mode = "basic_auth"
        env.suwayomi.auth_mode = type(env.suwayomi).auth_mode.__get__(env.suwayomi)  # type: ignore[method-assign]
        text = (
            '[notify.urls]\nops = "ntfy://ntfy.example.org/t"\n'
            f'[[targets]]\nkind = "kavita"\nname = "k"\nurl = "{KAVITA_URL}"\nuser = "u"\n'
            f'password_file = "{pw}"\npath_map = [{{ local = "/library/b", remote = "/b" }}]\n'
            f'[[sources]]\nkind = "suwayomi"\nurl = "{SUWAYOMI_URL}"\nsecret_file = "{sec}"\n'
            '[[shelves]]\nname = "S"\nmax_age = "18+"\ntarget = "k"\nsubpath = "s"\n'
        )
        plan = a.apply(parse_desired(text, base_dir=tmp_path))
        actions = {c.kind: c.action for c in plan.changes}
        assert actions["notify"] == "create" and actions["target"] == "update"
        assert a.get("path_map.k").value[0]["local"] == "/library/b"
        assert a.apply(parse_desired(text, base_dir=tmp_path)).is_noop
        moved = text.replace(SUWAYOMI_URL, "http://other-suwayomi.internal:4567")
        [source] = [
            c
            for c in a.apply(parse_desired(moved, base_dir=tmp_path), dry_run=True).changes
            if c.kind == "source"
        ]
        assert source.action == "update" and source.before == SUWAYOMI_URL
        with pytest.raises(ShelfConflict):
            a.apply(
                parse_desired(text.replace('subpath = "s"', 'subpath = "z"'), base_dir=tmp_path)
            )
        with pytest.raises(ConfigFileError):
            a.apply(
                parse_desired(text.replace('kind = "kavita"', 'kind = "komga"'), base_dir=tmp_path)
            )
        with pytest.raises(ConfigFileError):
            a.apply(
                parse_desired(
                    text.replace('kind = "suwayomi"', 'kind = "other"'), base_dir=tmp_path
                )
            )
        with pytest.raises(ConfigFileError, match="cannot read"):
            a.apply(
                parse_desired(
                    text.replace(str(pw), str(tmp_path / "gone")).replace(
                        'name = "k"', 'name = "k9"\naccept_admin_risk = true'
                    ),
                    base_dir=tmp_path,
                )
            )
        with pytest.raises(TargetConflict):
            a.apply(
                parse_desired(text.replace(KAVITA_URL, "http://x.internal:1"), base_dir=tmp_path)
            )
        with pytest.raises(InvalidConfigValue):
            a.apply(parse_desired(text.replace("ntfy://ntfy", "http://ntfy"), base_dir=tmp_path))


def test_http_routes_cover_config_and_sources(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    from tests.auth_env import auth_env

    with (
        headless(settings) as env,
        auth_env(
            monkeypatch, settings, headless_admin=env.admin, public_url="https://shelf.example.org"
        ) as api,
    ):
        assert api.login().status_code == 200

        def patch(values: dict[str, str]) -> Any:
            return api.client.patch(
                "/api/v1/config", json={"values": values},
                headers={"X-CSRF-Token": api.csrf()},
            )  # fmt: skip

        assert patch({"source.check_mode": "piloted"}).status_code == 200
        bad = patch({"source.check_mode": "never"})
        assert bad.status_code == 422 and bad.json()["code"] == "config.invalid_value"
        assert patch({"notify.urls": "a=ntfy://x/y"}).status_code == 403  # a secret: not here
        assert patch({"public_url": "https://shelf.example.org"}).status_code == 200
        source = {"kind": "suwayomi", "url": SUWAYOMI_URL, "secret": f"a:{SUWAYOMI_PASSWORD}"}
        made = api.post("/api/v1/sources", json=source)
        assert made.status_code == 201 and made.headers["Location"].startswith("https://shelf")
        env.suwayomi.mode = "ui_login"
        refused = api.post("/api/v1/sources", json={**source, "url": "http://s2.internal:1"})
        assert refused.json()["code"] == "source.auth_mode_unsupported"
        other = api.post("/api/v1/sources", json={**source, "kind": "komga"})
        assert other.status_code == 422
        kind = api.post("/api/v1/targets", json={"kind": "komga", "name": "x", "url": "http://a",
                                                 "username": "u", "password": "p"})  # fmt: skip
        assert kind.status_code == 422
        unsafe = api.post("/api/v1/targets", json={"kind": "kavita", "name": "x",
                          "url": "https://8.8.8.8", "username": "u", "password": "p",
                          "accept_admin_risk": True})  # fmt: skip
        assert unsafe.status_code == 422
