"""A small API with the L11 conventions wired on, over a real SQLite database (no mocks).

The routes are throwaway: they exist to exercise problems and pagination through the real
application factory. Idempotency, `If-Match` and 202 are proved on the real routes of `create_app()`
(`tests/integration/test_l51_plan_jobs.py`).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from pydantic import BaseModel

from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.idempotency_store import IdempotencyStore
from shelfhold.infrastructure.schema import metadata
from shelfhold.interfaces.http_api.app import create_http_app
from shelfhold.interfaces.http_api.pagination import Page, PageParams, make_page, page_params_for

T0 = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)


class Thing(BaseModel):
    id: int
    name: str
    version: int


class NewThing(BaseModel):
    name: str


class _Health:
    def migration_progress(self) -> tuple[int, int] | None:
        return None

    def readiness(self) -> Mapping[str, bool]:
        return {}


@dataclass
class ApiEnv:
    client: TestClient
    clock: list[datetime]
    created: list[Thing] = field(default_factory=list)


def build_env(
    tmp_path: Path, *, extra: APIRouter | None = None, frontend_dir: Path | None = None
) -> ApiEnv:
    engine = create_db_engine(tmp_path / "api.db")
    metadata.create_all(engine)
    clock = [T0]
    env_state: dict[str, ApiEnv] = {}
    router = APIRouter(prefix="/api/v1")
    things: dict[int, Thing] = {
        n: Thing(id=n, name=f"thing-{n:03d}", version=1) for n in range(1, 8)
    }

    @router.post("/things", status_code=201)
    def create_thing(body: NewThing) -> Thing:
        thing = Thing(id=len(things) + 1, name=body.name, version=1)
        things[thing.id] = thing
        env_state["env"].created.append(thing)
        return thing

    @router.get("/things")
    def list_things(params: PageParams = Depends(page_params_for(int))) -> Page[Thing]:  # noqa: B008
        after = params.after[0] if params.after else 0
        rows = [t for t in sorted(things.values(), key=lambda t: t.id) if t.id > int(after)]
        return make_page(rows[: params.limit + 1], params.limit, lambda t: (t.id,))

    @router.get("/boom")
    def boom() -> None:
        raise RuntimeError("/data/secret/path leaked sqlite3.OperationalError")

    app = create_http_app(
        health=_Health(),
        on_startup=lambda: None,
        version="9.9.9",
        frontend_dir=frontend_dir,
        routers=[router, *([extra] if extra else [])],
        idempotency=IdempotencyStore(engine, clock=lambda: clock[0]),
        actor_of=lambda scope: dict(scope["headers"]).get(b"x-test-actor", b"tester").decode(),
    )
    env = ApiEnv(client=TestClient(app, raise_server_exceptions=False), clock=clock)
    env_state["env"] = env
    return env
