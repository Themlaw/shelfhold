"""`/api/v1/shelves` and `/api/v1/plans` (EF-EXP-002, EF-EXP-008, EF-INT-024)."""

from dataclasses import dataclass, field
from typing import ClassVar

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.application.target_state import DestinationPausedError
from shelfhold.infrastructure.db import create_db_engine
from tests.auth_env import auth_env


@dataclass
class FakePreview:
    plan_id: str = "01JPLAN00000000000000000A1"
    base_version: ClassVar[str] = "4242"
    ops: tuple[object, ...] = ()
    blocked: tuple[object, ...] = ()
    warnings: tuple[object, ...] = ()
    checks: tuple[str, ...] = ("sentinel",)


@dataclass
class FakePipeline:
    stale: bool = False
    paused: str | None = None
    applied: list[tuple[str, str]] = field(default_factory=list)

    def preview(self, shelf_destination_id: str) -> FakePreview:
        return FakePreview()

    def queue_apply(self, plan_id: str, *, if_match: str) -> str:
        if self.paused:
            raise DestinationPausedError(self.paused)
        if self.stale or if_match != FakePreview.base_version:
            raise StalePlanError("the preview changed: recompute it")
        self.applied.append((plan_id, if_match))
        return "01JJOB0000000000000000000A"


@pytest.mark.req("EF-EXP-002", "EF-INT-024")
def test_shelf_route_refuses_below_18_and_creates_an_adult_shelf(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as api:
        anonymous = api.client.post("/api/v1/shelves", json={"name": "Manga", "phrase": "Mangas"})
        assert anonymous.status_code == 401
        assert api.login().status_code == 200
        refused = api.post(
            "/api/v1/shelves", json={"name": "Enfants", "phrase": "Jeunesse", "max_age_rating": 12}
        )
        assert refused.status_code == 422
        assert refused.json()["code"] == "shelf.parental_control_unavailable"
        for name, ceiling in (("Adultes", 18), ("X", 99), ("Sans plafond", None)):
            created = api.post(
                "/api/v1/shelves", json={"name": name, "phrase": name, "max_age_rating": ceiling}
            )
            assert created.status_code == 201, created.text
        engine = create_db_engine(settings.database_path)
        with engine.connect() as connection:
            rows = (
                connection.execute(sa.text("SELECT name FROM shelf ORDER BY name")).scalars().all()
            )
        engine.dispose()
        assert rows == ["Adultes", "Sans plafond", "X"]  # nothing was stored for the refused one


@pytest.mark.req("EF-EXP-008", "EF-EXP-042")
def test_plan_routes_preview_then_apply_with_if_match(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    pipeline = FakePipeline()
    with auth_env(monkeypatch, settings, plan_service=pipeline) as api:
        assert api.login().status_code == 200
        preview = api.post(
            "/api/v1/plans", json={"shelf_destination_id": "01JDEST0000000000000000A1"}
        )
        assert preview.status_code == 201
        assert preview.headers["ETag"] == '"4242"'
        assert preview.json()["plan_id"] == FakePreview.plan_id
        url = f"/api/v1/plans/{FakePreview.plan_id}/apply"
        assert api.post(url).status_code == 428  # a plan is never applied blind
        stale = api.post(url, headers={"If-Match": '"4241"'})
        assert (stale.status_code, stale.json()["code"]) == (412, "plan.stale")
        assert pipeline.applied == []
        accepted = api.post(url, headers={"If-Match": '"4242"'})
        assert accepted.status_code == 202  # queued: the job does the work (EF-INT-001)
        assert accepted.headers["location"] == "/api/v1/jobs/01JJOB0000000000000000000A"
        assert pipeline.applied == [(FakePreview.plan_id, "4242")]
        pipeline.stale = True  # it moved while applying: same answer, same code
        again = api.post(url, headers={"If-Match": '"4242"'})
        assert (again.status_code, again.json()["code"]) == (412, "plan.stale")


@pytest.mark.req("EF-EXP-055", "EF-EXP-039")
@pytest.mark.parametrize("code", ["target.child_exposure", "target.instance_changed"])
def test_apply_on_a_paused_destination_answers_a_target_code_not_a_500(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, code: str
) -> None:
    pipeline = FakePipeline(paused=code)
    with auth_env(monkeypatch, settings, plan_service=pipeline) as api:
        assert api.login().status_code == 200
        url = f"/api/v1/plans/{FakePreview.plan_id}/apply"
        paused = api.post(url, headers={"If-Match": '"4242"'})
        assert (paused.status_code, paused.json()["code"]) == (409, code)
