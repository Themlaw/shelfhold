"""EF-INT-001 (volet coupable M1a): `ETag` and `?since=` on lists."""

from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, Request, Response

from shelfhold.interfaces.http_api.lists import (
    Since,
    conditional_json,
    encode_since,
    since_param,
)
from tests.api_env import build_env

RETENTION_FLOOR = 40  # events up to this sequence number have been purged
CURRENT = {"seq": 100}


def _router() -> APIRouter:
    router = APIRouter(prefix="/api/v1")

    @router.get("/changes")
    def changes(request: Request, since: Since | None = Depends(since_param)) -> Response:  # noqa: B008
        if since is not None:
            since.require_not_older_than(RETENTION_FLOOR)
        after = since.seq if since else 0
        items = [n for n in range(after + 1, CURRENT["seq"] + 1, 30)]
        return conditional_json(request, {"items": items, "since": encode_since(CURRENT["seq"])})

    return router


@pytest.mark.req("EF-INT-001")
def test_list_etag_and_since(tmp_path: Path) -> None:
    env = build_env(tmp_path, extra=_router())
    first = env.client.get("/api/v1/changes")
    assert first.status_code == 200
    etag = first.headers["etag"]
    token = first.json()["since"]
    assert token.isascii() and not token.isdigit()  # opaque, not a bare sequence number

    unchanged = env.client.get("/api/v1/changes", headers={"If-None-Match": etag})
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["etag"] == etag
    assert (
        env.client.get("/api/v1/changes", headers={"If-None-Match": f"W/{etag}"}).status_code == 304
    )
    assert env.client.get("/api/v1/changes", headers={"If-None-Match": "*"}).status_code == 304

    CURRENT["seq"] = 130
    try:
        changed = env.client.get("/api/v1/changes", headers={"If-None-Match": etag})
        assert changed.status_code == 200
        assert changed.headers["etag"] != etag

        since = env.client.get("/api/v1/changes", params={"since": token})
        assert since.status_code == 200
        assert since.json()["items"] == [101]  # only what happened after the token
    finally:
        CURRENT["seq"] = 100


@pytest.mark.req("EF-INT-001")
def test_since_token_too_old_returns_resync_required(tmp_path: Path) -> None:
    env = build_env(tmp_path, extra=_router())
    old = env.client.get("/api/v1/changes", params={"since": encode_since(RETENTION_FLOOR - 1)})
    assert old.status_code == 410
    assert old.json()["type"] == "/api/v1/problems/resync-required"
    edge = env.client.get("/api/v1/changes", params={"since": encode_since(RETENTION_FLOOR)})
    assert edge.status_code == 200


@pytest.mark.req("EF-INT-001")
@pytest.mark.parametrize(
    "token", ["", "100", "s1.", "s1.!!!", "s2.MTAw", "s1.LTU", "x" * 300, encode_since(2**63)]
)
def test_since_token_malformed_is_a_400_problem(tmp_path: Path, token: str) -> None:
    env = build_env(tmp_path, extra=_router())
    response = env.client.get("/api/v1/changes", params={"since": token})
    assert response.status_code == 400
    assert response.json()["type"] == "/api/v1/problems/invalid-cursor"
