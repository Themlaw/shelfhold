"""The schemes an admin may configure (ENF-SEC-016): a short allowlist, not Apprise's catalogue.

Apprise ships some ninety services, among them generic webhooks (`json://`, `form://`, `xml://`)
that send a request to any host, desktop and local ones (`dbus://`, `gnome://`, `file`-like
attachments) and e-mail, whose SMTP host is derived from the address. None of them is needed to
be told that a backup failed, so none is allowed. `HOSTED` are the schemes whose target host is
chosen by the admin (a self-hosted server): only these reach a network address of the admin's
choosing, so only these go through the SSRF check; the others talk to one fixed public service.
"""

from __future__ import annotations

import re
from typing import Final

ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset(
    {
        "ntfy", "ntfys",
        "gotify", "gotifys",
        "matrix", "matrixs",
        "discord",
        "slack",
        "tgram",
        "pover",
    }
)  # fmt: skip
HOSTED: Final[frozenset[str]] = frozenset(
    {"ntfy", "ntfys", "gotify", "gotifys", "matrix", "matrixs"}
)
SECURE: Final[frozenset[str]] = frozenset({"ntfys", "gotifys", "matrixs"})
_SCHEME: Final = re.compile(r"([a-z][a-z0-9+.-]*)://", re.IGNORECASE)


def scheme_of(url: str) -> str | None:
    match = _SCHEME.match(url)
    return match.group(1).lower() if match else None
