"""What a KEPUB download looks like on the wire, for `/opds` and `/lite` alike (M5 L20b).

The bytes were made from the neutralized EPUB and judged for the profile before they get here; the
answer is an attachment that no browser runs (`sandbox`, `nosniff`), named after the title of the
Work the profile may see, with the extension a Kobo wants (`.kepub.epub`).
"""

from fastapi.responses import Response

from shelfhold.infrastructure.http_headers import CSP_SANDBOX, attachment_disposition
from shelfhold.interfaces.http_api.path_safety import safe_filename
from shelfhold.reading.application.kepub_delivery import KepubFile

__all__ = ["KEPUB_MEDIA", "kepub_name", "kepub_response"]

KEPUB_MEDIA = "application/kepub+zip"


def kepub_name(title: str) -> str:
    return f"{safe_filename(title)}.kepub.epub"


def kepub_response(file: KepubFile, headers: dict[str, str]) -> Response:
    return Response(
        file.data,
        media_type=KEPUB_MEDIA,
        headers={
            **headers,
            "Content-Disposition": attachment_disposition(kepub_name(file.title)),
            "Content-Security-Policy": CSP_SANDBOX,
        },
    )
