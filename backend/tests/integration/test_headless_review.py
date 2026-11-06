# ruff: noqa: E501 (synthetic secrets)
"""Review of PR #55 (L38): freshness of the admin-risk action, orphan key, secret-change marker,
`public_url` rules, slashes, target URL rule."""

import re
from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.headless import (
    HeadlessAdmin,
    InvalidConfigValue,
    TargetExchangeFailed,
    parse_desired,
)
from shelfhold.headless.applied import record_applied_public_url
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.interfaces.http_api.headless import build_headless_router
from tests.auth_env import auth_env, run_cli
from tests.headless_env import (
    KAVITA_PASSWORD,
    KAVITA_URL,
    SUWAYOMI_PASSWORD,
    SUWAYOMI_URL,
    headless,
    write_secret,
)

pytestmark = pytest.mark.req("EF-INT-024")

KEY_SHAPE = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")


def _body(name: str = "kavita") -> dict[str, object]:
    return {"kind": "kavita", "name": name, "url": KAVITA_URL, "username": "u",
            "password": KAVITA_PASSWORD, "accept_admin_risk": True}  # fmt: skip


def test_http_target_add_needs_a_fresh_session_even_without_the_hardened_profile(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with headless(settings) as env:
        router = build_headless_router(lambda: env.admin, lambda: None)
        with auth_env(monkeypatch, settings, routers=[router]) as api:
            assert api.login().status_code == 200
            api.clock.advance(timedelta(minutes=20))  # the password was typed 20 minutes ago
            stale = api.post("/api/v1/targets", json=_body())
            assert stale.status_code in {401, 403}
            assert stale.json()["code"] == "reauth_required"
            assert env.kavita.calls == [] and env.count("export_target") == 0
            assert api.login().status_code == 200
            assert api.post("/api/v1/targets", json=_body()).status_code == 201


def test_orphan_key_is_named_when_the_database_write_fails(settings: Settings) -> None:
    class FailingVault(SecretVault):
        def seal_row(self, connection: object, table: str, row_id: str, plaintext: bytes) -> None:
            raise sa.exc.OperationalError("seal", {}, Exception("disk full"))

    with headless(settings) as env:
        broken = HeadlessAdmin(
            env.engine, FailingVault(env.vault.keyring), clock=env.clock, kavita=env.kavita,
            suwayomi=env.suwayomi,
        )  # fmt: skip
        with pytest.raises(TargetExchangeFailed) as failed:
            broken.target_add_kavita(name="k", url=KAVITA_URL, username="u",
                                     password=KAVITA_PASSWORD, accept_admin_risk=True)  # fmt: skip
        assert env.count("export_target") == 0  # the transaction was rolled back
    text = str(failed.value)
    assert "shelfhold" in text and "revoke" in text and KAVITA_PASSWORD not in text


def test_apply_shows_a_changed_secret_or_account_without_the_value(
    settings: Settings, tmp_path: Path
) -> None:
    with headless(settings) as env:
        secret = write_secret(tmp_path, "s", f"admin:{SUWAYOMI_PASSWORD}\n")
        toml = f'[[sources]]\nkind = "suwayomi"\nurl = "{SUWAYOMI_URL}"\nsecret_file = "{secret}"\n'
        env.admin.apply(parse_desired(toml, base_dir=tmp_path))
        assert env.admin.apply(parse_desired(toml, base_dir=tmp_path), dry_run=True).is_noop
        secret.write_text("admin:another-password-0038\n")
        plan = env.admin.apply(parse_desired(toml, base_dir=tmp_path), dry_run=True)
        [change] = plan.changes
        assert (change.action, change.secret) == ("update", "changed")
        shown = plan.render()
        assert "secret: changed" in shown and "another-password-0038" not in shown
        # another Kavita account is a conflict, not a silent second key
        env.admin.target_add_kavita(name="kavita", url=KAVITA_URL, username="u",
                                    password=KAVITA_PASSWORD, accept_admin_risk=True)  # fmt: skip
        pw = write_secret(tmp_path, "pw", KAVITA_PASSWORD)
        other = (f'[[targets]]\nkind = "kavita"\nname = "kavita"\nurl = "{KAVITA_URL}"\n'
                 f'user = "someone-else"\npassword_file = "{pw}"\naccept_admin_risk = true\n')  # fmt: skip
        with pytest.raises(Exception, match="another Kavita account"):
            env.admin.apply(parse_desired(other, base_dir=tmp_path), dry_run=True)


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("http://kavita.internal:5000", True),
        ("http://192.168.1.20:5000", True),
        ("http://kavita:5000", True),
        ("http://localhost:5000", True),
        ("https://kavita.example.org", True),
        ("http://kavita.example.org", False),  # outside the LAN: https only
        ("https://8.8.8.8", False),  # a public IP is refused
        ("http://8.8.8.8", False),
    ],
)
def test_target_url_rule(settings: Settings, url: str, ok: bool) -> None:
    with headless(settings) as env:
        call = lambda: env.admin.target_add_kavita(  # noqa: E731
            name="k", url=url, username="u", password=KAVITA_PASSWORD, accept_admin_risk=True
        )
        if ok:
            call()
        else:
            with pytest.raises(InvalidConfigValue):
                call()
            assert env.kavita.calls == []


def test_trailing_slash_never_redirects_to_a_host_built_location(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with headless(settings) as env:
        router = build_headless_router(lambda: env.admin, lambda: "https://shelf.example.org")
        with auth_env(monkeypatch, settings, routers=[router]) as api:
            assert api.login().status_code == 200
            answer = api.client.get(
                "/api/v1/config/", headers={"Host": "evil.example"}, follow_redirects=False
            )
            assert answer.status_code == 404
            assert "location" not in {k.lower() for k in answer.headers}


def test_public_url_and_path_map_need_a_session_and_status_flags_the_restart(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    with headless(settings) as env:
        router = build_headless_router(lambda: env.admin, lambda: None)
        with auth_env(monkeypatch, settings, routers=[router]) as api:
            assert api.login().status_code == 200
            capsys.readouterr()
            assert run_cli(monkeypatch, settings, "admin", "keys", "create") == 0
            # a log line of a background thread (the job workers of the app under test) may land in
            # the same capture: the key is the one token of that shape
            key = KEY_SHAPE.search(capsys.readouterr().out).group(0)  # type: ignore[union-attr]
            keyed = {"Authorization": f"Bearer {key}"}
            denied = api.client.patch(
                "/api/v1/config", json={"values": {"public_url": "https://a.example.org"}},
                headers=keyed,
            )  # fmt: skip
            assert denied.status_code == 403 and denied.json()["code"] == "credential_not_allowed"
            ok = api.client.patch(
                "/api/v1/config", json={"values": {"source.check_mode": "piloted"}}, headers=keyed
            )
            assert ok.status_code == 200
        # status: the server started with no public URL; a later `config set` waits for a restart
        record_applied_public_url(env.engine, None)
        env.admin.set("public_url", "https://later.example.org")
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "status", "--json") == 0
        assert "public_url_restart" in capsys.readouterr().out
        record_applied_public_url(env.engine, "https://later.example.org")
        assert run_cli(monkeypatch, settings, "status", "--json") == 0
        assert "public_url_restart" not in capsys.readouterr().out
