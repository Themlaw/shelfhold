"""`GET /metrics` (ENF-OBS-002): Prometheus text, off unless the host turns it on, and then read
only with a key of scope `metrics`.

The order of the answers is the point: no credential is 401; a session of anyone (the admin's too),
a device token and a key of any other scope are 403 whatever the host chose, so the route tells
nothing about itself to them; only the right key then learns that the route is off (404). The
content is built by `ops.metrics`, which has no label that could name a title or a person.
"""

from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy import Engine

from shelfhold.identity.public import Credential, Principal
from shelfhold.interfaces.http_api.auth import require_principal
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.ops.metrics import CONTENT_TYPE, scrape

__all__ = ["build_metrics_router", "require_metrics_key"]


def require_metrics_key(
    principal: Annotated[Principal, Depends(require_principal)],
) -> Principal:
    """A key whose one scope is `metrics`: no session, no device token, no other key."""
    if principal.credential is not Credential.API_KEY or tuple(principal.scopes) != ("metrics",):
        raise ProblemError("credential_not_allowed")
    return principal


def build_metrics_router(
    get_engine: Callable[[], Engine], *, enabled: bool, version: str
) -> APIRouter:
    router = APIRouter(tags=["metrics"])

    @router.get(
        "/metrics", response_class=Response, responses={200: {"content": {"text/plain": {}}}}
    )
    def metrics(_key: Annotated[Principal, Depends(require_metrics_key)]) -> Any:
        if not enabled:
            raise ProblemError("not_found")
        return Response(
            scrape(get_engine(), version=version),
            media_type=CONTENT_TYPE,
            headers={"Cache-Control": "no-store"},
        )

    return router
