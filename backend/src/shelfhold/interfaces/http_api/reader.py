"""`GET /api/reader/{carrier_type}/{carrier_id}/publication`: what the reader opens (M5 L06).

The manifest of a book for the profile and the session that ask (EF-LEC-014, EF-LEC-005, D-P1):
the metadata, the direction of reading (`ltr`, `rtl`, `webtoon`), the pages of an image carrier or
the reading order of an EPUB, and a link into `/content` for each entry, carrying a ticket bound to
the session. The credential is the session cookie: a key of the API and a device token get no
ticket (403). A carrier that is absent, hidden from the profile, of a type that does not exist or
not an identifier answers one 404 (no oracle); a Work with no file that may be read, an excerpt that
cannot be cut within its bound, and a full read of what is only a sample are typed 409s that say
what to do. Nothing is written: no position, no history, no state.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel

from shelfhold.identity.public import Principal
from shelfhold.infrastructure.http_headers import PRIVATE_CACHE
from shelfhold.infrastructure.ids import is_ulid
from shelfhold.interfaces.http_api.auth import require_session
from shelfhold.interfaces.http_api.problems import ProblemError
from shelfhold.reading.application.content_access import (
    ExcerptUnavailableError,
    FullReadNotAllowedError,
    NoReadableCopyError,
)
from shelfhold.reading.application.publication import Publication, PublicationService
from shelfhold.reading.public import UnknownCarrierError

__all__ = ["build_reader_router"]

Session = Annotated[Principal, Depends(require_session)]
_ABSENT = {"Cache-Control": "no-store"}


class EntryOut(BaseModel):
    index: int
    href: str
    media_type: str


class PublicationOut(BaseModel):
    carrier_type: Literal["content", "export_item"]
    carrier_id: str
    scope: Literal["full", "excerpt"]
    kind: Literal["pages", "epub"]
    title: str
    number: str | None
    language: str | None
    reading_direction: Literal["ltr", "rtl", "webtoon"]
    expires_at: datetime
    pages: list[EntryOut]
    spine: list[EntryOut]
    package: str | None


def _out(found: Publication) -> PublicationOut:
    return PublicationOut(
        carrier_type=found.carrier_type,  # type: ignore[arg-type]
        carrier_id=found.carrier_id,
        scope=found.scope,
        kind=found.kind,
        title=found.title,
        number=found.number,
        language=found.language,
        reading_direction=found.reading_direction,
        expires_at=found.expires_at,
        pages=[EntryOut(index=e.index, href=e.href, media_type=e.media_type) for e in found.pages],
        spine=[EntryOut(index=e.index, href=e.href, media_type=e.media_type) for e in found.spine],
        package=found.package,
    )


def build_reader_router(get_service: Callable[[], PublicationService]) -> APIRouter:
    router = APIRouter(prefix="/api/reader", tags=["reader"])

    @router.get(
        "/{carrier_type}/{carrier_id}/publication",
        operation_id="reader_publication",
        response_model=PublicationOut,
        responses={404: {"description": "not-found"}, 409: {"description": "not-readable"}},
    )
    def publication(
        carrier_type: str,
        carrier_id: str,
        response: Response,
        principal: Session,
        scope: Annotated[Literal["full", "excerpt"] | None, Query()] = None,
    ) -> PublicationOut:
        if principal.session_id is None or principal.via_forward:
            raise ProblemError("credential_not_allowed")  # a ticket is bound to a real session
        if not is_ulid(carrier_id):
            raise ProblemError("not_found", headers=_ABSENT)
        try:
            found = get_service().publication(
                user_id=principal.user_id,
                session_id=principal.session_id,
                carrier_type=carrier_type,
                carrier_id=carrier_id,
                scope=scope,
            )
        except UnknownCarrierError:
            raise ProblemError("not_found", headers=_ABSENT) from None
        except FullReadNotAllowedError:
            raise ProblemError("full_read_not_allowed") from None
        except ExcerptUnavailableError:  # before its parent: a typed refusal of its own
            raise ProblemError("excerpt_unavailable") from None
        except NoReadableCopyError:
            raise ProblemError("no_readable_copy") from None
        response.headers["Cache-Control"] = PRIVATE_CACHE  # it holds tickets: one profile's browser
        return _out(found)

    return router
