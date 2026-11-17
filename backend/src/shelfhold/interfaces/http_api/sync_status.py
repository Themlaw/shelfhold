"""`GET /api/v1/sync-status` (M4 L33b, EF-EXP-026): the sync of progress, member by member.

For each member and each Kavita binding open to them: is a key linked (`linked`, or `no_token`),
and when the last pull and the last push were. A member reads their own rows; an admin reads every
member's. The route takes no profile: there is no identifier to swap for another's. A child and an
API key read nothing (a session of an adult only, as the key screen). No key, no fingerprint and no
Kavita account name is in the answer.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from shelfhold.identity.public import DestinationAccountError, Principal
from shelfhold.interfaces.http_api.auth import require_session
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.ops import OpsServices

__all__ = ["build_sync_status_router"]


class SyncStatusOut(BaseModel):
    user_id: str
    username: str
    shelf_destination_id: str
    shelf_name: str
    target_name: str
    state: Literal["linked", "no_token"]
    last_pull_at: datetime | None
    last_push_at: datetime | None


class SyncStatusList(BaseModel):
    items: list[SyncStatusOut]


def build_sync_status_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/sync-status", tags=["sync-status"])

    @router.get("", response_model=SyncStatusList)
    def read_sync_status(principal: Annotated[Principal, Depends(require_session)]) -> Any:
        try:
            rows = get_ops().destination_accounts.sync_status(principal)
        except DestinationAccountError:
            raise ProblemError("forbidden") from None
        return SyncStatusList(
            items=[
                SyncStatusOut(
                    user_id=r.user_id, username=r.username,
                    shelf_destination_id=r.shelf_destination_id, shelf_name=r.shelf_name,
                    target_name=r.target_name, state=r.state,  # type: ignore[arg-type]
                    last_pull_at=r.last_pull_at, last_push_at=r.last_push_at,
                )
                for r in rows
            ]
        )  # fmt: skip

    return router
