"""M4 L25 (ENF-OBS-002): `/metrics`, Prometheus text, opt-in, behind a key of scope `metrics`.

The real application (`create_app()`, real sessions, real keys). What is attacked: the door (off by
default; a session of any role, a key of any other scope, a device token and a metrics key on
another route are all refused) and the content (no title, no user, no path, no address in a name
or a label, whatever the instance holds)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema.coherence import PLAN_STATUSES
from shelfhold.infrastructure.schema.identity import REQUEST_STATES
from shelfhold.infrastructure.schema.platform import JOB_STATUSES
from shelfhold.interfaces.http_api.metrics import require_metrics_key
from shelfhold.interfaces.http_api.problems import ProblemError
from tests.auth_env import AuthEnv, auth_env, run_cli
from tests.visibility_env import HIDDEN_TITLES, VISIBLE_TITLES, add_profile, seed_catalogue

pytestmark = pytest.mark.req("ENF-OBS-002")

KEY = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")
SAMPLE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)$")
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
# The only label names that may ever appear, and the closed list behind each one.
LABEL_VALUES: dict[str, Any] = {
    "state": {*JOB_STATUSES, *PLAN_STATUSES, *REQUEST_STATES},
    "version": re.compile(r"[0-9][0-9A-Za-z.+-]*"),
}
PRIVATE_SOURCE = "secretsrc"
PRIVATE_HOST = "books.example.org"
PRIVATE_ERROR = "could not reach https://books.example.org/series/42"


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def make_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings,
    *extra: str,
) -> str:  # fmt: skip
    capsys.readouterr()
    args = ("admin", "keys", "create", "--name", "prom", *extra)
    assert run_cli(monkeypatch, settings, *args) == 0
    match = KEY.search(capsys.readouterr().out)
    assert match
    return match.group(0)


@pytest.fixture
def on(settings: Settings) -> Settings:
    return settings.model_copy(update={"metrics_enabled": True})


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, on: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, on) as env:
        yield env


def samples(text: str) -> list[tuple[str, dict[str, str], str]]:
    found = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        matched = SAMPLE.match(line)
        assert matched, f"not a Prometheus sample: {line!r}"
        name, labels, value = matched.groups()
        found.append((name, dict(LABEL.findall(labels or "")), value))
    return found


def test_metrics_endpoint_opt_in(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings,
    on: Settings,
) -> None:  # fmt: skip
    """Off unless the host asks (even a good key finds nothing); on, a metrics key reads text."""
    assert settings.metrics_enabled is False
    with auth_env(monkeypatch, settings) as env:
        key = make_key(monkeypatch, capsys, env.settings, "--scope", "metrics")
        assert env.client.get("/metrics", headers=bearer(key)).status_code == 404
        assert env.client.get("/metrics").status_code == 401  # nothing is told to a stranger
    with auth_env(monkeypatch, on, with_admin=False) as env:
        assert env.client.get("/metrics").status_code == 401
        good = env.client.get("/metrics", headers=bearer(key))
        assert good.status_code == 200
        assert good.headers["content-type"].startswith("text/plain")
        assert "version=0.0.4" in good.headers["content-type"]
        assert good.headers["cache-control"] == "no-store"
        names = {name for name, _, _ in samples(good.text)}
        assert {"shelfhold_build_info", "shelfhold_jobs"} <= names
        assert all(name.startswith("shelfhold_") for name in names)
        assert env.client.post("/metrics", headers=bearer(key)).status_code in (403, 405)


def test_metrics_labels_carry_no_title_nor_user(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    """An instance full of titles, profiles, a source, a host and an error text: none of it is in
    the answer, and every label name and value is on the closed lists above."""
    seed = seed_catalogue(api)
    add_profile(api, "kid", "child", "10.0.0.2", max_age_rating=10)
    add_profile(api, "maya-the-member", "member", "10.0.0.3")
    with seed.engine.begin() as connection:
        for status in JOB_STATUSES:
            connection.execute(
                sa.text(
                    "INSERT INTO job_view (id, kind, status, source_key, limit_key, last_error, "
                    "started_at, finished_at, created_at) VALUES (:id, 'scan', :status, :source, "
                    ":host, :error, '2026-11-01 10:00:00', '2026-11-01 10:00:05', "
                    "'2026-11-01 10:00:00')"
                ),
                {"id": new_ulid(), "status": status, "source": PRIVATE_SOURCE, "host": PRIVATE_HOST,
                 "error": PRIVATE_ERROR},
            )  # fmt: skip
        users = [r[0] for r in connection.execute(sa.text("SELECT id FROM user"))]
        names = [r[0] for r in connection.execute(sa.text("SELECT username FROM user"))]
    key = make_key(monkeypatch, capsys, api.settings, "--scope", "metrics")
    response = api.client.get("/metrics", headers=bearer(key))
    assert response.status_code == 200
    body = response.text

    forbidden = [
        *VISIBLE_TITLES.values(), *HIDDEN_TITLES.values(), *seed.works.values(), *users, *names,
        "kid", "maya", PRIVATE_SOURCE, PRIVATE_HOST, PRIVATE_ERROR, "/store", "/library", "/data",
        "http://", "https://", str(api.settings.data_dir),
    ]  # fmt: skip
    leaked = [word for word in forbidden if word and word in body]
    assert not leaked, leaked

    found = samples(body)
    assert found
    for name, labels, value in found:
        assert re.fullmatch(r"shelfhold_[a-z0-9_]+", name), name
        float(value)  # a number, never a text
        for label, label_value in labels.items():
            allowed = LABEL_VALUES.get(label)
            assert allowed is not None, f"label {label!r} is not on the list"
            ok = (
                allowed.fullmatch(label_value)
                if isinstance(allowed, re.Pattern)
                else label_value in allowed
            )
            assert ok, (label, label_value)
    seed.engine.dispose()


def test_metrics_refused_to_sessions_and_keys_without_metrics_scope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    """403 for a child, a member, the admin by session and every key that is not a metrics key
    (`admin` = the one that opens everything, `read`, `request`); a device token too."""
    kid = add_profile(api, "kid", "child", "10.0.0.2", max_age_rating=10)
    member = add_profile(api, "mia", "member", "10.0.0.3")
    assert api.login().status_code == 200
    auth = api.app.state.auth_gate.auth  # type: ignore[attr-defined]
    admin_key = make_key(monkeypatch, capsys, api.settings)  # the default: scope `admin`
    read_key = make_key(monkeypatch, capsys, api.settings, "--scope", "read")
    request_key = auth.create_api_key(
        name="ask", username="mia", scope="request", subject_username="mia"
    ).value
    kid_key = auth.create_api_key(name="kidkey", username="kid", read_only=True).value
    refused = {
        "kid session": kid,
        "member session": member,
        "admin session": api.client,
    }
    for who, client in refused.items():
        response = client.get("/metrics")
        assert response.status_code == 403, who
    for who, key in {
        "admin key": admin_key, "read key": read_key, "request key": request_key,
        "child key": kid_key,
    }.items():  # fmt: skip
        response = api.other_client("10.0.0.9").get("/metrics", headers=bearer(key))
        assert response.status_code == 403, who
        assert "shelfhold_" not in response.text

    token = Principal(
        "u", "admin", "admin", Credential.DEVICE_TOKEN, key_id="d", scopes=("metrics",)
    )
    with pytest.raises(ProblemError) as refusal:
        require_metrics_key(token)
    assert refusal.value.spec.code == "credential_not_allowed"


def test_metrics_key_opens_nothing_else(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    """The key of scope `metrics` is its own audience: every other route, read or write, says
    403 before it runs, though the key belongs to an admin."""
    key = make_key(monkeypatch, capsys, api.settings, "--scope", "metrics")
    other = api.other_client("10.0.0.9")
    for method, path in (
        ("GET", "/api/v1/about"), ("GET", "/api/v1/me"), ("GET", "/api/v1/stats"),
        ("GET", "/api/v1/profiles"), ("GET", "/api/v1/config"), ("GET", "/api/v1/jobs"),
        ("POST", "/api/v1/queue/pause"), ("PATCH", "/api/v1/config"), ("GET", "/healthz/"),
    ):  # fmt: skip
        response = other.request(
            method, path, headers=bearer(key), json={} if method != "GET" else None
        )
        assert response.status_code in (403, 404), (method, path, response.status_code)
        assert response.status_code != 200 or path.startswith("/healthz")
    assert other.get("/api/v1/about", headers=bearer(key)).status_code == 403
    assert other.get("/metrics", headers=bearer(key)).status_code == 200


def test_metrics_scope_is_the_admin_own_and_for_herself(
    monkeypatch: pytest.MonkeyPatch, api: AuthEnv
) -> None:
    """Who may hold one: an admin, for herself. A member, a child, or a key made for another."""
    add_profile(api, "kid", "child", "10.0.0.2", max_age_rating=10)
    add_profile(api, "mia", "member", "10.0.0.3")
    auth = api.app.state.auth_gate.auth  # type: ignore[attr-defined]
    for who in ("kid", "mia"):
        with pytest.raises(ValueError, match="scope_requires_admin"):
            auth.create_api_key(name="x", username=who, scope="metrics")
    with pytest.raises(ValueError, match="subject_not_allowed"):
        auth.create_api_key(name="x", scope="metrics", subject_username="mia")
    made = auth.create_api_key(name="ok", scope="metrics")
    [(scopes, read_only)] = [
        (r.scopes, r.read_only) for r in auth.api_keys() if r.prefix == made.prefix
    ]
    assert scopes == ("metrics",)
    assert read_only is True


def test_a_metrics_key_reaches_one_route_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    """Every route of the application (the ones left out of the OpenAPI document and the SPA
    included), every verb: 403 for the key of scope `metrics`, but for `GET /metrics`."""
    from tests.integration.test_child_surfaces import _fill, _routes

    key = make_key(monkeypatch, capsys, api.settings, "--scope", "metrics")
    other = api.other_client("10.0.0.9")
    reached, routes = [], sorted(_routes(api.app))
    assert "GET /metrics" in routes and len(routes) > 50
    for route in routes:
        method, path = _fill(route)
        if method in ("MOUNT", "WEBSOCKET"):
            continue
        response = other.request(
            method, path, headers=bearer(key), json={} if method != "GET" else None
        )
        if response.status_code != 403:
            reached.append((route, response.status_code))
    assert reached == [("GET /metrics", 200)]
