"""`GET /api/v1/health/page` (M3 L15): the Health page, an operating surface for the administrator
alone. Read from the database and the last measures: no disk is opened, nothing is sent, and no
secret, passphrase or real path is ever part of an answer.
"""

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from shelfhold.config import Settings
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.schema import acquisition
from shelfhold.ops.health import build_health_page
from shelfhold.ops.ports import WaitingItem
from tests.auth_env import PASSWORD, AuthEnv, auth_env
from tests.health_env import (
    HEALTH_PATH,
    HOST_CANARY,
    NOW,
    add_profile,
    cli_exists,
    engine_of,
    seed_lost_original,
    seed_requests,
    seed_sources,
    seed_units,
)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


def _key_of(api: AuthEnv, username: str) -> str:
    engine = create_db_engine(api.settings.database_path)
    try:
        return local_auth(engine, api.settings).create_api_key(
            name="k", username=username, read_only=True
        ).value
    finally:
        engine.dispose()


def _page(api: AuthEnv) -> dict[str, object]:
    assert api.login().status_code == 200
    response = api.client.get(HEALTH_PATH)
    assert response.status_code == 200, response.text
    return dict(response.json())


# -- access: the administrator alone ---------------------------------------------------------------


@pytest.mark.req("ENF-OBS-005")
@pytest.mark.parametrize(("role", "ceiling"), [("member", None), ("child", 12)])
def test_health_page_refused_to_member_and_child(
    api: AuthEnv, role: str, ceiling: int | None
) -> None:
    """A session and a key of a member or of a child: 403, before anything is read, and nothing
    of the page (no source name, no URL, no path) is in the refusal."""
    seed_sources(api.settings)
    add_profile(api, role, role, ceiling)
    session = api.other_client("10.0.0.20")
    assert api.login(role, PASSWORD, client=session).status_code == 200
    denied = [
        session.get(HEALTH_PATH),
        api.other_client("10.0.0.21").get(
            HEALTH_PATH, headers={"Authorization": f"Bearer {_key_of(api, role)}"}
        ),
    ]
    for response in denied:
        assert response.status_code == 403
        for secret in ("down-source", "expired-source", HOST_CANARY, "unavailable", "sources"):
            assert secret not in response.text


@pytest.mark.req("ENF-OBS-005")
def test_health_page_refused_without_a_session(api: AuthEnv) -> None:
    seed_sources(api.settings)
    response = api.other_client("10.0.0.22").get(HEALTH_PATH)
    assert response.status_code in (401, 403)
    assert "down-source" not in response.text


@pytest.mark.req("ENF-OBS-005")
def test_health_page_admin_session_and_key_read_the_same_page(api: AuthEnv) -> None:
    seed_sources(api.settings)
    body = _page(api)
    keyed = api.other_client("10.0.0.23").get(
        HEALTH_PATH, headers={"Authorization": f"Bearer {_key_of(api, 'admin')}"}
    )
    assert keyed.status_code == 200
    assert {s["plugin_id"] for s in keyed.json()["sources"]} == {
        s["plugin_id"] for s in body["sources"]  # type: ignore[attr-defined]
    }


@pytest.mark.req("ENF-OBS-005")
def test_health_page_is_get_only(api: AuthEnv) -> None:
    assert api.login().status_code == 200
    for verb in ("post", "put", "patch", "delete"):
        response = getattr(api.client, verb)(HEALTH_PATH, headers={"X-CSRF": api.csrf()})
        assert response.status_code in (403, 405), verb


# -- sources ----------------------------------------------------------------------------------------


@pytest.mark.req("EF-SRC-014")
def test_source_degraded_after_three_failures_with_action(api: AuthEnv) -> None:
    seed_sources(api.settings)
    sources = {s["plugin_id"]: s for s in _page(api)["sources"]}  # type: ignore[attr-defined]
    assert sources["good-source"]["state"] == "ok"  # one failure is a blip, not a verdict
    assert sources["good-source"]["code"] is None
    down = sources["down-source"]
    assert (down["state"], down["code"]) == ("degraded", "source.broken")
    assert down["action"]["command"] == "shelfhold source recheck"
    expired = sources["expired-source"]
    assert (expired["state"], expired["code"]) == ("paused", "source.auth_expired")
    assert expired["action"]["command"].startswith("shelfhold source set-secret")
    for source in sources.values():
        if source["state"] != "ok":
            assert cli_exists(source["action"]["command"]), source


@pytest.mark.req("EF-SRC-014")
def test_source_two_failures_are_not_degraded_three_are(api: AuthEnv) -> None:
    engine = engine_of(api.settings)
    with engine.begin() as connection:
        for plugin, outage in (("two", 2), ("three", 3)):
            connection.execute(
                acquisition.source_config.insert().values(
                    plugin_id=plugin, health={"outage": outage}
                )
            )
    engine.dispose()
    sources = {s["plugin_id"]: s["state"] for s in _page(api)["sources"]}  # type: ignore[attr-defined]
    assert sources == {"two": "ok", "three": "degraded"}


@pytest.mark.req("EF-SRC-014")
def test_health_page_never_carries_a_source_url_or_a_secret(api: AuthEnv) -> None:
    seed_sources(api.settings)
    engine = engine_of(api.settings)
    with engine.begin() as connection:
        connection.execute(
            acquisition.source_config.update()
            .where(acquisition.source_config.c.plugin_id == "expired-source")
            .values(user_agent=f"agent {HOST_CANARY}", terms_url=f"https://{HOST_CANARY}/terms")
        )
    engine.dispose()
    assert api.login().status_code == 200
    text = api.client.get(HEALTH_PATH).text
    assert HOST_CANARY not in text
    assert "http" not in text.replace("https://testserver", "")


# -- irreplaceable, lost originals, backup ----------------------------------------------------------


@pytest.mark.req("EF-BIB-031")
def test_health_page_counts_irreplaceable_units(api: AuthEnv) -> None:
    seed_units(api.settings)
    irreplaceable = _page(api)["irreplaceable"]
    assert irreplaceable["count"] == 3  # type: ignore[index]  # bare, single, kept
    assert irreplaceable["without_kept_copy"] == 1  # type: ignore[index]
    assert irreplaceable["single_copy"] == 1  # type: ignore[index]
    assert irreplaceable["store_backup"] == {"declared": False, "age_days": None}  # type: ignore[index]


@pytest.mark.req("EF-BIB-031")
def test_health_page_store_backup_age_is_the_declared_one(api: AuthEnv) -> None:
    connection = sqlite3.connect(api.settings.database_path)
    try:
        connection.execute(
            "INSERT INTO app_setting (key, value) VALUES ('store.backup_declared_at', ?)",
            (datetime.now(UTC).replace(microsecond=0).isoformat(),),
        )
        connection.commit()
    finally:
        connection.close()
    backup = _page(api)["irreplaceable"]["store_backup"]  # type: ignore[index]
    assert backup["declared"] is True
    assert backup["age_days"] == 0


@pytest.mark.req("EF-BIB-051")
def test_health_lists_lost_original_with_derivative(api: AuthEnv) -> None:
    unit, content = seed_lost_original(api.settings)
    lost = _page(api)["lost_originals"]
    assert [(i["unit_id"], i["content_id"]) for i in lost] == [(unit, content)]  # type: ignore[attr-defined]
    assert lost[0]["code"] == "content.original_lost"  # type: ignore[index]
    assert lost[0]["derivative_present"] is True  # type: ignore[index]
    assert cli_exists(lost[0]["action"]["command"])  # type: ignore[index]


@pytest.mark.req("EF-INT-015")
def test_backup_passphrase_in_data_warns(
    api: AuthEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The warning says the passphrase is in `/data`; it never says what it is, nor where."""
    phrase = "correct-horse-canary-phrase"
    file = api.settings.data_dir / "backup-phrase.txt"
    file.write_text(phrase)
    destination = tmp_path / "elsewhere"
    destination.mkdir()
    settings = api.settings.model_copy(
        update={"backup_dir": destination, "backup_passphrase_file": file}
    )
    connection = sqlite3.connect(api.settings.database_path)
    try:
        connection.execute(
            "INSERT INTO app_setting (key, value) VALUES ('backup.colocated_passphrase', ?)",
            ("2026-11-10",),
        )
        connection.commit()
    finally:
        connection.close()
    engine = engine_of(api.settings)
    try:
        page = build_health_page(engine, settings, NOW)
    finally:
        engine.dispose()
    warnings = {w["code"]: w for w in page["backup"]["warnings"]}  # type: ignore[index]
    assert warnings["backup_passphrase_colocated"]["action"]["command"].startswith(
        "shelfhold backup run"
    )
    text = json.dumps(page)
    for leaked in (phrase, str(file), str(api.settings.data_dir), str(destination), "backup-phrase"):
        assert leaked not in text
    file.unlink()  # the page does not read it: the warning is the database's, and stays
    engine = engine_of(api.settings)
    try:
        again = build_health_page(engine, settings, NOW)
    finally:
        engine.dispose()
    assert "backup_passphrase_colocated" in {w["code"] for w in again["backup"]["warnings"]}  # type: ignore[index]


@pytest.mark.req("EF-INT-015")
def test_backup_not_configured_is_an_entry_with_an_action(api: AuthEnv) -> None:
    warnings = _page(api)["backup"]["warnings"]  # type: ignore[index]
    assert {w["code"] for w in warnings} == {"backup_not_configured"}
    assert cli_exists(warnings[0]["action"]["command"])


# -- what waits: withdrawal, Kavita audit, jurisdiction ---------------------------------------------


@pytest.mark.req("EF-BIB-031")
def test_health_page_lists_withdrawal_pending_without_relaunching_it(api: AuthEnv) -> None:
    """D24: `withdrawal_pending` is an entry of the page, from the same list as `status`; the page
    applies nothing (the action is reserved, `shelf withdraw`)."""
    engine = engine_of(api.settings)
    waiting = [
        WaitingItem("withdrawal_pending", "export", "01ED", "adult: 1 file(s)",
                    "shelfhold shelf withdraw", True),
        WaitingItem("child_exposure", "export", "01TG", "reason", "shelfhold target recheck 01TG"),
        WaitingItem("dead_letter", "jobs.x", "01JB", "reason", "shelfhold jobs retry 01JB"),
    ]  # fmt: skip
    try:
        page = build_health_page(engine, api.settings, NOW, waiting=waiting)
    finally:
        engine.dispose()
    pending = page["withdrawal_pending"]  # type: ignore[index]
    assert pending["count"] == 1
    assert pending["action"] == {"command": "shelfhold shelf withdraw", "reserved": True}
    assert [a["code"] for a in page["kavita_audit"]] == ["child_exposure"]  # type: ignore[index]
    assert all(cli_exists(a["action"]["command"]) for a in page["kavita_audit"])  # type: ignore[index]


@pytest.mark.req("EF-BIB-031")
def test_health_page_reads_and_never_writes(api: AuthEnv) -> None:
    """Reading the page twice changes no row of any table: nothing is applied, announced or
    counted by being looked at (`source.broken` is the follow cycle's, not the page's)."""
    seed_units(api.settings)
    seed_sources(api.settings)

    def snapshot() -> list[tuple[str, int]]:
        connection = sqlite3.connect(api.settings.database_path)
        try:
            tables = [r[0] for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )]
            return [(t, connection.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0])
                    for t in sorted(tables)]  # noqa: S608
        finally:
            connection.close()

    assert api.login().status_code == 200
    before = snapshot()
    for _ in range(2):
        assert api.client.get(HEALTH_PATH).status_code == 200
    assert snapshot() == before


@pytest.mark.req("EF-SRC-014")
def test_health_page_reads_the_database_only(api: AuthEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """ENF-OPS-014: the page opens no file and walks no folder (a library disk asleep stays so)."""
    import builtins
    import os

    seed_units(api.settings)
    seed_sources(api.settings)
    assert api.login().status_code == 200
    guarded = str(api.settings.data_dir.parent)
    touched: list[str] = []
    real_open, real_scandir, real_stat = builtins.open, os.scandir, os.stat

    def spy(real):  # type: ignore[no-untyped-def]
        def wrapper(path, *args, **kwargs):  # type: ignore[no-untyped-def]
            if isinstance(path, str | Path) and str(path).startswith(guarded):
                if not str(path).endswith((".db", ".db-wal", ".db-shm", ".db-journal")):
                    touched.append(str(path))
            return real(path, *args, **kwargs)

        return wrapper

    monkeypatch.setattr(builtins, "open", spy(real_open))
    monkeypatch.setattr(os, "scandir", spy(real_scandir))
    monkeypatch.setattr(os, "stat", spy(real_stat))
    assert api.client.get(HEALTH_PATH).status_code == 200
    assert touched == [], touched


@pytest.mark.req("EF-INT-025")
def test_health_page_every_entry_has_a_stable_code_and_an_action(api: AuthEnv) -> None:
    seed_sources(api.settings)
    seed_lost_original(api.settings)
    page = _page(api)
    entries: list[dict[str, object]] = []
    entries += [s for s in page["sources"] if s["state"] != "ok"]  # type: ignore[attr-defined]
    entries += page["lost_originals"]  # type: ignore[operator]
    entries += page["backup"]["warnings"]  # type: ignore[index]
    assert entries
    for entry in entries:
        assert isinstance(entry["code"], str) and entry["code"]
        action = entry["action"]
        assert isinstance(action, dict) and cli_exists(str(action["command"])), entry


# -- stats: requests by profile ---------------------------------------------------------------------


@pytest.mark.req("EF-INT-025")
def test_stats_requests_per_profile_and_delivered_share(api: AuthEnv) -> None:
    ids = {"ana": add_profile(api, "ana", "member"), "leo": add_profile(api, "leo", "child", 12)}
    now = datetime.now(UTC)
    seed_requests(api.settings, now, ids)
    api.clock.current = now
    assert api.login().status_code == 200
    body = api.client.get("/api/v1/stats", params={"since": "30d"}).json()
    rows = {r["username"]: r for r in body["requests_by_profile"]}
    assert set(rows) == {"ana", "leo"}  # an operation has no profile; 40 days ago is outside
    assert (rows["ana"]["requested"], rows["ana"]["delivered"]) == (3, 2)
    assert rows["ana"]["delivered_share_percent"] == 67
    assert (rows["leo"]["requested"], rows["leo"]["delivered"]) == (1, 0)
    assert rows["leo"]["delivered_share_percent"] == 0
    assert rows["ana"]["profile_id"] == ids["ana"]
    # the page carries the same figures, for the administrator alone
    page = api.client.get(HEALTH_PATH).json()
    assert page["stats"]["requests_by_profile"] == body["requests_by_profile"]
    member = api.other_client("10.0.0.30")
    assert api.login("ana", PASSWORD, client=member).status_code == 200
    denied = member.get("/api/v1/stats")
    assert denied.status_code == 403 and "requests_by_profile" not in denied.text
