"""The REST subset of an lncrawl instance this adapter speaks (D11), and how it reads the answers.

Everything the instance says is untrusted data (EF-BIB-051): an answer is read through the checks
below and through nothing else. Identifiers are opaque words of a closed alphabet, lists and
documents have a bound, text is bounded, and what an answer says about itself (a name, a URL, a
message) is never copied into a reference, an error or a log line. The pinned contract is in
`docs/admin/lncrawl.md`.
"""

import json
import re
from typing import Any, Final

from shelfhold.sdk.fetcher import Response
from shelfhold.sdk.source import SourceChanged

API: Final = "/api/v1"
STATUS: Final = "/status"
LIBRARY: Final = "/library"
NOVEL: Final = "/novels/{novel}"
CHAPTERS: Final = "/novels/{novel}/chapters"
JOBS: Final = "/jobs"
JOB: Final = "/jobs/{job}"
CANCEL: Final = "/jobs/{job}/cancel"
ARTIFACT: Final = "/artifacts/{artifact}/file"

MIN_MAJOR: Final = 1  # the pinned API: 1.x
MAX_MAJOR_EXCLUSIVE: Final = 2
MAX_REPLY_BYTES: Final = 1 << 20  # one JSON document; a listing comes in pages
MAX_PAGES: Final = 100
MAX_ITEMS: Final = 20_000  # chapters of one novel, novels of one library
MAX_COVERED: Final = 5_000  # chapters of one file
MAX_TEXT: Final = 500
MAX_NUMBER: Final = 1_000_000

JOB_STATES: Final = frozenset({"queued", "running", "done", "failed", "cancelled"})
_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
_VERSION: Final = re.compile(r"v?(\d{1,4})\.(\d{1,4})(?:\.(\d{1,6}))?")
_VOLUME: Final = re.compile(r"\s*vol(?:ume)?\.?\s*(\d{1,4}(?:\.\d{1,2})?)\s*$", re.IGNORECASE)


def loads(response: Response) -> Any:
    """The JSON document of an answer, bounded; anything else is a change of the source."""
    if len(response.body) > MAX_REPLY_BYTES:
        raise SourceChanged("the instance sent a reply past the bound")
    try:
        return json.loads(response.body)
    except (ValueError, RecursionError):  # a body nested past the stack is no JSON either
        raise SourceChanged("the instance did not answer with JSON") from None


def mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SourceChanged("the instance sent an unexpected document")
    return value


def identifier(value: object) -> str:
    """An opaque identifier of the instance: letters, digits, `_` and `-`, at most 64."""
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise SourceChanged("the instance sent an unexpected identifier")
    return value


def text(value: object, *, limit: int = MAX_TEXT) -> str | None:
    """A bounded display string, or None. Control characters never get through."""
    if not isinstance(value, str):
        return None
    cleaned = "".join(c for c in value if c.isprintable()).strip()
    return cleaned[:limit] or None


def number(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_NUMBER:
        return None
    return value


def volume_label(value: object) -> str | None:
    """`Vol.3` from "Vol.3", "volume 3"...; the label the host reads in a name (EF-SRC-032)."""
    found = _VOLUME.fullmatch(value) if isinstance(value, str) else None
    return f"Vol.{found.group(1)}" if found else None


def major_supported(version: object) -> bool:
    found = _VERSION.fullmatch(version.strip()) if isinstance(version, str) else None
    return found is not None and MIN_MAJOR <= int(found.group(1)) < MAX_MAJOR_EXCLUSIVE


def items(document: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    """The page of a listing: its entries and the cursor of the next, if any."""
    raw = document.get("items")
    if not isinstance(raw, list) or len(raw) > MAX_ITEMS:
        raise SourceChanged("the instance sent an unexpected listing")
    cursor = document.get("next")
    if cursor is not None and not isinstance(cursor, str):
        raise SourceChanged("the instance sent an unexpected cursor")
    return [mapping(entry) for entry in raw], cursor or None


def covers(value: object) -> list[str]:
    """The chapters a file carries, as the instance lists them: a bounded list of identifiers."""
    if not isinstance(value, list) or not 0 < len(value) <= MAX_COVERED:
        raise SourceChanged("the instance named chapters that are not a range")
    return [identifier(entry) for entry in value]


_KEPT = ("id", "order", "title", "volume", "closed", "team")
_HOSTLIKE = re.compile(r"^\S+\.[A-Za-z]{2,}(:\d+)?(/\S*)?$")


def slim(entry: dict[str, Any]) -> dict[str, Any]:
    """The fields of a listing entry that are read, strings cut short: what the instance adds
    to an entry is not kept alive."""
    return {
        k: (v[:400] if isinstance(v, str) else v)
        for k, v in entry.items()
        if k in _KEPT and (v is None or isinstance(v, str | int | float | bool))
    }


def team(value: object) -> str | None:
    """A translation team is a name; what looks like an address or a host is not one and is
    dropped (Shelfhold shows no source name an instance could slip in there)."""
    name = text(value, limit=200)
    if name is None or "://" in name or _HOSTLIKE.match(name):
        return None
    return name
