"""`/api/v1/profiles`: the household profiles (EF-USR-001, EF-USR-005, ENF-SEC-008).

Creating, changing and removing a profile, and setting its policy per source, are **reserved
actions** (`profile.*`): an admin session proven within `fresh_auth_s` in every profile, refused to
every key and token. The guard answers first, before the id is even looked at, so that a caller who
may not manage cannot tell a real id from an invented one (IDOR). The service decides the rest
(`identity/application/profiles.py`): no role escalation, no child without an age ceiling, no right
to classify for a child, the last admin stays.

Reading the list and the policies is for the admin's session too, and nothing here carries a
password or its hash.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    Principal,
    ProfileError,
    ProfileRecord,
    SourcePolicyRecord,
)
from shelfhold.interfaces.http_api.auth import gate_of, require_principal, reserved_action
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


class ProfileOut(BaseModel):
    id: str
    username: str
    role: str
    max_age_rating: int | None
    can_rate: bool


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
