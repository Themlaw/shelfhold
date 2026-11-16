"""The HTTP door of a rename plan that carries progress (M4 L16, EF-EXP-017, EF-EXP-030,
EF-USR-024): the members it names and the refusal of a move across two roots reach the admin who
previews, and nobody else. The attacks: a read-only key, a key, a member, a child, no session."""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pytest

from shelfhold.config import Settings
from shelfhold.export.application.rename_plan import (
    PlannedRename,
    ProgressLossWarning,
    RenamePreview,
    RenameRefusedError,
)
from tests.auth_env import CSRF_HEADER, AuthEnv, auth_env, run_cli
from tests.visibility_env import add_profile

pytestmark = pytest.mark.req("EF-EXP-017", "EF-EXP-030", "EF-USR-024")

KEY = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")
RENAME = "01JPLAN0000000000000000R01"
DEST = "01JDEST000000000000000000A"
OTHER_DEST = "01JDEST000000000000000000B"
ITEM = "01JITEM00000000000000000A1"
PREVIEW = "/api/v1/plans/renames"
NAMED = "carol"  # a member without a key: named for the admin, never for anybody else


@dataclass
class FakePipeline:
    version: ClassVar[str] = "4242"
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def kind_of(self, plan_id: str) -> str:
        return "rename_plan"

    def rename_preview(
        self, destination_id: str, *, template: Any = None, moves: Any = ()
    ) -> RenamePreview:
        self.calls.append(("preview", destination_id, tuple(moves)))
        if any(getattr(m, "destination_id", None) not in (None, destination_id) for m in moves):
            raise RenameRefusedError("cross_root")
        step = PlannedRename("file", "A/a.cbz", "A/b.cbz", ITEM)
        warning = ProgressLossWarning(1, False, ("A",), members=(NAMED,))
        return RenamePreview(RENAME, self.version, (step,), 1, (), (warning,))

    def queue_apply(self, plan_id: str, *, if_match: str, confirm_progress_loss: bool = False) -> str:
        return "01JJOB0000000000000000000A"

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


BODY = {"shelf_destination_id": DEST, "moves": [{"export_item_id": ITEM, "new_rel_path": "B/b.cbz"}]}


def test_the_admin_sees_the_members_without_a_key(api: AuthEnv) -> None:
    api.login()
    shown = api.post(PREVIEW, json=BODY)
    assert shown.status_code == 200
    warning = shown.json()["warnings"][0]
    assert warning["code"] == "progress_loss" and warning["members"] == [NAMED]


def test_nobody_but_an_admin_session_sees_a_member_named(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv,
    pipeline: FakePipeline,
) -> None:  # fmt: skip
    read_only = _key(monkeypatch, capsys, api.settings, "--read-only")
    admin_key = _key(monkeypatch, capsys, api.settings)
    for headers in (read_only, admin_key):
        refused = api.client.post(PREVIEW, json=BODY, headers=headers)
        assert refused.status_code in (401, 403) and NAMED not in refused.text
    assert api.client.post(PREVIEW, json=BODY).status_code in (401, 403)
    api.login()
    for role, ip in (("member", "10.0.0.5"), ("child", "10.0.0.6")):
        extra: dict[str, object] = {"max_age_rating": 10} if role == "child" else {}
        who = add_profile(api, f"{role}1", role, ip, **extra)
        refused = who.post(PREVIEW, json=BODY, headers={CSRF_HEADER: api.csrf(who)})
        assert refused.status_code == 403 and NAMED not in refused.text, role
    assert pipeline.calls == []  # no refusal reached the service


def test_a_move_across_two_roots_is_refused_with_its_code(api: AuthEnv) -> None:
    api.login()
    body = {
        "shelf_destination_id": DEST,
        "moves": [{"export_item_id": ITEM, "new_rel_path": "B/b.cbz",
                   "destination_id": OTHER_DEST}],
    }  # fmt: skip
    refused = api.post(PREVIEW, json=body)
    assert refused.status_code == 409 and refused.json()["type"].endswith("rename.refused")
    assert refused.json()["reason"] == "cross_root"
    for bad in ("", "x" * 40, 7):  # the destination of a move is an identifier, nothing else
        body["moves"][0]["destination_id"] = bad  # type: ignore[index]
        assert api.post(PREVIEW, json=body).status_code == 422, bad
