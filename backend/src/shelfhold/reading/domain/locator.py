"""The locator v1 the integrated reader stores (ADR-0055, EF-LEC-007): closed, versioned, checked.

* **EPUB**: `{"v": 1, "type": "cfi", "cfi": "epubcfi(...)", "totalProgression": 0.37, "filter":
  "<filter>@<version>"}`. `cfi`, `totalProgression` (0 to 1) and `filter` (the version of the
  filter and of the neutralizer that made the DOM) are all required.
* **Pages** (CBZ, PDF, any support of pages): `{"v": 1, "type": "page", "index": 11, "count": 180}`,
  `index` from 0 and below `count`; `totalProgression = (index + 1) / count`.

Nothing else is accepted (no extra key, no number as text, no boolean for a number): what is stored
is rebuilt from the checked fields, so a client never decides the shape of a row. Pure.
"""

import math
import re
from dataclasses import dataclass
from typing import Any, Final, Literal

__all__ = [
    "InvalidLocatorV1Error",
    "LocatorV1",
    "current_content_filter",
    "parse_locator_v1",
]

_MAX_CFI = 2048
_MAX_COUNT = 1_000_000
_CFI = re.compile(r"epubcfi\(.+\)", re.DOTALL)
_FILTER = re.compile(r"[A-Za-z0-9._-]{1,48}@[A-Za-z0-9._-]{1,16}")
_EPUB_KEYS: Final = frozenset({"v", "type", "cfi", "totalProgression", "filter"})
_PAGE_KEYS: Final = frozenset({"v", "type", "index", "count"})


class InvalidLocatorV1Error(ValueError):
    """The locator is not a locator v1 (a 422 for the routes)."""


def current_content_filter() -> str:
    """The version of the filter and neutralizer that make the DOM the reader shows now.

    The one place it is read. TODO(2026-11-18): rewire on `CONTENT_FILTER` once the neutralizer v2
    (L05b, `feat/m5-l05b-neutralizer-v2`) is merged; until then the neutralizer is v1."""
    return "xhtml-neutralizer@v1"


@dataclass(frozen=True)
class LocatorV1:
    kind: Literal["cfi", "page"]
    progression: float  # `totalProgression`, 0 to 1
    cfi: str | None = None
    filter: str | None = None
    index: int | None = None
    count: int | None = None

    def stored(self) -> dict[str, Any]:
        """The JSON kept in `reading_position.locator`. A page also carries `page`, the integer the
        view of the progress reads (`reading_progress`): counted from 1 like the coverage of a Unit
        and kosync, so `index + 1` (the locator's own `index` counts from 0, ADR-0055)."""
        if self.kind == "cfi":
            return {"v": 1, "type": "cfi", "cfi": self.cfi, "totalProgression": self.progression,
                    "filter": self.filter}  # fmt: skip
        return {"v": 1, "type": "page", "index": self.index, "count": self.count,
                "page": (self.index or 0) + 1}  # fmt: skip


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidLocatorV1Error("an integer is expected")
    return value


def parse_locator_v1(raw: object) -> LocatorV1:
    """The locator v1 that `raw` is, else `InvalidLocatorV1Error`."""
    if not isinstance(raw, dict):
        raise InvalidLocatorV1Error("a locator is an object")
    if raw.get("v") != 1 or isinstance(raw.get("v"), bool):
        raise InvalidLocatorV1Error("version 1 only")
    kind = raw.get("type")
    if kind == "cfi":
        if set(raw) != _EPUB_KEYS:
            raise InvalidLocatorV1Error("cfi, totalProgression and filter, nothing else")
        cfi, total, version = raw["cfi"], raw["totalProgression"], raw["filter"]
        if not isinstance(cfi, str) or len(cfi) > _MAX_CFI or not _CFI.fullmatch(cfi):
            raise InvalidLocatorV1Error("cfi is an epubcfi(...)")
        if (
            isinstance(total, bool)
            or not isinstance(total, int | float)
            or not math.isfinite(total)
            or not 0.0 <= total <= 1.0
        ):
            raise InvalidLocatorV1Error("totalProgression is a number from 0 to 1")
        if not isinstance(version, str) or not _FILTER.fullmatch(version):
            raise InvalidLocatorV1Error("filter is <name>@<version>")
        return LocatorV1("cfi", float(total), cfi=cfi, filter=version)
    if kind == "page":
        if set(raw) != _PAGE_KEYS:
            raise InvalidLocatorV1Error("index and count, nothing else")
        index, count = _integer(raw["index"]), _integer(raw["count"])
        if not 1 <= count <= _MAX_COUNT or not 0 <= index < count:
            raise InvalidLocatorV1Error("index is from 0 and below count")
        return LocatorV1("page", (index + 1) / count, index=index, count=count)
    raise InvalidLocatorV1Error("type is cfi or page")
