"""`/api/v1/webhooks` (M4 L24, EF-INT-005): register, list, read, remove and rotate a webhook.

Registering one sends the household's events to an address outside the house and hands out a
secret: it is a reserved action (`webhook.manage`, a fresh session of an administrator, never a
key). Turning the secret (M6 L23a) is the same action, with the same guard. Reading is the
administrator's alone (a session, or a key of scope `admin` that is not read-only); a child, a
member, a read-only key and nobody are refused before anything is read, so a subscription that
exists and one that does not give one answer.

The secret is in the answer to the creation, and in the answer to the rotation, once each. No
other answer carries it, and what is shown of the address is its host.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.integrations.webhooks import (
    Subscription,
    SubscriptionUnreadable,
    WebhookAdmin,
    WebhookRefused,
    webhook_documentation,
)
from shelfhold.interfaces.http_api.auth import reserved_action
from shelfhold.interfaces.http_api.ops_dashboard import require_admin_surface
from shelfhold.interfaces.http_api.problems import ProblemError

__all__ = ["build_webhooks_router", "install_webhooks_openapi"]

# the guard writes no line: the service writes it, with the subscription, in its transaction
_MANAGE = reserved_action(RESERVED_ACTIONS["webhook.manage"], audit=False)
_Reader = Annotated[Principal, Depends(require_admin_surface)]
_Manager = Annotated[Principal, Depends(_MANAGE)]


class WebhookIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: StrictStr = Field(min_length=1, max_length=2000)
    event_types: list[StrictStr] = Field(min_length=1, max_length=64)
    allow_private: bool = False  # the administrator says this exact host and port may be private


class WebhookOut(BaseModel):
    id: str
    url_host: str
    event_types: list[str]
    private_target: str | None
    enabled: bool
    created_at: datetime


class WebhookCreated(WebhookOut):
    """The answer to the creation: the only one that carries the secret."""

    secret: str


class WebhookList(BaseModel):
    items: list[WebhookOut]


def _out(sub: Subscription) -> WebhookOut:
    return WebhookOut(
        id=sub.id, url_host=sub.url_host, event_types=list(sub.event_types),
        private_target=sub.private_target, enabled=sub.enabled, created_at=sub.created_at,
    )  # fmt: skip


def build_webhooks_router(admin: Callable[[], WebhookAdmin]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

    @router.get("")
    def list_webhooks(_principal: _Reader) -> WebhookList:
        return WebhookList(items=[_out(s) for s in admin().listing()])

    @router.post("", status_code=201)
    def create_webhook(body: WebhookIn, principal: _Manager, response: Response) -> WebhookCreated:
        try:
            created, secret = admin().create(
                principal, url=body.url, event_types=body.event_types,
                allow_private=body.allow_private,
            )  # fmt: skip
        except WebhookRefused as refused:
            if refused.reason == "event_types":
                raise ProblemError("webhook.event_types") from None
            raise ProblemError(
                "webhook.url_refused", extensions={"reason": refused.reason}
            ) from None
        response.headers["Cache-Control"] = "no-store"  # the one answer that carries the secret
        return WebhookCreated(**_out(created).model_dump(), secret=secret)

    @router.get("/{webhook_id}")
    def read_webhook(webhook_id: str, _principal: _Reader) -> WebhookOut:
        found = admin().get(webhook_id)
        if found is None:
            raise ProblemError("webhook.not_found")
        return _out(found)

    @router.delete("/{webhook_id}", status_code=204)
    def remove_webhook(webhook_id: str, principal: _Manager) -> Response:
        if not admin().delete(principal, webhook_id):
            raise ProblemError("webhook.not_found")
        return Response(status_code=204)

    @router.post("/{webhook_id}/rotate")
    def rotate_webhook(webhook_id: str, principal: _Manager, response: Response) -> WebhookCreated:
        """Turn the signing secret (M6 L23a): the same reserved action as the creation, and the
        same answer — the only other one that carries a secret, once."""
        try:
            turned = admin().rotate(principal, webhook_id)
        except SubscriptionUnreadable:
            raise ProblemError("webhook.secret_unreadable") from None
        if turned is None:
            raise ProblemError("webhook.not_found")
        response.headers["Cache-Control"] = "no-store"  # the one answer that carries the secret
        return WebhookCreated(**_out(turned[0]).model_dump(), secret=turned[1])

    return router


def install_webhooks_openapi(app: FastAPI) -> None:
    original = app.openapi

    def openapi() -> dict[str, Any]:
        schema = original()
        schema.setdefault("x-webhooks", webhook_documentation())
        return schema

    app.openapi = openapi  # type: ignore[method-assign]
