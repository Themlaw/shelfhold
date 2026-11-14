"""The HTTP doors the screens of L45 stand on (M3 L45, EF-EXP-031, EF-EXP-002, EF-USR-024).

`target remove` and `detach --full` (L30) and the listing of the destinations of the shelves exist
as services and as CLI commands only; the screens need a door each:

- `GET  /api/v1/destinations`                       the destinations, with their naming template;
- `POST /api/v1/destinations/{id}/removal-preview`  what the removal would lose, and the phrase;
- `POST /api/v1/destinations/{id}/removal`          the removal (reserved `destination.remove_all`);
- `POST /api/v1/targets/{id}/detach-preview`        what the detachment would lose, and the phrase;
- `POST /api/v1/targets/{id}/detach`                the detachment (reserved `detach.full`).

All five are the admin's: a key, a member, a child and nobody are refused before a service is
touched; the two that act need a fresh session (401 `reauth-required` otherwise) and the phrase.
The routes are the real ones; the services behind them are a recorder.
"""

import re
from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.ops.errors import CodedRefusal
from tests.auth_env import CSRF_HEADER, AuthEnv, auth_env, run_cli
from tests.visibility_env import add_profile

pytestmark = pytest.mark.req("EF-EXP-031", "EF-EXP-002", "EF-USR-024")

KEY = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")
DEST = "01JDEST000000000000000000A"
TARGET = "01JTARG000000000000000000A"
PHRASE = "RETIRER manga/"
DETACH_PHRASE = "DÉTACHER Kavita"
LIST = "/api/v1/destinations"
REMOVAL_PREVIEW = f"/api/v1/destinations/{DEST}/removal-preview"
REMOVAL = f"/api/v1/destinations/{DEST}/removal"
DETACH_PREVIEW = f"/api/v1/targets/{TARGET}/detach-preview"
DETACH = f"/api/v1/targets/{TARGET}/detach"
REFUSED = ("/api/v1/problems/credential-not-allowed", "/api/v1/problems/forbidden")

DESTINATION = {
    "id": DEST,
    "shelf_id": "01JSHLF000000000000000000A",
    "shelf_name": "Manga",
    "target_id": TARGET,
    "root_subpath": "manga/",
    "transport": "tree",
    "adopted": True,
    "flagged_names": 2,
    "template": {
        "chapter": "{series} - Chapter {number}",
        "volume": "{series} - Vol.{number}",
        "special": "{series} - {label}",
        "special_dir": "Specials",
        "segment": "{series} - Part {n}",
    },
}
REMOVAL_REPORT = {
    "deleted": 12, "forgotten": 40, "cancelled": False, "complete": True,
    "left": [{"code": "adopted_kept", "path": "manga/Adopted"}],
}  # fmt: skip
DETACH_REPORT = {
    "target_id": TARGET, "complete": True, "hidden_removed": 12, "key_revoked": True,
    "key_gone": False, "forgotten": 40, "cancelled": False, "settings_restored": True,
    "settings": [{"exclude_patterns_restored": True, "type_unchanged": True}],
    "leftovers": [{"code": "suwayomi_downloads", "count": 3}],
}  # fmt: skip


class _Teardown:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def list_destinations(self) -> list[dict[str, Any]]:
        self.calls.append(("list",))
        return [DESTINATION]

    def remove_preview(self, *, destination_id: str, mode: str) -> dict[str, Any]:
        self.calls.append(("remove_preview", destination_id, mode))
        return {
            "destination_id": destination_id, "mode": mode, "phrase": PHRASE,
            "warnings": [{"code": "removal:files_deleted", "count": 12}],
        }  # fmt: skip

    def remove(
        self, *, destination_id: str, mode: str, typed: str | None, give_up: bool, principal: Any
    ) -> dict[str, Any]:
        self.calls.append(("remove", destination_id, mode, typed, give_up))
        if typed != PHRASE:
            raise CodedRefusal("removal.refused", "typed_mismatch")
        return REMOVAL_REPORT

    def full_preview(self, *, target_id: str) -> dict[str, Any]:
        self.calls.append(("full_preview", target_id))
        return {
            "target_id": target_id, "phrase": DETACH_PHRASE,
            "warnings": [{"code": "detach:hidden_erased", "count": 12}],
        }  # fmt: skip

    def full(
        self, *, target_id: str, typed: str | None, credential: str | None,
        discard_hidden_data: bool, discard_tombstone_journal: bool, key_gone: bool = False,
        give_up: bool, principal: Any,
    ) -> dict[str, Any]:  # fmt: skip
        self.calls.append(
            ("full", target_id, typed, credential, discard_hidden_data, discard_tombstone_journal)
        )
        if typed != DETACH_PHRASE:
            raise CodedRefusal("detach.refused", "typed_mismatch")
        return DETACH_REPORT


@pytest.fixture
def teardown() -> _Teardown:
    return _Teardown()


@pytest.fixture
def api(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, teardown: _Teardown
) -> Iterator[AuthEnv]:
    services = SimpleNamespace(teardown=teardown)
    with auth_env(monkeypatch, settings, ops_services=services) as env:
        yield env


def _key(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings,
         *extra: str) -> dict[str, str]:  # fmt: skip
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "admin", "keys", "create", "--name", "ci", *extra) == 0
    match = KEY.search(capsys.readouterr().out)
    assert match
    return {"Authorization": f"Bearer {match.group(0)}"}


DOORS: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("GET", LIST, None),
    ("POST", REMOVAL_PREVIEW, {"mode": "keep_files"}),
    ("POST", REMOVAL, {"mode": "keep_files", "typed": PHRASE}),
    ("POST", DETACH_PREVIEW, None),
    ("POST", DETACH, {"typed": DETACH_PHRASE}),
)


def test_a_key_a_member_a_child_and_nobody_reach_none_of_the_five_doors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv,
    teardown: _Teardown,
) -> None:  # fmt: skip
    admin_key = _key(monkeypatch, capsys, api.settings)
    for method, path, body in DOORS:
        anonymous = api.client.request(method, path, json=body)
        assert anonymous.status_code in (401, 403), path
        keyed = api.client.request(method, path, json=body, headers=admin_key)
        assert keyed.status_code == 403 and keyed.json()["type"] in REFUSED, path
    api.login()
    for role, ip in (("member", "10.0.0.5"), ("child", "10.0.0.6")):
        extra: dict[str, object] = {"max_age_rating": 10} if role == "child" else {}
        who = add_profile(api, f"{role}1", role, ip, **extra)
        for method, path, body in DOORS:
            refused = who.request(method, path, json=body, headers={CSRF_HEADER: api.csrf(who)})
            assert refused.status_code == 403, (role, path)
            assert "phrase" not in refused.text and "manga/" not in refused.text
    assert teardown.calls == []  # no refusal reached a service


def test_the_listing_and_the_two_previews_answer_the_admin_and_change_nothing(
    api: AuthEnv, teardown: _Teardown
) -> None:
    api.login()
    listed = api.client.get(LIST)
    assert listed.status_code == 200 and listed.json() == {"items": [DESTINATION]}
    shown = api.post(REMOVAL_PREVIEW, json={"mode": "delete_files"})
    assert shown.status_code == 200
    assert shown.json()["phrase"] == PHRASE
    assert shown.json()["warnings"] == [{"code": "removal:files_deleted", "count": 12}]
    detach = api.post(DETACH_PREVIEW)
    assert detach.status_code == 200 and detach.json()["phrase"] == DETACH_PHRASE
    assert [c[0] for c in teardown.calls] == ["list", "remove_preview", "full_preview"]
    assert teardown.calls[1] == ("remove_preview", DEST, "delete_files")


def test_the_removal_runs_with_the_mode_and_the_phrase_the_admin_typed(
    api: AuthEnv, teardown: _Teardown
) -> None:
    api.login()
    done = api.post(REMOVAL, json={"mode": "delete_files", "typed": PHRASE})
    assert done.status_code == 200 and done.json() == REMOVAL_REPORT
    assert teardown.calls == [("remove", DEST, "delete_files", PHRASE, False)]


def test_the_detachment_runs_with_its_phrase_options_and_password(
    api: AuthEnv, teardown: _Teardown
) -> None:
    api.login()
    body = {
        "typed": DETACH_PHRASE, "credential": "synthetic-kavita-pw",
        "discard_hidden_data": True, "discard_tombstone_journal": False,
    }  # fmt: skip
    done = api.post(DETACH, json=body)
    assert done.status_code == 200 and done.json() == DETACH_REPORT
    assert teardown.calls == [("full", TARGET, DETACH_PHRASE, "synthetic-kavita-pw", True, False)]
    assert "synthetic-kavita-pw" not in done.text  # the report never echoes the password


def test_a_wrong_phrase_is_a_409_and_the_two_acting_doors_need_a_fresh_session(
    api: AuthEnv, teardown: _Teardown
) -> None:
    api.login()
    wrong = api.post(REMOVAL, json={"mode": "keep_files", "typed": "retirer manga/"})
    assert wrong.status_code == 409 and wrong.json()["type"].endswith("removal.refused")
    wrong = api.post(DETACH, json={"typed": DETACH_PHRASE + " "})
    assert wrong.status_code == 409 and wrong.json()["type"].endswith("detach.refused")
    teardown.calls.clear()
    api.clock.advance(timedelta(minutes=20))
    for path, body in ((REMOVAL, {"mode": "keep_files", "typed": PHRASE}),
                       (DETACH, {"typed": DETACH_PHRASE})):  # fmt: skip
        stale = api.post(path, json=body)
        assert stale.status_code == 401, path
        assert stale.json()["type"] == "/api/v1/problems/reauth-required"
    assert teardown.calls == []  # a stale session reaches no service


def test_the_bodies_are_closed_a_mode_and_a_phrase_are_required(
    api: AuthEnv, teardown: _Teardown
) -> None:
    api.login()
    for path, body in (
        (REMOVAL_PREVIEW, {}),
        (REMOVAL_PREVIEW, {"mode": "everything"}),
        (REMOVAL, {"mode": "keep_files"}),
        (REMOVAL, {"typed": PHRASE}),
        (DETACH, {}),
        (DETACH, {"typed": DETACH_PHRASE, "discard_hidden_data": "maybe"}),
    ):
        assert api.post(path, json=body).status_code == 422, (path, body)
    assert teardown.calls == []
