"""`/api/v1/profiles`: the household profiles (EF-USR-001, EF-USR-005, ENF-SEC-008).

Creating, changing and removing a profile, and setting its policy per source, are **reserved
actions** (`profile.*`): an admin session proven within `fresh_auth_s` in every profile, refused to
every key and token. The guard answers first, before the id is even looked at, so that a caller who
may not manage cannot tell a real id from an invented one (IDOR). The service decides the rest
(`identity/application/profiles.py`): no role escalation, no child without an age ceiling, no right
to classify for a child, the last admin stays.

Reading the list and the policies is for the admin's session too, and nothing here carries a
password or its hash.

`visibility` (EF-USR-016, M3 L09): `PUT /api/v1/profiles/{id}/visibility` is the same management
action (audited when it changes what holds), and `GET`/`PUT /api/v1/me/visibility` is the caller's
own, session only: a key or a token is a program, it has no visibility to choose.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints

from shelfhold.catalog.public import MAX_TERM_CHARS, MAX_TERMS, clean_terms
from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    Principal,
    ProfileError,
    ProfileRecord,
    SourcePolicyRecord,
    effective_visibility,
)
from shelfhold.interfaces.http_api.auth import (
    gate_of,
    require_principal,
    require_session,
    reserved_action,
)
from shelfhold.interfaces.http_api.problems import ProblemError

PROFILES_PATH = "/api/v1/profiles"
_CREATE = reserved_action(RESERVED_ACTIONS["profile.create"])
_UPDATE = reserved_action(RESERVED_ACTIONS["profile.update"])
_REMOVE = reserved_action(RESERVED_ACTIONS["profile.remove"])
_POLICY = reserved_action(RESERVED_ACTIONS["profile.source_policy"])
_CONFLICTS = frozenset(
    {
        "username_taken", "too_many_admins", "last_admin", "cannot_remove_self",
        "cannot_change_own_role", "changed_meanwhile",
    }
)  # fmt: skip


class ProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)
    role: str = Field(min_length=1, max_length=16)
    max_age_rating: StrictInt | None = None
    can_rate: bool | None = None


class ProfilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str | None = Field(default=None, min_length=1, max_length=16)
    max_age_rating: StrictInt | None = None
    can_rate: bool | None = None


class PolicyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: str = Field(min_length=1, max_length=32)


class VisibilityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visibility: Literal["private", "household"]


class FamilySearchIn(BaseModel):
    """The family search of a child profile (EF-USR-018): both fields are given, so a call never
    changes one by leaving the other out; the words are bounded (and folded when stored)."""

    model_config = ConfigDict(extra="forbid")
    hide_unrated_remote: StrictBool
    subject_filter: list[Annotated[str, StringConstraints(max_length=MAX_TERM_CHARS)]] = Field(
        max_length=MAX_TERMS
    )


class FamilySearchOut(BaseModel):
    hide_unrated_remote: bool
    subject_filter: list[str]


class VisibilityOut(BaseModel):
    visibility: str  # what holds now: the choice, else the default of the role


class ProfileOut(BaseModel):
    id: str
    username: str
    role: str
    max_age_rating: int | None
    can_rate: bool
    visibility: str  # what holds now (EF-USR-016): `private` for an adult, `household` for a child
    hide_unrated_remote: bool  # EF-USR-018: a child hides the unrated remote results (default)
    subject_filter: list[str]  # the words this profile adds to the default list


class ProfileList(BaseModel):
    items: list[ProfileOut]


class PolicyOut(BaseModel):
    source_key: str
    policy: str  # the stored choice
    effective: str  # what holds now: a child on a source that is not family has "forbidden"


class PolicyList(BaseModel):
    items: list[PolicyOut]


def _out(record: ProfileRecord) -> ProfileOut:
    return ProfileOut(
        id=record.id,
        username=record.username,
        role=record.role,
        max_age_rating=record.max_age_rating,
        can_rate=record.can_rate,
        visibility=effective_visibility(record.role, record.visibility),
        hide_unrated_remote=record.hide_unrated_remote,
        subject_filter=list(record.subject_filter),
    )


def _problem(error: ProfileError) -> ProblemError:
    code = error.code
    if code == "not_allowed":
        return ProblemError("forbidden")
    if code == "not_found":
        return ProblemError("profile_not_found")
    kind = "profile_conflict" if code in _CONFLICTS else "profile_refused"
    return ProblemError(kind, extensions={"reason": code})


def _manager(request: Request) -> Principal:
    """The caller of a read: a session of an admin (the service checks it again)."""
    return require_principal(request)


def build_profiles_router() -> APIRouter:
    router = APIRouter(prefix=PROFILES_PATH, tags=["profiles"])

    def run(request: Request, call: Any) -> Any:
        try:
            return call(gate_of(request).auth)
        except ProfileError as error:
            raise _problem(error) from None
        except ValueError as error:  # a username or a password with no acceptable shape
            raise ProblemError("profile_refused", extensions={"reason": str(error)}) from None

    @router.get("", response_model=ProfileList)
    def list_profiles(request: Request, principal: Annotated[Principal, Depends(_manager)]) -> Any:
        records = run(request, lambda auth: auth.profile_list(by=principal))
        return ProfileList(items=[_out(r) for r in records])

    @router.post("", status_code=201, response_model=ProfileOut)
    def create_profile(
        request: Request, body: ProfileIn, principal: Annotated[Principal, Depends(_CREATE)]
    ) -> Any:
        def create(auth: Any) -> ProfileRecord:
            created: ProfileRecord = auth.profile_add_record(
                body.username, body.password, body.role, max_age_rating=body.max_age_rating,
                can_rate=body.can_rate, by=principal,
            )  # fmt: skip
            return created  # answered from what was written, never from a second, guarded read

        return _out(run(request, create))

    @router.patch("/{profile_id}", response_model=ProfileOut)
    def update_profile(
        request: Request,
        profile_id: str,
        body: ProfilePatch,
        principal: Annotated[Principal, Depends(_UPDATE)],
    ) -> Any:
        record = run(
            request,
            lambda auth: auth.profile_set(
                profile_id,
                role=body.role,
                max_age_rating=body.max_age_rating,
                can_rate=body.can_rate,
                by=principal,
            ),
        )
        return _out(record)

    @router.put("/{profile_id}/visibility", response_model=VisibilityOut)
    def set_visibility(
        request: Request,
        profile_id: str,
        body: VisibilityIn,
        principal: Annotated[Principal, Depends(_UPDATE)],
    ) -> Any:
        chosen = run(
            request,
            lambda auth: auth.profile_set_visibility(profile_id, body.visibility, by=principal),
        )
        return VisibilityOut(visibility=chosen)

    @router.put("/{profile_id}/family-search", response_model=FamilySearchOut)
    def set_family_search(
        request: Request,
        profile_id: str,
        body: FamilySearchIn,
        principal: Annotated[Principal, Depends(_UPDATE)],
    ) -> Any:
        saved = run(
            request,
            lambda auth: auth.profile_set_family_search(
                profile_id, body.hide_unrated_remote, clean_terms(body.subject_filter), by=principal
            ),
        )
        return FamilySearchOut(
            hide_unrated_remote=saved.hide_unrated_remote, subject_filter=list(saved.subject_filter)
        )

    @router.delete("/{profile_id}", status_code=204)
    def remove_profile(
        request: Request, profile_id: str, principal: Annotated[Principal, Depends(_REMOVE)]
    ) -> Response:
        run(request, lambda auth: auth.profile_remove(profile_id, by=principal))
        return Response(status_code=204)

    @router.get("/{profile_id}/source-policies", response_model=PolicyList)
    def source_policies(
        request: Request, profile_id: str, principal: Annotated[Principal, Depends(_manager)]
    ) -> Any:
        records = run(request, lambda auth: auth.profile_policies(profile_id, by=principal))
        return PolicyList(
            items=[PolicyOut(source_key=r.source_key, policy=r.policy, effective=r.effective)
                   for r in records]
        )  # fmt: skip

    @router.put("/{profile_id}/source-policies/{source_key}", response_model=PolicyOut)
    def set_source_policy(
        request: Request,
        profile_id: str,
        source_key: str,
        body: PolicyIn,
        principal: Annotated[Principal, Depends(_POLICY)],
    ) -> Any:
        def put(auth: Any) -> PolicyOut:
            written: SourcePolicyRecord = auth.profile_policy_set(
                profile_id, source_key, body.policy, by=principal
            )  # the row the store wrote, with the policy that holds
            return PolicyOut(
                source_key=written.source_key, policy=written.policy, effective=written.effective
            )

        return run(request, put)

    return router


def build_own_visibility_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/me", tags=["profiles"])

    @router.get("/visibility", response_model=VisibilityOut)
    def get_own_visibility(
        request: Request, principal: Annotated[Principal, Depends(require_session)]
    ) -> Any:
        return VisibilityOut(visibility=gate_of(request).auth.visibility_of(principal.user_id))

    @router.put("/visibility", response_model=VisibilityOut)
    def put_own_visibility(
        request: Request,
        body: VisibilityIn,
        principal: Annotated[Principal, Depends(require_session)],
    ) -> Any:
        try:
            chosen = gate_of(request).auth.set_own_visibility(principal, body.visibility)
        except ProfileError as error:
            raise _problem(error) from None
        return VisibilityOut(visibility=chosen)

    return router
