"""A small API with the L11 conventions wired on, over a real SQLite database (no mocks).

The routes are throwaway: they exist to exercise problems, pagination, idempotency, `If-Match`
and 202 through the real application factory.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Header, Response
from fastapi.testclient import TestClient
from pydantic import BaseModel

from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.idempotency_store import IdempotencyStore
from shelfhold.infrastructure.schema import metadata
from shelfhold.interfaces.http_api.accepted import JobAccepted, accepted_job
from shelfhold.interfaces.http_api.app import create_http_app
from shelfhold.interfaces.http_api.pagination import Page, PageParams, make_page, page_params
from shelfhold.interfaces.http_api.preconditions import check_if_match, etag_for_version

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
    exports_started: list[str] = field(default_factory=list)
    boom_calls: list[int] = field(default_factory=list)


def build_env(tmp_path: Path, *, extra: APIRouter | None = None) -> ApiEnv:
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
    def list_things(params: PageParams = Depends(page_params)) -> Page[Thing]:  # noqa: B008
        after = params.after[0] if params.after else 0
        rows = [t for t in sorted(things.values(), key=lambda t: t.id) if t.id > int(after)]
        return make_page(rows[: params.limit + 1], params.limit, lambda t: (t.id,))

    @router.get("/things/{thing_id}")
    def get_thing(thing_id: int, response: Response) -> Thing:
        thing = things[thing_id]
        response.headers["ETag"] = etag_for_version(thing.version)
        return thing

    @router.put("/things/{thing_id}")
    def rename_thing(
        thing_id: int, body: NewThing, if_match: str | None = Header(default=None)
    ) -> Thing:
        thing = things[thing_id]
        check_if_match(if_match, etag_for_version(thing.version))
        things[thing_id] = thing = Thing(id=thing_id, name=body.name, version=thing.version + 1)
        return thing

    @router.post("/exports", status_code=202, response_model=JobAccepted)
    def start_export() -> Response:
        job_id = f"01JEXPORT{len(env_state['env'].exports_started):017d}"
        env_state["env"].exports_started.append(job_id)
        return accepted_job(job_id)

    @router.get("/boom")
    def boom() -> None:
        raise RuntimeError("/data/secret/path leaked sqlite3.OperationalError")

    @router.post("/boom-post")
    def boom_post() -> None:
        env_state["env"].boom_calls.append(1)
        raise RuntimeError("sqlite3.OperationalError at /data/x")

    app = create_http_app(
        health=_Health(),
        on_startup=lambda: None,
        version="9.9.9",
        routers=[router, *([extra] if extra else [])],
        idempotency=IdempotencyStore(engine, clock=lambda: clock[0]),
    )
    env = ApiEnv(client=TestClient(app, raise_server_exceptions=False), clock=clock)
    env_state["env"] = env
    return env
