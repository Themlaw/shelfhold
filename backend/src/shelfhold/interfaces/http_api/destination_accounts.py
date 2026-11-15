"""`/api/v1/profiles/{user_id}/destination-accounts` (M4 L12, EF-USR-012, D8): a member's own Kavita
key, per destination: link or rotate, list, revoke.

Pasting a key is a reserved action (`destination_account.bind`: a fresh session in every profile,
never an API key, never a device token); so is revoking one. An adult handles their own; a parent
(an admin) enters a child's, for the account declared in the profile; the admin revokes any and
reads none. The key is in the request body once and nowhere else: the answers carry its
fingerprint, and the profile of another answers 404, like one that is not there.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    AccountView,
    DestinationAccountError,
    Principal,
)
from shelfhold.interfaces.http_api.auth import require_session, reserved_action
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.ops import OpsServices

__all__ = ["build_destination_accounts_router"]

_ADULTS = ("admin", "member")
# the guard writes no line: the service writes it, with its target, in the transaction that
# changes the account (a refusal or a key that did not verify leaves none)
_BIND = reserved_action(RESERVED_ACTIONS["destination_account.bind"], roles=_ADULTS, audit=False)
_REVOKE = reserved_action(
    RESERVED_ACTIONS["destination_account.revoke"], roles=_ADULTS, audit=False
)
_CONFLICTS = frozenset({"remote_user_taken"})


class LinkIn(BaseModel):
    """The key and, for an adult, the name of the Kavita account it opens. Nothing else: a child's
    account is the one her parent declared."""

    model_config = ConfigDict(extra="forbid")
    token: StrictStr = Field(min_length=1, max_length=512)
    remote_user: StrictStr | None = Field(default=None, max_length=128)


class AccountOut(BaseModel):
    id: str
    user_id: str
    shelf_destination_id: str
    remote_user: str | None
    token_fingerprint: str | None
    verified_at: datetime | None
    revoked_at: datetime | None


class AccountList(BaseModel):
    items: list[AccountOut]


def _out(view: AccountView) -> AccountOut:
    return AccountOut(
        id=view.id,
        user_id=view.user_id,
        shelf_destination_id=view.shelf_destination_id,
        remote_user=view.remote_user,
        token_fingerprint=view.token_fingerprint,
        verified_at=view.verified_at,
        revoked_at=view.revoked_at,
    )


def _problem(error: DestinationAccountError) -> ProblemError:
    code = error.code
    if code == "not_allowed":
        return ProblemError("forbidden")
    if code == "not_found":
        return ProblemError("destination_account_not_found")
    kind = "destination_account_conflict" if code in _CONFLICTS else "destination_account_refused"
    return ProblemError(kind, extensions={"reason": code})


def _adult(principal: Annotated[Principal, Depends(require_session)]) -> Principal:
    """A session of an admin or a member: a child manages no account, a key none at all."""
    if principal.role not in _ADULTS:
        raise ProblemError("forbidden")
    return principal


def build_destination_accounts_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/profiles", tags=["destination-accounts"])

    def run[T](call: Callable[[], T]) -> T:
        try:
            return call()
        except DestinationAccountError as error:
            raise _problem(error) from None

    @router.get("/{user_id}/destination-accounts", response_model=AccountList)
    def list_accounts(user_id: str, principal: Annotated[Principal, Depends(_adult)]) -> Any:
        views = run(lambda: get_ops().destination_accounts.list_accounts(principal, user_id))
        return AccountList(items=[_out(v) for v in views])

    @router.put("/{user_id}/destination-accounts/{shelf_destination_id}", response_model=AccountOut)
    def link_account(
        user_id: str,
        shelf_destination_id: str,
        body: LinkIn,
        principal: Annotated[Principal, Depends(_BIND)],
    ) -> Any:
        view = run(
            lambda: get_ops().destination_accounts.bind(
                principal, user_id, shelf_destination_id,
                token=body.token, remote_user=body.remote_user,
            )
        )  # fmt: skip
        return _out(view)

    @router.delete("/{user_id}/destination-accounts/{shelf_destination_id}", status_code=204)
    def revoke_account(
        user_id: str,
        shelf_destination_id: str,
        principal: Annotated[Principal, Depends(_REVOKE)],
    ) -> Response:
        run(lambda: get_ops().destination_accounts.revoke(principal, user_id, shelf_destination_id))
        return Response(status_code=204)

    return router
