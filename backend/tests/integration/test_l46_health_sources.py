"""M3 L46: the diagnostic bundle over HTTP and the library type gaps of the Health page
(ENF-OBS-005, EF-EXP-036, EF-INT-025). The bundle is for the administrator alone, an attachment,
built in a temporary folder, and carries no secret, no host and no path."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.ops import diagnostic
from shelfhold.ops.health import build_health_page
from shelfhold.ops.ports import WaitingItem
from tests.auth_env import PASSWORD, AuthEnv, auth_env
from tests.health_env import HEALTH_PATH, HOST_CANARY, NOW, add_profile, engine_of, seed_sources
from tests.integration.test_health_page import _key_of

BUNDLE_PATH = "/api/v1/diagnostic/bundle"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


@pytest.mark.req("ENF-OBS-005")
def test_diagnostic_bundle_http_download_is_attachment_admin_only(api: AuthEnv) -> None:
    assert api.login().status_code == 200
    response = api.client.get(BUNDLE_PATH)
    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "diagnostic-bundle.zip" in response.headers["content-disposition"]
    assert response.headers["content-type"].startswith("application/zip")
    assert response.headers["cache-control"] == "no-store"
    assert zipfile.is_zipfile(io.BytesIO(response.content))


@pytest.mark.req("ENF-OBS-005")
@pytest.mark.parametrize(("role", "ceiling"), [("member", None), ("child", 12)])
def test_diagnostic_bundle_refused_to_member_child_and_their_keys(
    api: AuthEnv, role: str, ceiling: int | None
) -> None:
    add_profile(api, role, role, ceiling)
    session = api.other_client("10.0.0.40")
    assert api.login(role, PASSWORD, client=session).status_code == 200
    denied = [
        session.get(BUNDLE_PATH),
        api.other_client("10.0.0.41").get(
            BUNDLE_PATH, headers={"Authorization": f"Bearer {_key_of(api, role)}"}
        ),
        api.other_client("10.0.0.42").get(BUNDLE_PATH),  # no session at all
    ]
    for response in denied:
        assert response.status_code in (401, 403)
        assert "zip" not in response.headers.get("content-type", "")
        assert "attachment" not in response.headers.get("content-disposition", "")
        assert not response.content.startswith(b"PK")


@pytest.mark.req("ENF-OBS-005")
def test_diagnostic_bundle_is_get_only(api: AuthEnv) -> None:
    assert api.login().status_code == 200
    for verb in ("post", "put", "patch", "delete"):
        response = getattr(api.client, verb)(BUNDLE_PATH)
        assert response.status_code in (403, 405), verb
        assert not response.content.startswith(b"PK")


@pytest.mark.req("ENF-OBS-005")
def test_diagnostic_bundle_carries_no_secret_host_or_path(api: AuthEnv) -> None:
    """The bundle served over HTTP is the one of the CLI: no source URL, no password hash, no
    real path of the instance."""
    seed_sources(api.settings)
    engine = engine_of(api.settings)
    try:
        with engine.connect() as connection:
            (password_hash,) = connection.execute(
                sa.text("SELECT password_hash FROM user WHERE role = 'admin'")
            ).one()
    finally:
        engine.dispose()
    assert api.login().status_code == 200
    response = api.client.get(BUNDLE_PATH)
    assert response.status_code == 200
    forbidden = [
        HOST_CANARY,
        PASSWORD,
        password_hash,
        str(api.settings.database_path),
        str(api.settings.library_dir),
        str(api.settings.store_dir),
        str(Path(api.settings.database_path).parent),
    ]
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        for name in archive.namelist():
            body = archive.read(name).decode("utf-8", errors="replace")
            for value in forbidden:
                assert value not in body, (name, value[:6])


@pytest.mark.req("ENF-OBS-005")
def test_diagnostic_bundle_refusal_of_the_builder_leaks_nothing(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle the builder refuses (a value that must not leave) is an error with nothing of the
    refused section or of the value in it, and no partial file is served."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise diagnostic.DiagnosticLeakError("secret", "config.json")

    monkeypatch.setattr("shelfhold.interfaces.http_api.health.build_bundle", refuse)
    client = TestClient(
        api.app,  # type: ignore[arg-type]
        base_url="https://testserver",
        client=("10.0.0.44", 40000),
        raise_server_exceptions=False,
    )
    assert api.login(client=client).status_code == 200
    response = client.get(BUNDLE_PATH)
    assert response.status_code >= 400
    assert "config.json" not in response.text
    assert not response.content.startswith(b"PK")


@pytest.mark.req("EF-EXP-036", "EF-INT-025")
def test_health_page_lists_library_type_changes_with_a_reconnect_action(api: AuthEnv) -> None:
    waiting = [
        WaitingItem("library_type_changed", "export", "01TG", "a reason with /srv/kavita/lib",
                    "shelfhold target reconnect 01TG"),
        WaitingItem("child_exposure", "export", "01TH", "reason", "shelfhold target recheck 01TH"),
    ]  # fmt: skip
    engine = engine_of(api.settings)
    try:
        page = build_health_page(engine, api.settings, NOW, waiting=waiting)
    finally:
        engine.dispose()
    changes = page["library_type_changes"]
    assert [c["code"] for c in changes] == ["library_type_changed"]
    assert changes[0]["action"]["command"] == "shelfhold target reconnect 01TG"
    assert "/srv/kavita" not in str(page)  # the reason of the wait (it may hold a name) stays out
    assert [a["code"] for a in page["kavita_audit"]] == ["child_exposure"]  # not mixed


@pytest.mark.req("EF-EXP-036", "ENF-OBS-005")
def test_health_page_exposes_library_type_changes_to_the_admin_alone(api: AuthEnv) -> None:
    assert api.login().status_code == 200
    assert api.client.get(HEALTH_PATH).json()["library_type_changes"] == []
    add_profile(api, "kid", "child", 12)
    session = api.other_client("10.0.0.43")
    assert api.login("kid", PASSWORD, client=session).status_code == 200
    denied = session.get(HEALTH_PATH)
    assert denied.status_code == 403
    assert "library_type_changes" not in denied.text
