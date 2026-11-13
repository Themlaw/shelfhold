"""The HTTP doors of the rename plans (M3 L29, EF-EXP-030, EF-EXP-037, EF-USR-024): the preview
and `POST /api/v1/plans/{id}/apply`. Applying a rename plan is a reserved action (a fresh admin
session, never a key, a member or a child) and the reader count never reaches anybody else. The
attacks: a key, a member, a child, no session, no confirmation, a plan that is not the caller's
to apply, a stale validator."""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pytest

from shelfhold.config import Settings
from shelfhold.export.application.pipeline import UnknownPlanError
from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.application.rename_plan import (
    ItemMove,
    PlannedRename,
    ProgressLossWarning,
    RenameConfirmationRequiredError,
    RenamePreview,
    RenameRefusedError,
)
from tests.auth_env import CSRF_HEADER, AuthEnv, auth_env, run_cli
from tests.visibility_env import add_profile

pytestmark = pytest.mark.req("EF-EXP-030", "EF-EXP-037", "EF-USR-024")

KEY = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")
RENAME = "01JPLAN0000000000000000R01"
EXPORT = "01JPLAN0000000000000000E01"
DEST = "01JDEST000000000000000000A"
READERS = 7
REFUSED = ("/api/v1/problems/credential-not-allowed", "/api/v1/problems/forbidden")


@dataclass
class FakePipeline:
    version: ClassVar[str] = "4242"
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    stale: bool = False

    def kind_of(self, plan_id: str) -> str:
        if plan_id == RENAME:
            return "rename_plan"
        if plan_id == EXPORT:
            return "export"
        raise UnknownPlanError(plan_id)

    def rename_preview(
        self, destination_id: str, *, template: Any = None, moves: Any = ()
    ) -> RenamePreview:
        self.calls.append(("preview", destination_id, template, tuple(moves)))
        if destination_id != DEST:
            raise RenameRefusedError("unknown_item")
        step = PlannedRename("file", "A/a.cbz", "A/b.cbz", "01JITEM00000000000000000A1")
        return RenamePreview(
            RENAME, self.version, (step,), 1, (), (ProgressLossWarning(READERS, True, ("A",)),)
        )

    def queue_apply(
        self, plan_id: str, *, if_match: str, confirm_progress_loss: bool = False
    ) -> str:
        self.calls.append(("apply", plan_id, if_match, confirm_progress_loss))
        if self.stale or if_match != self.version:
            raise StalePlanError("the preview changed: recompute it")
        if plan_id == RENAME and not confirm_progress_loss:
            raise RenameConfirmationRequiredError(READERS)
        return "01JJOB0000000000000000000A"

    # the other doors of the router are not used here
    def preview(self, shelf_destination_id: str) -> Any: ...
    def naming_preview(self, shelf_destination_id: str, template: Any) -> Any: ...


@pytest.fixture
def pipeline() -> FakePipeline:
    return FakePipeline()


@pytest.fixture
def api(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, pipeline: FakePipeline
) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings, plan_service=pipeline) as env:
        yield env


def _key(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings,
         *extra: str) -> dict[str, str]:  # fmt: skip
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "admin", "keys", "create", "--name", "ci", *extra) == 0
    match = KEY.search(capsys.readouterr().out)
    assert match
    return {"Authorization": f"Bearer {match.group(0)}"}


APPLY = f"/api/v1/plans/{RENAME}/apply"
PREVIEW = "/api/v1/plans/renames"
BODY = {"shelf_destination_id": DEST, "template": {"chapter": "{series} - Chapter {number}"}}
IF_MATCH = {"If-Match": f'"{FakePipeline.version}"'}


def test_a_key_a_member_a_child_and_nobody_apply_or_see_the_count(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv,
    pipeline: FakePipeline,
) -> None:  # fmt: skip
    admin_key = _key(monkeypatch, capsys, api.settings)
    read_only = _key(monkeypatch, capsys, api.settings, "--read-only")
    doors = ((PREVIEW, BODY), (APPLY, {"confirm_progress_loss": True}))
    for path, body in doors:
        assert api.client.post(path, json=body, headers=IF_MATCH).status_code in (401, 403)
    for headers in (admin_key, read_only):
        refused = api.client.post(APPLY, json={"confirm_progress_loss": True},
                                  headers={**headers, **IF_MATCH})  # fmt: skip
        assert refused.status_code == 403, headers  # a key, even with the admin scope
        assert refused.json()["type"] in REFUSED and "readers" not in refused.text
    api.login()
    for role, ip in (("member", "10.0.0.5"), ("child", "10.0.0.6")):
        extra: dict[str, object] = {"max_age_rating": 10} if role == "child" else {}
        who = add_profile(api, f"{role}1", role, ip, **extra)
        for path, body in doors:
            refused = who.post(path, json=body, headers={CSRF_HEADER: api.csrf(who), **IF_MATCH})
            assert refused.status_code == 403, (role, path)
            assert "readers" not in refused.text  # the count never leaves for a non-admin
    assert pipeline.calls == []  # no refusal reached the service


def test_admin_session_previews_with_the_count_and_applies_only_with_confirmation(
    api: AuthEnv, pipeline: FakePipeline
) -> None:
    api.login()
    shown = api.post(PREVIEW, json=BODY)
    assert shown.status_code == 200 and shown.headers["ETag"] == f'"{FakePipeline.version}"'
    warning = shown.json()["warnings"][0]
    assert warning["code"] == "progress_loss" and warning["readers"] == READERS
    refused = api.post(APPLY, headers=IF_MATCH, json={})
    assert refused.status_code == 409
    assert refused.json()["type"] == "/api/v1/problems/rename.confirmation-required"
    assert refused.json()["readers"] == READERS
    accepted = api.post(APPLY, headers=IF_MATCH, json={"confirm_progress_loss": True})
    assert accepted.status_code == 202 and "Location" in accepted.headers
    assert pipeline.calls[-1] == ("apply", RENAME, FakePipeline.version, True)


def test_the_confirmation_is_a_boolean_and_nothing_else(
    api: AuthEnv, pipeline: FakePipeline
) -> None:
    api.login()
    for body in ({"confirm_progress_loss": "yes"}, {"confirm_progress_loss": 1, "x": 1},
                 {"confirm": True}):  # fmt: skip
        assert api.post(APPLY, headers=IF_MATCH, json=body).status_code == 422, body
    assert [c for c in pipeline.calls if c[0] == "apply"] == []


def test_a_stale_or_missing_validator_and_an_unknown_plan(
    api: AuthEnv, pipeline: FakePipeline
) -> None:
    api.login()
    body = {"confirm_progress_loss": True}
    assert api.post(APPLY, json=body).status_code == 428  # no If-Match
    assert api.post(APPLY, headers={"If-Match": '"abc"'}, json=body).status_code == 412
    gone = api.post("/api/v1/plans/01JPLAN0000000000000000X99/apply", headers=IF_MATCH, json=body)
    assert gone.status_code == 404


def test_an_export_plan_is_not_a_rename_plan_and_keeps_its_door(
    api: AuthEnv, pipeline: FakePipeline, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    admin_key = _key(monkeypatch, capsys, api.settings)
    path = f"/api/v1/plans/{EXPORT}/apply"
    ok = api.client.post(path, headers={**admin_key, **IF_MATCH})
    assert ok.status_code == 202  # an export plan is applied by an admin key as before
    # but a rename plan is never reachable by it, whatever the path says
    assert api.client.post(APPLY, headers={**admin_key, **IF_MATCH}).status_code == 403


def test_a_refused_preview_says_why_and_stores_nothing(
    api: AuthEnv, pipeline: FakePipeline
) -> None:
    api.login()
    other = api.post(PREVIEW, json={**BODY, "shelf_destination_id": "01JDEST000000000000000000B"})
    assert other.status_code == 409 and other.json()["type"].endswith("rename.refused")
    assert other.json()["reason"] == "unknown_item"
    for body in ({}, {"shelf_destination_id": DEST, "moves": [{"export_item_id": "x"}]},
                 {"shelf_destination_id": DEST, "template": {"bogus": "x"}},
                 {"shelf_destination_id": DEST, "template": {"chapter": "{nope}"}},
                 {"shelf_destination_id": DEST, "moves": [], "template": None}):  # fmt: skip
        assert api.post(PREVIEW, json=body).status_code == 422, body
    assert ItemMove  # the model of a move is part of the contract
