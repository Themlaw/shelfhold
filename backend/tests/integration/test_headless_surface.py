"""EF-INT-024, ENF-SEC-016: the socket methods, the CLI commands, the HTTP routes and the public
URL of the headless configuration (L38)."""
# ruff: noqa: S105, E501 (synthetic secrets; the tables of arguments read better long)

import ast
import re
from pathlib import Path

import pytest
from fastapi import APIRouter

from shelfhold.config import Settings
from shelfhold.headless import PublicUrlMissing, absolute_url, wiring
from shelfhold.identity.application.service import LocalAuth
from shelfhold.identity.public import RESERVED_ACTIONS, local_auth
from shelfhold.infrastructure.local_socket import RpcFailure, call
from shelfhold.infrastructure.migrate import migrate
from shelfhold.interfaces.cli.registry import build_parser
from shelfhold.interfaces.http_api.headless import build_headless_router
from shelfhold.interfaces.local_cli import IdentityDispatcher
from shelfhold.interfaces.local_cli.dispatch import HEADLESS_METHODS
from tests.auth_env import auth_env, run_cli
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

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"


def _auth(env: Headless) -> LocalAuth:
    return local_auth(env.engine, env.settings)


@pytest.mark.req("EF-INT-024")
def test_headless_config_needs_no_sql(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """From an empty database to a shelf bound to a Kavita destination, by commands only."""
    with headless(settings) as env:
        sock = tmp_path / "s.sock"
        with serving(sock, IdentityDispatcher(_auth(env), headless=env.admin)):
            call(sock, "config.set", {"key": "public_url", "value": "https://shelf.example.org"})
            call(sock, "config.set", {"key": "source.check_mode", "value": "always_on"})
            target = call(
                sock, "target.add",
                {"kind": "kavita", "name": "kavita", "url": KAVITA_URL, "username": "shelfhold",
                 "password": KAVITA_PASSWORD, "accept_admin_risk": True},
            )  # fmt: skip
            call(sock, "config.set", {"key": "path_map.kavita",
                                      "value": '[{"local": "/library/manga", "remote": "/manga"}]'})  # fmt: skip
            call(sock, "source.add", {"kind": "suwayomi", "url": SUWAYOMI_URL,
                                      "secret": f"admin:{SUWAYOMI_PASSWORD}"})  # fmt: skip
            shelf = call(sock, "shelf.create", {"name": "Manga", "max_age": "18+",
                                                "target": "kavita", "subpath": "manga"})  # fmt: skip
            listed = call(sock, "config.list", {})
            # the secret never comes back on the wire either
            assert KAVITA_PASSWORD not in repr((target, shelf, listed))
            with pytest.raises(RpcFailure) as refused:
                call(sock, "target.add", {"kind": "kavita", "name": "k2", "url": KAVITA_URL,
                                          "username": "u", "password": "p" * 12})  # fmt: skip
            assert (refused.value.data or {}).get("code") == "target.admin_risk_not_accepted"
        assert env.count("export_target") == 1 and env.count("shelf_destination") == 1
        assert env.count("source_config") == 1
    # and no step of the documented path asks for SQL
    docs = (SRC.parents[2] / "docs").rglob("*.md")
    steps = [d for d in docs if "headless" in d.name or "configuration" in d.name]
    for path in steps:
        assert not re.search(
            r"\bsqlite3\b|\bINSERT INTO\b|\bUPDATE \w+ SET\b", path.read_text("utf-8")
        )


@pytest.mark.req("EF-INT-024", "EF-USR-024")
def test_headless_methods_are_whitelisted_with_their_reserved_actions() -> None:
    expected = {
        "config.get": None, "config.list": None, "config.set": None, "config.apply": None,
        "target.add": "admin_risk.accept", "source.add": "source.set_secret",
        "shelf.create": "shelf.max_age_rating",
    }  # fmt: skip
    assert {name: reserved for name, (_impl, reserved) in HEADLESS_METHODS.items()} == expected
    assert all(reserved in RESERVED_ACTIONS for reserved in expected.values() if reserved)


@pytest.mark.req("EF-INT-024", "ENF-SEC-013")
def test_secrets_are_never_read_from_argv() -> None:
    parser = build_parser()
    for argv in (
        ["target", "add", "kavita", "--url", "http://k", "--user", "u", "--password", "x"],
        ["target", "add", "kavita", "--url", "http://k", "--user", "u", "--secret", "x"],
        ["source", "add", "suwayomi", "--url", "http://s", "--secret", "x"],
        ["source", "add", "suwayomi", "--url", "http://s", "--password", "x"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(argv)
    ok = parser.parse_args(["target", "add", "kavita", "--url", "http://k", "--user", "u",
                            "--password-file", "-", "--accept-admin-risk"])  # fmt: skip
    assert ok.accept_admin_risk is True and ok.password_file == "-"


@pytest.mark.req("EF-INT-024", "ENF-SEC-013")
def test_cli_target_add_stops_on_the_warning_without_the_flag(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    migrate(settings)
    with headless(settings) as env:
        monkeypatch.setattr(wiring, "open_headless", lambda _settings: env.admin)
        pw = write_secret(tmp_path, "pw", KAVITA_PASSWORD + "\n")
        base = ["target", "add", "kavita", "--url", KAVITA_URL, "--user", "shelfhold",
                "--password-file", str(pw)]  # fmt: skip
        assert run_cli(monkeypatch, settings, *base) != 0
        err = capsys.readouterr().err
        assert "administrateur" in err and "--accept-admin-risk" in err
        assert env.kavita.calls == [] and env.count("export_target") == 0
        # the password comes from stdin when the file is "-"
        stdin = [*base[:-1], "-", "--accept-admin-risk"]
        assert run_cli(monkeypatch, settings, *stdin, stdin=KAVITA_PASSWORD + "\n") == 0
        assert env.kavita.calls[0]["password"] == KAVITA_PASSWORD
        out = capsys.readouterr()
        assert KAVITA_PASSWORD not in out.out + out.err


@pytest.mark.req("EF-INT-024")
def test_cli_config_apply_dry_run_prints_the_diff_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    file = tmp_path / "shelfhold.toml"
    file.write_text('public_url = "https://shelf.example.org"\n', encoding="utf-8")
    with headless(settings) as env:
        monkeypatch.setattr(wiring, "open_headless", lambda _settings: env.admin)
        assert run_cli(monkeypatch, settings, "config", "apply", "-f", str(file), "--dry-run") == 0
        assert "public_url" in capsys.readouterr().out
        assert env.count("app_setting") == 0
        assert run_cli(monkeypatch, settings, "config", "apply", "-f", str(file)) == 0
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "config", "get", "public_url") == 0
        assert capsys.readouterr().out.strip() == "https://shelf.example.org"
        assert (
            run_cli(monkeypatch, settings, "config", "set", "notify.urls", "ops=ntfy://x/tok") != 0
        )
        assert "--value-file" in capsys.readouterr().err  # a secret key is never taken from argv


def _router(env: Headless, public_url: str | None) -> APIRouter:
    return build_headless_router(lambda: env.admin, lambda: public_url)


@pytest.mark.req("ENF-SEC-016", "EF-INT-024")
def test_absolute_urls_built_from_configured_public_url(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    assert absolute_url("https://shelf.example.org", "/api/v1/targets/01J") == (
        "https://shelf.example.org/api/v1/targets/01J"
    )
    assert absolute_url("https://shelf.example.org/base/", "api/v1/x") == (
        "https://shelf.example.org/base/api/v1/x"
    )
    with pytest.raises(PublicUrlMissing):
        absolute_url(None, "/api/v1/x")

    with headless(settings) as env:
        router = _router(env, "https://shelf.example.org")
        with auth_env(monkeypatch, settings, routers=[router]) as api:
            assert api.login().status_code == 200
            created = api.post(
                "/api/v1/targets",
                json={"kind": "kavita", "name": "kavita", "url": KAVITA_URL, "username": "u",
                      "password": KAVITA_PASSWORD, "accept_admin_risk": True},
                headers={"Host": "evil.example", "X-Forwarded-Host": "evil.example",
                         "X-Forwarded-Proto": "http"},
            )  # fmt: skip
            assert created.status_code == 201, created.text
            location = created.headers["Location"]
            assert location == f"https://shelf.example.org/api/v1/targets/{created.json()['id']}"
            assert "evil" not in created.text and KAVITA_PASSWORD not in created.text
            denied = api.post(
                "/api/v1/targets",
                json={"kind": "kavita", "name": "k2", "url": KAVITA_URL, "username": "u",
                      "password": KAVITA_PASSWORD},
            )  # fmt: skip
            assert denied.status_code == 409
            assert denied.json()["code"] == "target.admin_risk_not_accepted"
            current = api.client.get("/api/v1/config")
            assert current.status_code == 200
            assert {row["key"] for row in current.json()["items"]} >= {"public_url"}

    # no URL is ever built from the request: not in the interfaces, not in the facade
    offenders: list[str] = []
    for folder in ("interfaces", "headless"):
        for path in (SRC / folder).rglob("*.py"):
            tree = ast.parse(path.read_text("utf-8"))
            only_path = {  # `request.url.path` is a path, not a link
                id(n.value)
                for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and n.attr == "path"
            }
            for node in ast.walk(tree):
                if id(node) in only_path:
                    continue
                bad_attr = isinstance(node, ast.Attribute) and (
                    node.attr in {"base_url", "url_for"}
                    or (node.attr == "url" and isinstance(node.value, ast.Name)
                        and node.value.id == "request")
                )  # fmt: skip
                forwarded = isinstance(node, ast.Constant) and str(node.value).lower() in {
                    "x-forwarded-host",
                    "forwarded",
                }
                if bad_attr or forwarded:
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == []
