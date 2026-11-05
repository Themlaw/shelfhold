"""`/api/v1/shelves` and `/api/v1/plans` (EF-EXP-002, EF-EXP-008, EF-INT-024)."""

from dataclasses import dataclass, field
from typing import ClassVar

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.application.shelves import ShelfService
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.interfaces.http_api.plans import build_plans_router
from shelfhold.interfaces.http_api.shelves import build_shelves_router
from tests.auth_env import auth_env


@dataclass
class FakePreview:
    plan_id: str = "01JPLAN00000000000000000A1"
    base_version: ClassVar[int] = 4242
    ops: tuple[object, ...] = ()
    blocked: tuple[object, ...] = ()
    warnings: tuple[object, ...] = ()
    checks: tuple[str, ...] = ("sentinel",)


@dataclass
class FakePipeline:
    stale: bool = False
    applied: list[tuple[str, int]] = field(default_factory=list)

    def preview(self, shelf_destination_id: str) -> FakePreview:
        return FakePreview()

    def apply(self, plan_id: str, *, if_match: int, job_id: str) -> None:
        if self.stale or if_match != FakePreview.base_version:
            raise StalePlanError("the preview changed: recompute it")
        self.applied.append((plan_id, if_match))


@pytest.mark.req("EF-EXP-002", "EF-INT-024")
def test_shelf_route_refuses_below_18_and_creates_an_adult_shelf(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    router = build_shelves_router(lambda: ShelfService(create_db_engine(settings.database_path)))
    with auth_env(monkeypatch, settings, routers=[router]) as api:
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
    with auth_env(monkeypatch, settings, routers=[build_plans_router(lambda: pipeline)]) as api:
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
        assert api.post(url, headers={"If-Match": '"4242"'}).status_code == 200
        assert pipeline.applied == [(FakePreview.plan_id, 4242)]
        pipeline.stale = True  # it moved while applying: same answer, same code
        again = api.post(url, headers={"If-Match": '"4242"'})
        assert (again.status_code, again.json()["code"]) == (412, "plan.stale")
