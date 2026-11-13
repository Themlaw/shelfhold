"""M3 L06 (EF-USR-024 (4), EF-USR-010): a `request` key acts as its subject, never as more.

The key is issued by an adult for herself, or by an admin for a profile; every right and every
filter is read from the subject. It opens the four routes a person uses for her own requests and
nothing else: no decision, no queue, no reserved action, no request of someone else."""

from __future__ import annotations

from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.identity.public import Credential, Principal, local_auth
from shelfhold.infrastructure.db import create_db_engine
from tests.auth_env import AuthEnv, db_rows
from tests.federated_env import CHILD_ID, MEMBER_ID, instance
from tests.integration.test_requests_http import (
    REQUESTS,
    alpha,
    ask_as_child,
    family,
    ports,  # noqa: F401 (autouse fixture of the request routes)
    table,
)

FREE = {"target_kind": "free_text", "target_value": "A book I heard of"}


def issue(api: AuthEnv, owner: str, *, scope: str, subject: str | None = None) -> str:
    engine = create_db_engine(api.settings.database_path)
    if scope == "request" and subject is None:
        subject = owner  # a request key names its subject: here, the one who holds it
    try:
        return (
            local_auth(engine, api.settings)
            .create_api_key(name="k", username=owner, scope=scope, subject_username=subject)
            .value
        )
    finally:
        engine.dispose()


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.req("EF-USR-024", "EF-USR-010")
def test_request_key_acts_as_subject_user(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        key = issue(api, "member", scope="request")  # an adult, for herself
        answer = api.client.post(REQUESTS, json=FREE, headers=bearer(key))
        assert answer.status_code == 201, answer.text
        rows = db_rows(api.settings, "SELECT requester_user_id, visibility FROM request")
        assert rows == [(MEMBER_ID, "private")]  # the subject's, and private like hers
        row = db_rows(api.settings, "SELECT subject_user_id, issued_by FROM api_key")[0]
        assert row == (MEMBER_ID, None)  # NULL: the host operator (the audit names the uid)
        mine = api.client.get(REQUESTS, headers=bearer(key)).json()["items"]
        assert [i["id"] for i in mine] == [answer.json()["id"]]
        # the admin who reads the queue never sees a private request's title
        admin_key_less = api.client.get(f"{REQUESTS}/queue")
        assert admin_key_less.status_code == 401


@pytest.mark.req("EF-USR-024")
def test_a_key_issued_for_a_profile_acts_as_that_profile_and_issuer_is_recorded(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        key = issue(api, "admin", scope="request", subject="member")
        assert api.client.post(REQUESTS, json=FREE, headers=bearer(key)).status_code == 201
        assert db_rows(api.settings, "SELECT requester_user_id FROM request") == [(MEMBER_ID,)]
        (subject, issuer) = db_rows(api.settings, "SELECT subject_user_id, issued_by FROM api_key")[
            0
        ]
        assert subject == MEMBER_ID and issuer != MEMBER_ID
        # the session of the real admin, with a key in the same request: the key is what counts
        mixed = api.client.get(REQUESTS, headers=bearer(key))
        assert mixed.status_code == 200 and len(mixed.json()["items"]) == 1


@pytest.mark.req("EF-USR-024", "EF-USR-014")
def test_a_child_key_stays_a_child(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        kid = family(api)
        key = issue(api, "admin", scope="request", subject="kid")
        # a child asks by the reference of her search only: free text is refused for her key too
        refused = api.client.post(REQUESTS, json=FREE, headers=bearer(key))
        assert refused.status_code in (403, 404, 422), refused.text
        assert table(api) == []
        # her own request, made in her session, is read through her key with the child's shape
        mine = ask_as_child(api, kid)
        listed = api.client.get(REQUESTS, headers=bearer(key)).json()["items"]
        assert [i["id"] for i in listed] == [mine]
        assert set(listed[0]) == {
            "id", "state", "title", "reject_reason", "created_at", "updated_at",
            "family_state", "open_url",
        }  # fmt: skip
        # the same key never reads the parents' side
        assert api.client.get(f"{REQUESTS}/queue", headers=bearer(key)).status_code in (401, 403)


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_request_key_cannot_read_or_cancel_the_request_of_someone_else(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        kid = family(api)
        theirs = ask_as_child(api, kid)
        key = issue(api, "member", scope="request")
        ghost = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        real = api.client.get(f"{REQUESTS}/{theirs}", headers=bearer(key))
        absent = api.client.get(f"{REQUESTS}/{ghost}", headers=bearer(key))
        assert real.status_code == absent.status_code == 404
        assert real.json()["code"] == absent.json()["code"]  # as absent as one that never was
        assert (
            api.client.post(f"{REQUESTS}/{theirs}/cancel", headers=bearer(key)).status_code == 404
        )
        assert db_rows(api.settings, "SELECT state FROM request") == [("pending",)]
        assert api.client.get(REQUESTS, headers=bearer(key)).json()["items"] == []


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_request_key_cannot_decide_or_reach_a_reserved_action(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        kid = family(api)
        theirs = ask_as_child(api, kid)
        for owner, subject in (("member", None), ("admin", None)):  # even an admin's own key
            key = issue(api, owner, scope="request", subject=subject)
            calls: list[Any] = [
                ("POST", f"{REQUESTS}/{theirs}/approve", {"decision": "accept"}),
                ("POST", f"{REQUESTS}/{theirs}/reject", {"reason": "no"}),
                ("POST", f"{REQUESTS}/{theirs}/not-found", None),
                ("GET", f"{REQUESTS}/{theirs}/approval", None),
                ("GET", f"{REQUESTS}/queue", None),
            ]
            for method, path, body in calls:
                answer = api.client.request(method, path, json=body, headers=bearer(key))
                assert answer.status_code in (401, 403), (owner, path, answer.status_code)
            assert db_rows(api.settings, "SELECT state FROM request") == [("pending",)]
        # approving with the key of a member is the reserved door: refused for the credential
        member_key = issue(api, "member", scope="request")
        refused = api.client.post(
            f"{REQUESTS}/{theirs}/approve", json={}, headers=bearer(member_key)
        )
        assert refused.status_code == 403
        assert refused.json()["code"] in {"credential_not_allowed", "forbidden"}


@pytest.mark.req("EF-USR-024")
def test_read_key_never_creates_a_request_and_a_member_cannot_issue_an_admin_key(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        read = issue(api, "member", scope="read")
        assert api.client.post(REQUESTS, json=FREE, headers=bearer(read)).status_code == 403
        assert table(api) == []
        with pytest.raises(ValueError, match="scope_requires_admin"):
            issue(api, "member", scope="admin")
        with pytest.raises(ValueError, match="scope"):
            issue(api, "member", scope="destructive")  # reserved, not implemented (EF-USR-024 (6))
        adult = Principal(
            user_id=MEMBER_ID,
            username="member",
            role="member",
            credential=Credential.SESSION,
            session_id="01J0000000000000000000SESS",
            auth_time=None,
        )
        engine = create_db_engine(api.settings.database_path)
        try:  # an adult issues for herself only: for a profile it takes an admin
            with pytest.raises(ValueError, match="subject_not_allowed"):
                local_auth(engine, api.settings).create_api_key(
                    name="k", username="member", scope="request", subject_username="kid", by=adult
                )
        finally:
            engine.dispose()


@pytest.mark.req("EF-USR-024")
def test_keys_for_a_deleted_subject_stop_working_and_a_child_key_cannot_hold_admin(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        key = issue(api, "admin", scope="request", subject="kid")
        import sqlite3

        connection = sqlite3.connect(api.settings.database_path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("DELETE FROM user WHERE id = ?", (CHILD_ID,))
            connection.commit()
        finally:
            connection.close()
        assert api.client.get(REQUESTS, headers=bearer(key)).status_code == 401


@pytest.mark.req("EF-USR-024")
def test_cli_issues_a_request_key_for_a_profile_and_refuses_an_admin_one(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.auth_env import run_cli

    with instance(monkeypatch, settings, alpha()) as api:
        capsys.readouterr()
        assert run_cli(monkeypatch, api.settings, "admin", "keys", "create", "--scope", "request",
                       "--subject", "member") == 0  # fmt: skip
        key = capsys.readouterr().out.strip().splitlines()[-1]
        assert api.client.post(REQUESTS, json=FREE, headers=bearer(key)).status_code == 201
        assert db_rows(api.settings, "SELECT requester_user_id FROM request") == [(MEMBER_ID,)]
        # the `admin` scope is an admin's for herself: never for another account
        assert run_cli(monkeypatch, api.settings, "admin", "keys", "create", "--scope", "admin",
                       "--subject", "member") == 1  # fmt: skip
        assert run_cli(monkeypatch, api.settings, "admin", "keys", "create", "--scope", "request",
                       "--subject", "nobody") == 1  # fmt: skip
