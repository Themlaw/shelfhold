"""« Soutenir l'auteur » (M4 L23, EF-BIB-032): the official links of an Edition and the proposal
to pause a community binding.

The read is open to every profile, through the one filter of the catalogue: an Edition a reader
may not see is exactly an Edition that does not exist (the same 404, by session and by key, for a
child and for a guessed identifier). The links come out cleaned (https only, never affiliated),
each with the provider that gave it and when. The proposal to pause a binding is told to an admin
alone; doing it is a separate write, the admin's, and refuses any binding that was not proposed.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Engine

from shelfhold.catalog.public import pause_community_binding_of, support_author
from shelfhold.identity.public import Principal, reader_scope
from shelfhold.interfaces.http_api.auth import holds_admin_power, require_principal
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.interfaces.http_api.stats import require_admin

__all__ = ["build_support_author_router"]


class OfficialLinkOut(BaseModel):
    url: str
    kind: Literal["official_read", "buy", "licensed_in"]
    lang: str
    provider: str
    fetched_at: datetime | None


class SupportAuthorOut(BaseModel):
    edition_id: str
    links: list[OfficialLinkOut]
    pause_suggestions: list[str]  # binding ids: only an admin is told, nothing is paused by it


class BindingPausedOut(BaseModel):
    edition_id: str
    binding_id: str
    paused: Literal[True]


def build_support_author_router(get_engine: Callable[[], Engine]) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["support-author"])

    @router.get("/editions/{edition_id}/support-author")
    def support_author_read(
        edition_id: str, principal: Annotated[Principal, Depends(require_principal)]
    ) -> SupportAuthorOut:
        with get_engine().connect() as connection:
            found = support_author(
                connection,
                reader_scope(connection, principal.user_id),
                edition_id,
                is_admin=holds_admin_power(principal),
            )
        if found is None:
            raise ProblemError("not_found")
        return SupportAuthorOut(
            edition_id=found.edition_id,
            links=[
                OfficialLinkOut(
                    url=link.url, kind=link.kind, lang=link.lang,  # type: ignore[arg-type]
                    provider=link.provider, fetched_at=link.fetched_at,
                )
                for link in found.links
            ],
            pause_suggestions=found.pause_suggestions,
        )  # fmt: skip

    @router.post("/editions/{edition_id}/bindings/{binding_id}/pause")
    def support_author_pause(
        edition_id: str, binding_id: str, _admin: Annotated[Principal, Depends(require_admin)]
    ) -> BindingPausedOut:
        if not pause_community_binding_of(get_engine(), edition_id, binding_id):
            raise ProblemError("not_found")
        return BindingPausedOut(edition_id=edition_id, binding_id=binding_id, paused=True)

    return router
