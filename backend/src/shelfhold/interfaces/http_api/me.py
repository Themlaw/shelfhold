"""`/api/v1/me`: who is signed in and the navigation their role gets (EF-UX-005), the state of the
user (EF-USR-007) and their languages (EF-BIB-022).

There is no `{user_id}` anywhere: the user is the one of the session, so nobody can name another
(IDOR, ENF-SEC-010). Keys and device tokens are refused (`require_session`). A Work above a child's
ceiling, unrated for a child, or absent, is answered by the one 404 of an absent Work, on every
route, so that marking a Work never tells whether it exists. The ceiling is read from the database
on each call. The user's own clock is never an ordering key; a claim from the future is a 422.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import AwareDatetime, BaseModel, ConfigDict

from shelfhold.identity.application.user_state import (
    NOT_FOUND,
    UserStateService,
    WorkState,
)
from shelfhold.identity.domain.user_state import UserStateError
from shelfhold.identity.public import Principal
from shelfhold.interfaces.http_api.auth import require_session
from shelfhold.interfaces.http_api.problems import ProblemError

__all__ = ["build_me_router"]

Session = Annotated[Principal, Depends(require_session)]


def navigation_for(role: str) -> list[str]:
    """The destinations a role is offered (EF-UX-005), decided here and not by the screen: the
    admin ones go to the `admin` role only, and a role this code does not know gets none."""
    family = ["home", "search", "requests"]
    if role == "admin":
        return [*family, "activity", "settings"]
    if role in ("member", "child"):
        return family
    return []


class MeOut(BaseModel):
    id: str
    username: str
    role: str
    nav: list[str]  # destination ids, in display order (the screen maps them to labels)


class StateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    followed: bool | None = None
    favorite: bool | None = None
    read: bool | None = None
    client_ts: AwareDatetime | None = None  # a claim: checked against the server's clock, unused


class StateOut(BaseModel):
    work_id: str
    followed: bool
    favorite: bool
    read_at: datetime | None
    stamp: datetime | None  # the server's instant of the last write; null: never touched


class StateList(BaseModel):
    items: list[StateOut]


class LanguagesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    languages: Any  # checked by the domain: a list of language tags, a clear 422 otherwise


class LanguagesOut(BaseModel):
    languages: list[str]


class TitleOut(BaseModel):
    work_id: str
    title: str
    lang: str | None  # the language of the title chosen; null: the Work's own title


def _out(state: WorkState) -> StateOut:
    flags = state.flags.value if state.flags else None
    return StateOut(
        work_id=state.work_id,
        followed=bool(flags and flags.followed),
        favorite=bool(flags and flags.favorite),
        read_at=flags.read_at if flags else None,
        stamp=state.stamp,
    )


def _problem(error: UserStateError) -> ProblemError:
    if error.code == NOT_FOUND:
        return ProblemError("not_found")
    if error.code == "not_allowed":
        return ProblemError("credential_not_allowed")
    return ProblemError("user_state_refused", extensions={"reason": error.code})


def build_me_router(get_service: Callable[[], UserStateService]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/me", tags=["me"])

    def run[T](call: Callable[[UserStateService], T]) -> T:
        try:
            return call(get_service())
        except UserStateError as error:
            raise _problem(error) from None

    @router.get("", response_model=MeOut)
    def me(principal: Session) -> MeOut:
        return MeOut(
            id=principal.user_id,
            username=principal.username,
            role=principal.role,
            nav=navigation_for(principal.role),
        )

    @router.get("/state", response_model=StateList)
    def list_state(principal: Session) -> Any:
        return StateList(items=[_out(s) for s in run(lambda s: s.items(principal))])

    @router.get("/state/{work_id}", response_model=StateOut)
    def get_state(work_id: str, principal: Session) -> Any:
        return _out(run(lambda s: s.get(principal, work_id)))

    @router.put("/state/{work_id}", response_model=StateOut)
    def put_state(work_id: str, body: StateIn, principal: Session) -> Any:
        return _out(
            run(
                lambda s: s.set(
                    principal, work_id, followed=body.followed, favorite=body.favorite,
                    read=body.read, client_ts=body.client_ts,
                )
            )
        )  # fmt: skip

    @router.get("/title-languages", response_model=LanguagesOut)
    def get_languages(principal: Session) -> Any:
        return LanguagesOut(languages=run(lambda s: s.languages(principal)))

    @router.put("/title-languages", response_model=LanguagesOut)
    def put_languages(body: LanguagesIn, principal: Session) -> Any:
        return LanguagesOut(languages=run(lambda s: s.set_languages(principal, body.languages)))

    @router.get("/works/{work_id}/title", response_model=TitleOut)
    def get_title(work_id: str, principal: Session) -> Any:
        choice = run(lambda s: s.title(principal, work_id))
        return TitleOut(work_id=choice.work_id, title=choice.title, lang=choice.lang)

    return router
