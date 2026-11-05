"""The routes that leave the waiting states (EF-INT-023), one per exit command of the catalogue.

Each route is the twin of a CLI command and calls the same service. The reserved ones (a secret, a
parental-control release, the lost keyring, a deletion confirmed after a restore) declare their
action, so a fresh session or the local CLI is needed and every API key is refused
(`credential-not-allowed`); the conditional ones decide from the state of the thing they act on.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from shelfhold.export.domain.bindings import BindingError
from shelfhold.headless import HeadlessError
from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.interfaces.http_api.auth import reserved_action
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.interfaces.http_api.stats import require_admin
from shelfhold.ops import ENDPOINTS, OpsError, OpsServices, user_of

__all__ = ["build_ops_router"]

_ROTATE_KEY = reserved_action(RESERVED_ACTIONS["target.rotate_key"])
_SET_SECRET = reserved_action(RESERVED_ACTIONS["source.set_secret"])
_LOST_KEYRING = reserved_action(RESERVED_ACTIONS["tombstone.accept_lost_keyring"])
Admin = Annotated[Principal, Depends(require_admin)]


class Done(BaseModel):
    ok: bool = True


class ResolveIn(BaseModel):
    action: str = Field(pattern="^(reexport|unfollow)$")


class JobOut(BaseModel):
    id: str
    kind: str
    status: str
    error_code: str | None
    last_error: str | None
    attempts: int


class JobsOut(BaseModel):
    items: list[JobOut]


class RecheckOut(BaseModel):
    cleared: list[str]
    denied: list[str]


class RotateIn(BaseModel):
    password: str = Field(min_length=1, repr=False)


class SecretIn(BaseModel):
    secret: str = Field(min_length=1, repr=False)  # user:password


class LanguageIn(BaseModel):
    lang: str = Field(min_length=1)


class LanguageOut(BaseModel):
    lang: str


class FollowFromIn(BaseModel):
    series: str = Field(min_length=1)
    chapter: str = Field(min_length=1)


class MigrationIn(BaseModel):
    fingerprint: str = Field(min_length=1)


class RestoreIn(BaseModel):
    confirm_deletion: bool = False


class KeyringIn(BaseModel):
    confirmation: str = Field(min_length=1)


class KeyringOut(BaseModel):
    waived: list[str]


@contextmanager
def refusals() -> Iterator[None]:
    """A refusal of a service is a problem of the catalog, never a 500."""
    try:
        yield
    except OpsError as error:
        raise ProblemError(error.code) from None
    except (HeadlessError, BindingError) as error:
        raise ProblemError(error.code) from None


def _path(command: str) -> str:
    return ENDPOINTS[command][1].removeprefix("/api/v1")


def build_ops_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["operations"])

    def conditional(request: Request, action: str) -> Principal:
        return reserved_action(RESERVED_ACTIONS[action])(request)

    @router.post(_path("missing resolve"))
    def missing_resolve(
        destination_id: str, export_item_id: str, body: ResolveIn, principal: Admin
    ) -> Done:
        with refusals():
            get_ops().missing_resolve(
                destination_id, export_item_id, body.action, actor_id=user_of(principal)
            )
        return Done()

    @router.post(_path("series resume"))
    def series_resume(export_series_id: str, _admin: Admin) -> Done:
        with refusals():
            get_ops().series_resume(export_series_id)
        return Done()

    @router.get(_path("jobs list"), response_model=JobsOut)
    def jobs_list(_admin: Admin, status: str | None = None) -> JobsOut:
        with refusals():
            rows = get_ops().jobs_list(status=status)
        return JobsOut(
            items=[JobOut(**{k: getattr(r, k) for k in JobOut.model_fields}) for r in rows]
        )

    @router.post(_path("jobs retry"))
    def jobs_retry(job_id: str, _admin: Admin) -> Done:
        with refusals():
            get_ops().jobs_retry(job_id)
        return Done()

    @router.post(_path("target reconnect"))
    def target_reconnect(target_id: str, _admin: Admin) -> Done:
        with refusals():
            get_ops().target_reconnect(target_id)
        return Done()

    @router.post(_path("target recheck"))
    def target_recheck(target_id: str, request: Request, _admin: Admin) -> RecheckOut:
        with refusals():
            ops = get_ops()
            if ops.target_recheck_is_reserved(target_id):  # parental control: fresh session only
                conditional(request, "target.recheck_child_exposure")
            outcome = ops.target_recheck(target_id)
        return RecheckOut(cleared=list(outcome.cleared), denied=list(outcome.denied))

    @router.post(_path("target rotate-key"))
    def target_rotate_key(
        target_id: str,
        body: RotateIn,
        _principal: Annotated[Principal, Depends(_ROTATE_KEY)],
    ) -> Done:
        with refusals():
            get_ops().target_rotate_key(target_id, password=body.password)
        return Done()

    @router.post(_path("source set-secret"))
    def source_set_secret(
        body: SecretIn,
        _principal: Annotated[Principal, Depends(_SET_SECRET)],
    ) -> Done:
        with refusals():
            get_ops().source_set_secret(body.secret)
        return Done()

    @router.post(_path("source recheck"))
    def source_recheck(_admin: Admin) -> Done:
        with refusals():
            get_ops().source_recheck()
        return Done()

    @router.post(_path("edition set-lang"), response_model=LanguageOut)
    def edition_set_lang(edition_id: str, body: LanguageIn, _admin: Admin) -> LanguageOut:
        with refusals():
            return LanguageOut(lang=get_ops().edition_set_lang(edition_id, body.lang))

    @router.post(_path("adopt set-follow-from"))
    def adopt_set_follow_from(destination_id: str, body: FollowFromIn, _admin: Admin) -> Done:
        with refusals():
            get_ops().adopt_set_follow_from(
                destination_id=destination_id, series=body.series, chapter=body.chapter
            )
        return Done()

    @router.post(_path("adopt --confirm-migration"))
    def adopt_confirm_migration(destination_id: str, body: MigrationIn, _admin: Admin) -> Done:
        with refusals():
            get_ops().adopt_confirm_migration(
                destination_id=destination_id, fingerprint=body.fingerprint
            )
        return Done()

    @router.post(_path("reconcile --adopt-after-restore"))
    def restore_adopt(body: RestoreIn, request: Request, principal: Admin) -> Done:
        if body.confirm_deletion:  # the step that confirms a deletion is a reserved action
            principal = conditional(request, "reconcile.confirm_deletion")
        with refusals():
            get_ops().restore_adopt(confirm_deletion=body.confirm_deletion, principal=principal)
        return Done()

    @router.post(_path("tombstone accept-lost-keyring"), response_model=KeyringOut)
    def accept_lost_keyring(
        body: KeyringIn,
        principal: Annotated[Principal, Depends(_LOST_KEYRING)],
    ) -> KeyringOut:
        with refusals():
            waived = get_ops().tombstone_accept_lost_keyring(
                confirmation=body.confirmation, principal=principal
            )
        return KeyringOut(waived=waived)

    return router
