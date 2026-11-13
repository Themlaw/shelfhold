"""The routes that leave the waiting states (EF-INT-023), one per exit command of the catalogue.

Each route is the twin of a CLI command and calls the same service. The reserved ones (a secret, a
parental-control release, the lost keyring, a deletion confirmed after a restore) declare their
action, so a fresh session or the local CLI is needed and every API key is refused
(`credential-not-allowed`); the conditional ones decide from the state of the thing they act on.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from shelfhold.export.domain.bindings import BindingError
from shelfhold.headless import HeadlessError
from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.interfaces.http_api.auth import reserved_action
from shelfhold.interfaces.http_api.lists import conditional_json
from shelfhold.interfaces.http_api.pagination import PageParams, encode_cursor, page_params_for
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.interfaces.http_api.stats import require_admin
from shelfhold.ops import ENDPOINTS, OpsError, OpsServices, user_of
from shelfhold.ops.exits import command_of

__all__ = ["build_ops_router"]

_ROTATE_KEY = reserved_action(RESERVED_ACTIONS["target.rotate_key"])
_SET_SECRET = reserved_action(RESERVED_ACTIONS["source.set_secret"])
_LOST_KEYRING = reserved_action(RESERVED_ACTIONS["tombstone.accept_lost_keyring"])
_SECRETS_IMPORT = reserved_action(RESERVED_ACTIONS["secrets.import"])
_SET_ASIDE = reserved_action(RESERVED_ACTIONS["tombstone.set_aside_journal"])
_RATE = reserved_action(RESERVED_ACTIONS["rate"])
_ROOT_TAKEOVER = reserved_action(RESERVED_ACTIONS["root.takeover"])
_ROOT_REINIT = reserved_action(RESERVED_ACTIONS["root.reinit"])
_STORE_REINIT = reserved_action(RESERVED_ACTIONS["store.reinit"])
_RESTORE_ACK = reserved_action(RESERVED_ACTIONS["restore.acknowledge"])
Admin = Annotated[Principal, Depends(require_admin)]


class Done(BaseModel):
    ok: bool = True


class AcknowledgedOut(BaseModel):
    ok: bool = True
    passkey_changes: list[str] = []


class ResolveIn(BaseModel):
    action: str = Field(pattern="^(reexport|unfollow)$")


class JobOut(BaseModel):
    id: str
    kind: str
    status: str
    error_code: str | None
    last_error: str | None
    attempts: int
    request_id: str | None  # the request the job serves (EF-INT-017: « pour qui »); admin only
    priority_class: str
    source_key: str | None
    progress: float | None


class JobsOut(BaseModel):
    items: list[JobOut]
    next_cursor: str | None = None  # absent on the last page (EF-INT-001)


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


class SeriesNameIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class SeriesNameOut(BaseModel):
    export_series_id: str
    name: str


class FollowFromIn(BaseModel):
    series: str = Field(min_length=1)
    chapter: str = Field(min_length=1)


class MigrationIn(BaseModel):
    fingerprint: str = Field(min_length=1)


class RestoreIn(BaseModel):
    confirm_deletion: bool = False


class ForgetRootIn(BaseModel):
    root_id: str = Field(min_length=1)
    confirm: str = Field(min_length=1)


class KeyringIn(BaseModel):
    confirmation: str = Field(min_length=1)


class KeyringOut(BaseModel):
    waived: list[str]


class SecretsImportIn(BaseModel):
    """Two plain file names in `<data folder>/import/` on the host of the server: the passphrase
    never travels in the request, and the caller picks no path."""

    passphrase_file: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    in_file: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")


class SecretsImportOut(BaseModel):
    added: list[str]  # key ids merged into the keyring (never the keys)
    present: int  # key ids of the file the keyring already held
    replayed: int  # tombstone lines read again with the merged keyring
    deferred: bool = False  # restored_behind: the replay is the adoption's


class RateIn(BaseModel):
    age: str = Field(min_length=1, max_length=8, examples=["13+", "18+", "X"])


class WithdrawalOut(BaseModel):
    edition_id: str
    shelf_id: str
    reason: str


class EditionSuggestionOut(BaseModel):
    """An open suggested age above an Edition's: rating the Work leaves it open and the Edition
    stays out of the shelves below 18+ until `rate_edition`."""

    edition_id: str
    age: int
    x: bool
    provenance: str


class RatedOut(BaseModel):
    """The Work is rated; the files a raised age no longer lets stay on a shelf below 18+ are
    listed in `withdrawals` and nothing was taken out (`shelf withdraw`, EF-BIB-037)."""

    target: str
    label: str  # i18n key of the age
    effective: int | None
    effective_x: bool
    withdrawals: list[WithdrawalOut]
    open_suggestions: list[EditionSuggestionOut] = []


class RootIn(BaseModel):
    """`path`: a folder the database does not know yet (`foreign_root` found by the start-up),
    named by its absolute path instead of the `root_id` of the URL."""

    path: str | None = Field(default=None, min_length=1, max_length=4096)


class RootOut(BaseModel):
    command: str
    root_id: str
    path: str
    previous: str  # the state the root was in
    sentinel: str  # `stamped` or `restamped`
    predecessors: list[str]
    missing: int  # records that went to `missing` (reinit)
    planned: int  # records planned again for the next export (restored)
    units_store_missing: int
    distrusted_copies: int
    ignored_claims: list[str]  # what an unauthenticated sentinel claimed and was ignored
    notes: list[str]


class SetAsideIn(BaseModel):
    root_id: str | None = None  # default: every root whose journal cannot be read


class SetAsideOut(BaseModel):
    aside: dict[str, str]  # root id -> the name the journal was put aside under


class FlushOut(BaseModel):
    still_owed: list[str]  # roots that still owe lines (not mounted yet, or refusing writes)


@contextmanager
def refusals() -> Iterator[None]:
    """A refusal of a service is a problem of the catalog, never a 500."""
    try:
        yield
    except OpsError as error:
        raise ProblemError(error.code) from None
    except (HeadlessError, BindingError) as error:
        raise ProblemError(error.code) from None


def _path(*words: str) -> str:
    return ENDPOINTS[command_of(*words)][1].removeprefix("/api/v1")


def build_ops_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["operations"])

    def conditional(request: Request, action: str) -> Principal:
        return reserved_action(RESERVED_ACTIONS[action])(request)

    @router.post(_path("missing", "resolve"))
    def missing_resolve(
        destination_id: str, export_item_id: str, body: ResolveIn, principal: Admin
    ) -> Done:
        with refusals():
            get_ops().missing_resolve(
                destination_id, export_item_id, body.action, actor_id=user_of(principal)
            )
        return Done()

    @router.post(_path("series", "resume"))
    def series_resume(export_series_id: str, _admin: Admin) -> Done:
        with refusals():
            get_ops().series_resume(export_series_id)
        return Done()

    @router.get(_path("jobs", "list"), response_model=JobsOut)
    def jobs_list(
        request: Request,
        _admin: Admin,
        params: Annotated[PageParams, Depends(page_params_for(str))],
        status: str | None = None,
    ) -> Response:
        after = str(params.after[0]) if params.after else None
        with refusals():
            rows = get_ops().jobs_list(status=status, limit=params.limit + 1, after=after)
        served = rows[: params.limit]
        last = served[-1] if len(rows) > params.limit else None
        page = JobsOut(
            items=[JobOut(**{k: getattr(r, k) for k in JobOut.model_fields}) for r in served],
            next_cursor=encode_cursor([last.id]) if last else None,
        )
        return conditional_json(request, page.model_dump(mode="json"))

    @router.post(_path("jobs", "retry"))
    def jobs_retry(job_id: str, request: Request, admin: Admin) -> Done:
        with refusals():
            ops = get_ops()
            if ops.jobs_retry_is_reserved(job_id):  # the application of a rename plan: a session
                conditional(request, "plan.apply_rename")
            ops.jobs_retry(job_id, principal=admin)
        return Done()

    @router.post(_path("target", "reconnect"))
    def target_reconnect(target_id: str, _admin: Admin) -> Done:
        with refusals():
            get_ops().target_reconnect(target_id)
        return Done()

    @router.post(_path("target", "recheck"))
    def target_recheck(target_id: str, request: Request, _admin: Admin) -> RecheckOut:
        with refusals():
            ops = get_ops()
            if ops.target_recheck_is_reserved(target_id):  # parental control: fresh session only
                conditional(request, "target.recheck_child_exposure")
            outcome = ops.target_recheck(target_id)
        return RecheckOut(cleared=list(outcome.cleared), denied=list(outcome.denied))

    @router.post(_path("rate"))
    def rate_work(
        work_id: str, body: RateIn, principal: Annotated[Principal, Depends(_RATE)]
    ) -> RatedOut:
        """Fix the age of a Work: the exit of `rating_pending` and `follow_suspended`. Reserved
        (EF-BIB-036): a fresh admin session holding the right to classify, never a key."""
        with refusals():
            done = get_ops().rating.rate(
                body.age, work_id=work_id, edition_id=None, principal=principal
            )
        return RatedOut(**done)

    @router.post(_path("root", "takeover"), response_model=RootOut)
    def root_takeover(
        root: str, body: RootIn, principal: Annotated[Principal, Depends(_ROOT_TAKEOVER)]
    ) -> RootOut:
        """Stamp a root as ours, keeping its `root_id` and its predecessors (EF-EXP-064).
        Reserved: a fresh admin session or the host CLI, never a key."""
        with refusals():
            return RootOut(**get_ops().root_takeover(body.path or root, principal=principal))

    @router.post(_path("root", "reinit"), response_model=RootOut)
    def root_reinit(
        root: str, body: RootIn, principal: Annotated[Principal, Depends(_ROOT_REINIT)]
    ) -> RootOut:
        """The disk is not what the database remembers: its records go to `missing`. Reserved."""
        with refusals():
            return RootOut(**get_ops().root_reinit(body.path or root, principal=principal))

    @router.post(_path("root", "restored"), response_model=RootOut)
    def root_restored(root: str, principal: Admin) -> RootOut:
        """A root put back from an older copy: its missing files are planned for the next export."""
        with refusals():
            return RootOut(**get_ops().root_restored(root, principal=principal))

    @router.post(_path("store", "reinit"), response_model=RootOut)
    def store_reinit(principal: Annotated[Principal, Depends(_STORE_REINIT)]) -> RootOut:
        """`/store` is not what the database remembers: its Units go to `store_missing`."""
        with refusals():
            return RootOut(**get_ops().store_reinit(principal=principal))

    @router.post(_path("restore", "acknowledge"))
    def restore_acknowledge(
        principal: Annotated[Principal, Depends(_RESTORE_ACK)],
    ) -> AcknowledgedOut:
        """The passkeys and passwords changed since the backup were looked at: passkey sign-in
        comes back. Lists the passkeys created or revoked since the backup, as far as known."""
        with refusals():
            changes = get_ops().restore_acknowledge(principal=principal)
        return AcknowledgedOut(passkey_changes=changes)

    @router.post(_path("target", "rotate-key"))
    def target_rotate_key(
        target_id: str,
        body: RotateIn,
        _principal: Annotated[Principal, Depends(_ROTATE_KEY)],
    ) -> Done:
        with refusals():
            get_ops().target_rotate_key(target_id, password=body.password)
        return Done()

    @router.post(_path("source", "set-secret"))
    def source_set_secret(
        body: SecretIn,
        _principal: Annotated[Principal, Depends(_SET_SECRET)],
    ) -> Done:
        with refusals():
            get_ops().source_set_secret(body.secret)
        return Done()

    @router.post(_path("source", "recheck"))
    def source_recheck(_admin: Admin) -> Done:
        with refusals():
            get_ops().source_recheck()
        return Done()

    @router.post(_path("source", "reconnect"))
    def source_reconnect(_admin: Admin) -> Done:
        with refusals():
            get_ops().source_reconnect()
        return Done()

    @router.post(_path("edition", "set-lang"), response_model=LanguageOut)
    def edition_set_lang(edition_id: str, body: LanguageIn, _admin: Admin) -> LanguageOut:
        with refusals():
            return LanguageOut(lang=get_ops().edition_set_lang(edition_id, body.lang))

    @router.post(_path("edition", "set-series-name"), response_model=SeriesNameOut)
    def edition_set_series_name(
        edition_id: str, body: SeriesNameIn, _admin: Admin
    ) -> SeriesNameOut:
        with refusals():
            series_id, name = get_ops().edition_set_series_name(edition_id, body.name)
            return SeriesNameOut(export_series_id=series_id, name=name)

    @router.post(_path("adopt", "set-follow-from"))
    def adopt_set_follow_from(destination_id: str, body: FollowFromIn, _admin: Admin) -> Done:
        with refusals():
            get_ops().adopt_set_follow_from(
                destination_id=destination_id, series=body.series, chapter=body.chapter
            )
        return Done()

    @router.post(_path("adopt", "--confirm-migration"))
    def adopt_confirm_migration(destination_id: str, body: MigrationIn, _admin: Admin) -> Done:
        with refusals():
            get_ops().adopt_confirm_migration(
                destination_id=destination_id, fingerprint=body.fingerprint
            )
        return Done()

    @router.post(_path("reconcile", "--adopt-after-restore"))
    def restore_adopt(body: RestoreIn, request: Request, principal: Admin) -> Done:
        if body.confirm_deletion:  # the step that confirms a deletion is a reserved action
            principal = conditional(request, "reconcile.confirm_deletion")
        with refusals():
            get_ops().restore_adopt(confirm_deletion=body.confirm_deletion, principal=principal)
        return Done()

    @router.post(_path("reconcile", "--forget-root"))
    def restore_forget_root(body: ForgetRootIn, request: Request) -> Done:
        # a fresh session, never a key; the guard writes no line (the host path writes its own)
        principal = reserved_action(RESERVED_ACTIONS["reconcile.forget_root"], audit=False)(request)
        with refusals():
            get_ops().restore_forget_root(
                root_id=body.root_id, confirm=body.confirm, principal=principal
            )
        return Done()

    @router.post(_path("tombstone", "accept-lost-keyring"), response_model=KeyringOut)
    def accept_lost_keyring(
        body: KeyringIn,
        principal: Annotated[Principal, Depends(_LOST_KEYRING)],
    ) -> KeyringOut:
        with refusals():
            waived = get_ops().tombstone_accept_lost_keyring(
                confirmation=body.confirmation, principal=principal
            )
        return KeyringOut(waived=waived)

    @router.post(_path("secrets", "import"), response_model=SecretsImportOut)
    def secrets_import(
        body: SecretsImportIn,
        principal: Annotated[Principal, Depends(_SECRETS_IMPORT)],
    ) -> SecretsImportOut:
        with refusals():
            report = get_ops().secrets_import(
                passphrase_file=body.passphrase_file,
                in_file=body.in_file,
                principal=principal,
                confined=True,
            )
        return SecretsImportOut(**report)

    @router.post(_path("tombstone", "set-aside-journal"), response_model=SetAsideOut)
    def set_aside_journal(
        body: SetAsideIn,
        principal: Annotated[Principal, Depends(_SET_ASIDE)],
    ) -> SetAsideOut:
        with refusals():
            aside = get_ops().tombstone_set_aside_journal(principal=principal, root_id=body.root_id)
        return SetAsideOut(aside=aside)

    @router.post(_path("tombstone", "flush"), response_model=FlushOut)
    def tombstone_flush(_principal: Admin) -> FlushOut:
        with refusals():
            return FlushOut(still_owed=get_ops().tombstone_flush())

    return router
