"""`/api/v1/audit/kavita-accounts` and `/api/v1/audit/komga-accounts`: the read-only audits of
the accounts (EF-EXP-036, EF-USR-017, M3 L10; EF-EXP-047, ENF-SEC-016, L22).

`GET` gives the last answer of each target; `POST` audits again now — for Komga it is the
`target.komga_audit` job the daily cron runs, run on demand (the proof of partitioning a
family binding waits for, L22). Both are the admin's (a session or a key of the admin: nothing
here changes a decision of the parental control, and the servers are only read). A child, a
member and an anonymous caller are refused. What comes back holds profile identifiers, closed
words and counts, never the name of an account, so the page can be shown as it is.

Naming the account of a child is another thing, and a reserved one: `PUT
/api/v1/profiles/{id}/kavita-account` (`profiles.py`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from shelfhold.identity.public import Principal
from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.interfaces.http_api.stats import require_admin
from shelfhold.ops import OpsServices

__all__ = ["AUDIT_PATH", "KOMGA_AUDIT_PATH", "build_audit_router", "build_komga_audit_router"]

AUDIT_PATH = "/api/v1/audit/kavita-accounts"
KOMGA_AUDIT_PATH = "/api/v1/audit/komga-accounts"
Admin = Annotated[Principal, Depends(require_admin)]


class FindingOut(BaseModel):
    code: str
    library: str | None


class ChildAuditOut(BaseModel):
    profile_id: str
    status: str  # ok, alert or unmapped
    findings: list[FindingOut]


class TargetAuditOut(BaseModel):
    target_id: str
    state: str  # never_audited, unverified, ok or alert
    alerts: int = 0
    unmapped: int = 0
    audited_at: str | None = None
    children: list[ChildAuditOut] = []


class AccountsAuditOut(BaseModel):
    targets: list[TargetAuditOut]


class KomgaAuditOut(BaseModel):
    target_id: str
    state: str  # never_audited, unverified, ok or alert
    reason: str | None = None  # the closed code of the last audit, when it proved nothing
    proved: int = 0  # the folders it proved
    audited_at: str | None = None
    version: str | None = None


class KomgaAccountsAuditOut(BaseModel):
    targets: list[KomgaAuditOut]


def build_audit_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    router = APIRouter(prefix=AUDIT_PATH, tags=["audit"])

    @router.get("", response_model=AccountsAuditOut)
    def last_audit(_admin: Admin) -> AccountsAuditOut:
        with refusals():
            return AccountsAuditOut(targets=get_ops().kavita_accounts.entries())  # type: ignore[arg-type]

    @router.post("", response_model=AccountsAuditOut)
    def audit_now(_admin: Admin) -> AccountsAuditOut:
        with refusals():
            return AccountsAuditOut(targets=get_ops().kavita_accounts.run())  # type: ignore[arg-type]

    return router


def build_komga_audit_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    """The Komga twin: the `target.komga_audit` job on demand, for the admin alone (L22)."""

    router = APIRouter(prefix=KOMGA_AUDIT_PATH, tags=["audit"])

    @router.get("", response_model=KomgaAccountsAuditOut)
    def last_audit(_admin: Admin) -> KomgaAccountsAuditOut:
        with refusals():
            return KomgaAccountsAuditOut(targets=get_ops().komga_accounts.entries())  # type: ignore[arg-type]

    @router.post("", response_model=KomgaAccountsAuditOut)
    def audit_now(_admin: Admin) -> KomgaAccountsAuditOut:
        with refusals():
            return KomgaAccountsAuditOut(targets=get_ops().komga_accounts.run())  # type: ignore[arg-type]

    return router
