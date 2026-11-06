"""L05: local admin, server sessions, CSRF, attempt limiting, neutral login page (EF-USR-021)."""

import base64
import hashlib
import re
from datetime import timedelta

import pytest
from argon2 import PasswordHasher

from shelfhold import __version__
from shelfhold.config import Settings, load_settings
from shelfhold.infrastructure.migrate import migrate
from tests.auth_env import (
    ADMIN,
    CSRF_COOKIE,
    CSRF_HEADER,
    PASSWORD,
    SESSION_COOKIE,
    WRONG,
    auth_env,
    create_admin,
    db_dump,
    db_rows,
    run_cli,
)

DEFAULT_SOURCE = f"https://github.com/Themlaw/shelfhold/tree/v{__version__}"


def _set_cookie_headers(response: object) -> list[str]:
    return response.headers.get_list("set-cookie")  # type: ignore[attr-defined]


def _cookie_line(response: object, name: str) -> str:
    lines = [c for c in _set_cookie_headers(response) if c.startswith(f"{name}=")]
    assert lines, f"{name} not set"
    return lines[-1]


# --- CLI, argon2id -----------------------------------------------------------------------------


@pytest.mark.req("EF-USR-021", "EF-USR-001", "ENF-SEC-008")
def test_admin_created_by_cli_password_argon2id(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    migrate(settings)
    assert create_admin(monkeypatch, settings) == 0
    [(role, can_rate, digest)] = db_rows(
        settings, 'SELECT role, can_rate, password_hash FROM "user" WHERE username = ?', ADMIN
    )
    assert role == "admin"
    assert can_rate == 1
    assert isinstance(digest, str)
    assert digest.startswith("$argon2id$")
    params = dict(p.split("=") for p in digest.split("$")[3].split(","))
    assert int(params["m"]) >= 19 * 1024  # OWASP floor for argon2id
    assert int(params["t"]) >= 2
    assert PasswordHasher().verify(digest, PASSWORD)
    assert PASSWORD not in db_dump(settings)
    # One local admin in M1a; a second `create` is refused, the first one is untouched.
    assert create_admin(monkeypatch, settings, username="other") != 0
    assert db_rows(settings, 'SELECT count(*) FROM "user"') == [(1,)]


@pytest.mark.req("EF-USR-021")
def test_admin_password_never_taken_from_the_command_line(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    migrate(settings)
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, settings, "admin", "create", "--username", "a", "--password", "x")
    # stdin is accepted (`--password-file -`), and a too short password is refused.
    assert (
        run_cli(
            monkeypatch, settings, "admin", "create", "--username", "a", "--password-file", "-",
            stdin="short\n",
        )
        != 0
    )  # fmt: skip
    assert db_rows(settings, 'SELECT count(*) FROM "user"') == [(0,)]


# --- Cookies, CSRF -----------------------------------------------------------------------------


@pytest.mark.req("EF-USR-021", "ENF-SEC-008")
def test_session_cookie_httponly_secure_samesite_strict(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as env:
        response = env.login()
        assert response.status_code == 200
        assert "no-store" in response.headers["cache-control"]
        session = _cookie_line(response, SESSION_COOKIE).lower()
        for attribute in ("httponly", "secure", "samesite=strict", "path=/"):
            assert attribute in session
        assert "domain=" not in session  # `__Host-` forbids it
        assert "max-age=2592000" in session  # 30 days, "remember me" ticked by default
        csrf = _cookie_line(response, CSRF_COOKIE).lower()
        assert "secure" in csrf
        assert "samesite=strict" in csrf
        assert "httponly" not in csrf  # the page reads it to send the header
        assert response.json()["csrf_token"] == env.csrf()


@pytest.mark.req("EF-USR-021", "ENF-SEC-008")
def test_session_cookie_without_secure_prefix_on_plain_http(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """On `http://` (LAN address, no TLS) a `Secure` cookie would never come back: plain name."""
    with auth_env(monkeypatch, settings, base_url="http://testserver") as env:
        response = env.login()
        assert response.status_code == 200
        line = _cookie_line(response, "sid").lower()
        assert "httponly" in line
        assert "samesite=strict" in line
        assert "secure" not in line
        assert not any(c.startswith("__Host-") for c in _set_cookie_headers(response))
        assert env.client.get("/api/v1/auth/session").status_code == 200


@pytest.mark.req("EF-USR-021", "ENF-SEC-008")
def test_post_without_csrf_token_403(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    with auth_env(monkeypatch, settings) as env:
        env.login()
        missing = env.client.post("/api/v1/auth/logout")
        assert missing.status_code == 403
        assert missing.json()["code"] == "csrf_failed"
        wrong = env.client.post("/api/v1/auth/logout", headers={CSRF_HEADER: "x" * 43})
        assert wrong.status_code == 403
        assert env.client.get("/api/v1/auth/session").status_code == 200  # still logged in
        assert env.post("/api/v1/auth/logout").status_code == 204
        assert env.client.get("/api/v1/auth/session").status_code == 401


@pytest.mark.req("EF-USR-021", "ENF-SEC-008")
def test_login_refuses_cross_site_and_form_posts(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """Login CSRF: a cross-site form cannot send JSON without a preflight, and CORS is closed."""
    with auth_env(monkeypatch, settings) as env:
        form = env.client.post("/api/v1/auth/login", data={"username": ADMIN, "password": PASSWORD})
        assert form.status_code in {403, 415}
        cross = env.client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN, "password": PASSWORD},
            headers={"Sec-Fetch-Site": "cross-site"},
        )
        assert cross.status_code == 403
        assert SESSION_COOKIE not in env.client.cookies


# --- Attempts, enumeration ---------------------------------------------------------------------


@pytest.mark.req("EF-USR-021", "ENF-SEC-008")
def test_login_attempts_rate_limited(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    with auth_env(monkeypatch, settings) as env:
        for _ in range(10):
            assert env.login(password=WRONG).status_code == 401
        limited = env.login(password=WRONG)
        assert limited.status_code == 429
        assert limited.json()["code"] == "too_many_attempts"
        assert 1 <= int(limited.headers["retry-after"]) <= 60
        # The right password does not get through while throttled (no oracle).
        assert env.login().status_code == 429
        env.clock.advance(timedelta(seconds=61))
        assert env.login().status_code == 200


@pytest.mark.req("ENF-SEC-008")
def test_throttled_address_never_locks_the_account_for_another_address(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """Backoff, not a hard lock: someone else's failures do not keep the owner out."""
    with auth_env(monkeypatch, settings) as env:
        attacker = env.other_client("203.0.113.7")
        for _ in range(11):
            env.login(password=WRONG, client=attacker)
        assert env.login(password=WRONG, client=attacker).status_code == 429
        owner = env.other_client("198.51.100.20")
        assert env.login(client=owner).status_code == 200


@pytest.mark.req("ENF-SEC-008")
def test_account_backoff_is_temporary(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    with auth_env(monkeypatch, settings) as env:
        for n in range(40):  # spread over many addresses: the per-account backoff kicks in
            env.login(password=WRONG, client=env.other_client(f"192.0.2.{n + 1}"))
        fresh = env.other_client("198.51.100.30")
        throttled = env.login(client=fresh)
        assert throttled.status_code == 429
        assert int(throttled.headers["retry-after"]) <= 60
        env.clock.advance(timedelta(seconds=61))
        assert env.login(client=fresh).status_code == 200


@pytest.mark.req("ENF-SEC-008")
def test_unknown_user_and_wrong_password_same_answer(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """No enumeration: same status and body, and an argon2 verification in both cases."""
    verified: list[str] = []
    original = PasswordHasher.verify

    def counting(self: PasswordHasher, digest: str | bytes, password: str | bytes) -> bool:
        verified.append("x")
        return original(self, digest, password)

    monkeypatch.setattr(PasswordHasher, "verify", counting)
    with auth_env(monkeypatch, settings) as env:
        verified.clear()
        unknown = env.login(username="nobody-here", password=WRONG)
        assert len(verified) == 1
        wrong = env.login(password=WRONG)
        assert len(verified) == 2

    def _shape(response: object) -> tuple[int, dict[str, object]]:
        body = dict(response.json())  # type: ignore[attr-defined]
        body.pop("request_id", None)
        return response.status_code, body  # type: ignore[attr-defined]

    assert _shape(unknown) == _shape(wrong)
    assert unknown.json()["code"] == "invalid_credentials"


# --- Sessions ----------------------------------------------------------------------------------


@pytest.mark.req("EF-USR-022", "ENF-SEC-008")
def test_session_token_stored_hashed(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    with auth_env(monkeypatch, settings) as env:
        env.login()
        token = env.client.cookies[SESSION_COOKIE]
        dump = db_dump(settings)
        assert token not in dump
        [(stored,)] = db_rows(settings, "SELECT token_hash FROM session WHERE revoked_at IS NULL")
        assert stored == "sha256:" + hashlib.sha256(token.encode()).hexdigest()


@pytest.mark.req("EF-USR-022", "ENF-SEC-008")
def test_session_id_renewed_on_login(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    with auth_env(monkeypatch, settings) as env:
        # A planted value is never adopted (fixation).
        planted = env.other_client("198.51.100.41").post(
            "/api/v1/auth/login",
            json={"username": ADMIN, "password": PASSWORD},
            headers={"Cookie": f"{SESSION_COOKIE}=planted-by-someone-else"},
        )
        issued = _cookie_line(planted, SESSION_COOKIE).split(";")[0].split("=", 1)[1]
        assert issued != "planted-by-someone-else"
        env.login()
        first = env.client.cookies[SESSION_COOKIE]
        env.login()
        second = env.client.cookies[SESSION_COOKIE]
        assert second != first
        # The session presented at login is closed: its token no longer opens anything.
        other = env.other_client("198.51.100.40")
        other.cookies.set(SESSION_COOKIE, first, domain="testserver")
        assert other.get("/api/v1/auth/session").status_code == 401


@pytest.mark.req("EF-USR-022")
def test_session_default_30_days_sliding(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as env:
        env.login()
        env.clock.advance(timedelta(days=29))
        touched = env.client.get("/api/v1/auth/session")
        assert touched.status_code == 200
        assert "max-age=2592000" in _cookie_line(touched, SESSION_COOKIE).lower()  # slid
        env.clock.advance(timedelta(days=29))  # 58 days after login, 29 after the last request
        assert env.client.get("/api/v1/auth/session").status_code == 200
        env.clock.advance(timedelta(days=31))
        assert env.client.get("/api/v1/auth/session").status_code == 401


@pytest.mark.req("EF-USR-022")
def test_session_without_remember_me_is_a_browser_session(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as env:
        response = env.login(remember=False)
        assert "max-age" not in _cookie_line(response, SESSION_COOKIE).lower()
        assert db_rows(settings, "SELECT remember FROM session") == [(0,)]


@pytest.mark.req("EF-USR-022", "ENF-SEC-008")
@pytest.mark.parametrize("login", [{"remember": False}, {"confirm_remember": True}])
def test_hardened_profile_admin_session_idle(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, login: dict[str, bool]
) -> None:
    with auth_env(monkeypatch, settings, hardened=True) as env:
        opened = env.login(**login)
        if login.get("confirm_remember"):  # the confirmed session is a persistent one
            assert "max-age=1800" in _cookie_line(opened, SESSION_COOKIE).lower()
        env.clock.advance(timedelta(minutes=29))
        assert env.client.get("/api/v1/auth/session").status_code == 200
        env.clock.advance(timedelta(minutes=31))
        assert env.client.get("/api/v1/auth/session").status_code == 401
        # 12 hours absolute, however active the session is.
        env.login(**login)
        for _ in range(12 * 3):
            env.clock.advance(timedelta(minutes=20))
            last = env.client.get("/api/v1/auth/session")
        assert last.status_code == 401


@pytest.mark.req("EF-USR-021")
def test_cli_revoked_session_redirects_to_login(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as env:
        env.login()
        assert env.client.get("/api/v1/auth/session").status_code == 200
        assert run_cli(monkeypatch, settings, "admin", "sessions", "revoke", "--all") == 0
        api = env.client.get("/api/v1/auth/session")
        assert api.status_code == 401
        assert api.json()["login_url"] == "/login"
        page = env.client.get(
            "/api/v1/about", headers={"Accept": "text/html"}, follow_redirects=False
        )
        assert page.status_code == 303
        assert page.headers["location"] == "/login"
        [(revoked,)] = db_rows(settings, "SELECT count(*) FROM session WHERE revoked_at IS NULL")
        assert revoked == 0


# --- Pages, about, OpenAPI ---------------------------------------------------------------------


def _inline_hashes(html: str, tag: str) -> set[str]:
    bodies = re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", html, flags=re.DOTALL)
    return {
        "'sha256-" + base64.b64encode(hashlib.sha256(b.encode()).digest()).decode() + "'"
        for b in bodies
    }


@pytest.mark.req("ENF-SEC-004", "EF-USR-021")
def test_served_pages_have_hash_csp_no_unsafe_inline(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as env:
        page = env.client.get("/login")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    csp = page.headers["content-security-policy"]
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    directives = dict(d.strip().split(" ", 1) for d in csp.split(";") if " " in d.strip())
    assert directives["default-src"] == "'none'"
    assert directives["frame-ancestors"] == "'none'"
    for tag, directive in (("script", "script-src"), ("style", "style-src")):
        hashes = _inline_hashes(page.text, tag)
        assert hashes, f"no inline {tag}"
        assert hashes == set(directives[directive].split())
    assert " on" not in re.sub(r"<script.*?</script>", "", page.text, flags=re.DOTALL).lower()


@pytest.mark.req("ENF-PRIV-002")
def test_login_page_neutral_no_og_tags(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    with auth_env(monkeypatch, settings) as env:
        html = env.client.get("/login").text
    lowered = html.lower()
    assert 'property="og:' not in lowered
    assert 'name="twitter:' not in lowered
    assert re.search(r"<title>connexion</title>", lowered)
    without_links = re.sub(r'href="[^"]*"', "", lowered)
    assert "shelfhold" not in without_links  # no tool name in tags, title or text
    assert "kavita" not in without_links
    assert "<img" not in lowered


@pytest.mark.req("ENF-LEG-012")
def test_login_page_has_source_link(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    with auth_env(monkeypatch, settings, with_admin=False) as env:
        assert f'href="{DEFAULT_SOURCE}"' in env.client.get("/login").text
    custom = settings.model_copy(update={"source_url": "https://code.example.org/fork?a=1&b=2"})
    with auth_env(monkeypatch, custom, with_admin=False) as env:
        assert 'href="https://code.example.org/fork?a=1&amp;b=2"' in env.client.get("/login").text


@pytest.mark.req("ENF-LEG-012")
def test_about_exposes_source_url(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    with auth_env(monkeypatch, settings) as env:
        assert env.client.get("/api/v1/about").status_code == 401
        env.login()
        about = env.client.get("/api/v1/about")
    assert about.status_code == 200
    assert about.json() == {"version": __version__, "source_url": DEFAULT_SOURCE}


@pytest.mark.req("ENF-PRIV-002")
def test_openapi_json_requires_auth(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    with auth_env(monkeypatch, settings) as env:
        anonymous = env.client.get("/api/v1/openapi.json")
        assert anonymous.status_code == 401
        assert __version__ not in anonymous.text
        assert env.client.get("/openapi.json").status_code == 404
        env.login()
        schema = env.client.get("/api/v1/openapi.json")
    assert schema.status_code == 200
    assert schema.json()["info"]["version"] == __version__
    assert "/api/v1/auth/login" in schema.json()["paths"]


@pytest.mark.req("ENF-PRIV-002", "ENF-OPS-003")
def test_healthz_exposes_paused_queues(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """A7: anonymous callers get the state only; a session also gets version and paused queues."""
    with auth_env(monkeypatch, settings) as env:
        import sqlite3

        connection = sqlite3.connect(settings.database_path)
        connection.execute(
            "INSERT INTO job_view (id, kind, status) VALUES (?, 'export.scan', 'paused')",
            ("01JC0000000000000000000000",),
        )
        connection.commit()
        connection.close()
        anonymous = env.client.get("/healthz")
        assert anonymous.json() == {"state": "ok"}
        env.login()
        full = env.client.get("/healthz").json()
        ready = env.client.get("/readyz").json()
    assert full["state"] == "ok"
    assert full["version"] == __version__
    assert full["paused_queues"] == ["export.scan"]
    assert set(ready["checks"]) >= {"database_writable", "migrations_applied"}


# --- Profiles ----------------------------------------------------------------------------------

_INVISIBLE = re.compile(r"csrf|argon|throttl|rate_limit|attempt|cookie|samesite|secure|csp|hash")


@pytest.mark.req("ENF-SEC-008")
def test_invisible_protections_not_disableable(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, env: dict[str, str]
) -> None:
    assert not [name for name in Settings.model_fields if _INVISIBLE.search(name)]
    attempts = {
        "SHELFHOLD_CSRF": "false",
        "SHELFHOLD_DISABLE_CSRF": "1",
        "SHELFHOLD_LOGIN_RATE_LIMIT": "0",
        "SHELFHOLD_COOKIE_SECURE": "false",
        "SHELFHOLD_ARGON2_MEMORY_COST": "8",
    }
    assert load_settings({**env, **attempts}).values == load_settings(env).values
    for name, value in attempts.items():
        monkeypatch.setenv(name, value)
    with auth_env(monkeypatch, settings) as auth:
        auth.login()
        assert auth.client.post("/api/v1/auth/logout").status_code == 403
        for _ in range(10):
            auth.login(password=WRONG)
        assert auth.login().status_code == 429


@pytest.mark.req("ENF-SEC-008")
def test_hardened_profile_never_required_at_startup(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    assert Settings().auth_hardened is False
    with auth_env(monkeypatch, settings) as env:
        assert env.client.get("/readyz").status_code == 200
        assert env.login().status_code == 200
        session = env.client.get("/api/v1/auth/session").json()
    assert session["username"] == ADMIN
    assert session["credential"] == "session"
