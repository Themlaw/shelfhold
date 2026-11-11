"""`shelfhold revisions list|apply` and `/api/v1/revisions` (EF-BIB-043, EF-BIB-024, EF-USR-024).

The commands are the real ones (the host CLI under the `flock`); the routes are the real ones of
`create_app()`, with a stub of the operating services where the test needs to see what reached it.
"""

import re
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import IncomingUnit
from shelfhold.config import Settings
from shelfhold.identity.public import RESERVED_ACTIONS, Effect
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.ops.errors import CodedRefusal
from tests.auth_env import auth_env, run_cli
from tests.catalog_env import Harness, at
from tests.support.catalog import SupportCatalog

KEY = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64


def seed(settings: Settings, *, suspect: bool = False) -> None:
    """One Variant whose second revision waits in the queue (a rewrite, or a quarantined one)."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    try:
        with engine.begin() as connection:
            h = Harness(connection, SupportCatalog(connection))
            binding = h.binding(h.edition())
            h.catalog.ingest(
                IncomingUnit(
                    binding=binding, remote_unit_id="c1", raw_number="1", seen_at=at(0),
                    page_count=20, content_hash=HASH_A, revision_hint="v1",
                )
            )  # fmt: skip
            h.catalog.ingest(
                IncomingUnit(
                    binding=binding, remote_unit_id="c1", raw_number="1", seen_at=at(1),
                    page_count=1 if suspect else 20, content_hash=HASH_B, revision_hint="v2",
                )
            )  # fmt: skip
    finally:
        engine.dispose()


def pending(settings: Settings) -> list[sa.Row[Any]]:
    engine = create_db_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            return list(connection.execute(sa.text("SELECT * FROM revision_pending")))
    finally:
        engine.dispose()


@pytest.mark.req("EF-BIB-043")
def test_cli_revisions_list_shows_impact_and_token_then_apply_needs_the_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    seed(settings)
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "revisions", "list") == 0
    listed = capsys.readouterr().out
    token = re.search(r"--confirm (rewrite-\d+-[0-9a-f]{8})", listed)
    assert token and "KOReader" in listed
    # no token: nothing asked, exit code 3
    assert run_cli(monkeypatch, settings, "revisions", "apply") == 3
    assert (
        run_cli(monkeypatch, settings, "revisions", "apply", "--confirm", "rewrite-1-deadbeef") == 3
    )
    assert {r.state for r in pending(settings)} == {"open"}
    # the right token, but nothing is connected to fetch the new versions: still nothing rewritten
    assert run_cli(monkeypatch, settings, "revisions", "apply", "--confirm", token.group(1)) != 0
    assert {r.state for r in pending(settings)} == {"open"}


@pytest.mark.req("EF-BIB-043", "EF-BIB-024")
def test_cli_force_suspect_is_its_own_door_and_still_needs_the_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    seed(settings, suspect=True)
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "revisions", "list") == 0
    assert "suspecte" in capsys.readouterr().out
    assert (
        run_cli(
            monkeypatch,
            settings,
            "revisions",
            "apply",
            "--force-suspect",
            "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        )
        == 3
    )
    assert {r.state for r in pending(settings)} == {"open"}
    assert pending(settings)[0].applied_via is None


@pytest.mark.req("EF-BIB-024", "EF-USR-024")
def test_revisions_apply_and_force_are_reserved_actions() -> None:
    for name in ("revisions.apply", "revisions.force"):
        action = RESERVED_ACTIONS[name]
        assert action.effects == {Effect.REWRITES_EXPORTED_FILES}
        assert action.needs_fresh_auth(restricted_profile_exists=False, hardened=False)


class FakeOps:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.revisions = self

    def list_pending(self) -> dict[str, Any]:
        return {
            "items": [],
            "koreader_positions": 0,
            "token": "rewrite-0-e3b0c442",
            "newly_queued": 0,
        }

    def apply(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if kwargs["confirm"] != "ok":
            raise CodedRefusal("revisions.confirmation_required", "rewrite-0-e3b0c442")
        return {"metadata_applied": 0, "requested": 0, "skipped_read": 0, "skipped_suspect": 0}


@pytest.mark.req("EF-BIB-043", "EF-BIB-024", "EF-USR-024")
def test_http_apply_and_force_never_for_a_key_and_force_is_no_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    fake = FakeOps()
    with auth_env(monkeypatch, settings, ops_services=SimpleNamespace(revisions=fake)) as api:
        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "admin", "keys", "create", "--name", "ci") == 0
        key = KEY.search(capsys.readouterr().out)
        assert key
        bearer = {"Authorization": f"Bearer {key.group(0)}"}
        assert api.client.get("/api/v1/revisions", headers=bearer).status_code == 200  # a listing
        for path in ("/api/v1/revisions/apply", "/api/v1/revisions/force"):
            refused = api.client.post(path, json={"confirm": "ok"}, headers=bearer)
            assert refused.status_code == 403
            assert refused.json()["type"] == "/api/v1/problems/credential-not-allowed"
        assert api.client.post("/api/v1/revisions/apply", json={"confirm": "ok"}).status_code == 401
        assert fake.calls == []

        assert api.login().status_code == 200
        api.clock.advance(timedelta(minutes=20))  # an old session
        stale = api.post("/api/v1/revisions/force", json={"confirm": "ok"})
        assert stale.status_code == 401 and stale.json()["type"].endswith("reauth-required")
        assert fake.calls == []

        assert api.login().status_code == 200  # a fresh one
        # `force` is not a flag of the apply route: it has its own reserved action
        flagged = api.post("/api/v1/revisions/apply", json={"confirm": "ok", "force": True})
        assert flagged.status_code == 422 and fake.calls == []
        # nor does the force route lift the guard for a whole batch: revisions are named
        unnamed = api.post("/api/v1/revisions/force", json={"confirm": "ok"})
        assert unnamed.status_code == 422 and fake.calls == []
        unconfirmed = api.post("/api/v1/revisions/apply", json={})
        assert unconfirmed.status_code == 409
        assert unconfirmed.json()["type"].endswith("revisions.confirmation-required")
        ok = api.post("/api/v1/revisions/apply", json={"confirm": "ok"})
        assert ok.status_code == 200, ok.text
        forced = api.post(
            "/api/v1/revisions/force",
            json={"confirm": "ok", "revision_ids": ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]},
        )
        assert forced.status_code == 200
        assert [c["force_ids"] for c in fake.calls] == [(), (), ("01ARZ3NDEKTSV4RRFFQ69G5FAV",)]
