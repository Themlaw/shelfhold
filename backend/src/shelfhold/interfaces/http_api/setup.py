"""`/api/v1/setup/*`: the routes of the first-run wizard behind the screens "Kavita" and
"Dossier" (L39, EF-UX-001, EF-UX-002).

The Kavita connection of the second screen is `POST /api/v1/targets` (`headless.py`): the
credentials are exchanged for a named, expiring key and never stored, logged or answered; the
wizard adds no second door for them. Here: the state the screens read, the shared-folder probe
that Kavita reads, the adoption of an existing library (preview, then confirmation on the
fingerprint) and the making of a new one.

Every route is the admin's, on a fresh session, never a key (`admin_risk.accept`, the reserved
action of the Kavita connection itself): the member, the child and a key are refused, and so is
an admin whose session is stale. Each call leaves a line in the audit.
"""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.interfaces.http_api.auth import reserved_action
from shelfhold.interfaces.http_api.ops import refusals
from shelfhold.ops import OpsServices

__all__ = ["build_setup_router"]

_RISK = RESERVED_ACTIONS["admin_risk.accept"]
Admin = Annotated[Principal, Depends(reserved_action(_RISK))]  # one line in the audit per call


class WizardTargetOut(BaseModel):
    id: str
    name: str
    key_expires_at: str | None  # when the dedicated key lapses; the key itself is never shown


class WizardDestinationOut(BaseModel):
    id: str
    target_id: str
    adopted: bool
    linked: bool


class WizardStateOut(BaseModel):
    targets: list[WizardTargetOut]
    destinations: list[WizardDestinationOut]
    first_run: bool


class WizardWriteProbeOut(BaseModel):
    ok: bool
    cause: str | None
    failed_step: str | None


class WizardProbeOut(BaseModel):
    ok: bool
    write: WizardWriteProbeOut
    kavita_read: bool | Literal["no_library"] | None  # None: not asked (the write failed)
    yaml_hint: str | None  # the exact compose block that fixes what failed


class WizardVerdictOut(BaseModel):
    kind: Literal["under_library", "outside_library"]
    copy_needed: bool
    action: str


class WizardRootOut(BaseModel):
    path: str
    present: bool


class WizardPreviewOut(BaseModel):
    fingerprint: str
    library_ref: str
    series: int
    files: int
    child_library: bool
    download_dir: WizardVerdictOut | None
    roots: list[WizardRootOut]


class WizardAdoptOut(BaseModel):
    adopted_files: int
    already_adopted: int
    series: int
    library_ref: str
    linked: bool


class WizardLibraryOut(BaseModel):
    library_ref: str
    linked: bool


class WizardDestinationIn(BaseModel):
    destination_id: str = Field(min_length=1)


class WizardPreviewIn(WizardDestinationIn):
    library_ref: str | None = None  # the Kavita library to adopt; by default the one on the folder
    download_dir: str | None = Field(default=None, min_length=1)  # where Suwayomi writes


class WizardAdoptIn(WizardDestinationIn):
    fingerprint: str = Field(min_length=1)  # what the preview showed: nothing else is adopted
    library_ref: str | None = None


class WizardLibraryIn(WizardDestinationIn):
    name: str = Field(min_length=1, max_length=128)
    library_type: str | None = None


def build_setup_router(get_ops: Callable[[], OpsServices]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/setup", tags=["setup"])

    @router.get("/state")
    def wizard_state(_admin: Admin) -> WizardStateOut:
        with refusals():
            return WizardStateOut(**get_ops().wizard.state())

    @router.post("/probe")
    def wizard_probe(body: WizardDestinationIn, _admin: Admin) -> WizardProbeOut:
        with refusals():
            return WizardProbeOut(**get_ops().wizard.probe(body.destination_id))

    @router.post("/adoption/preview")
    def wizard_adoption_preview(body: WizardPreviewIn, _admin: Admin) -> WizardPreviewOut:
        with refusals():
            return WizardPreviewOut(
                **get_ops().wizard.adoption_preview(
                    body.destination_id,
                    library_ref=body.library_ref,
                    download_dir=body.download_dir,
                )
            )

    @router.post("/adoption/confirm")
    def wizard_adoption_confirm(body: WizardAdoptIn, _admin: Admin) -> WizardAdoptOut:
        with refusals():
            return WizardAdoptOut(
                **get_ops().wizard.adopt(
                    body.destination_id, fingerprint=body.fingerprint, library_ref=body.library_ref
                )
            )

    @router.post("/library", status_code=201)
    def wizard_new_library(body: WizardLibraryIn, _admin: Admin) -> WizardLibraryOut:
        with refusals():
            return WizardLibraryOut(
                **get_ops().wizard.create_library(
                    body.destination_id, name=body.name, library_type=body.library_type
                )
            )

    return router
