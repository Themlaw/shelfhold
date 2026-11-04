"""Supported range of Kavita versions and the modes it implies (ENF-COMP-001, the single source).

One minimum for every shelf (0.9.1): `plugin/parse` and `parse-bulk` exist from 0.9.0 and 0.9.1
fixes the known `access_control` and `auth` flaws. Outside the range the destination is
`degraded_readonly` (preview only, `target.version_unsupported`). A route of the allow-list that
answers 404 puts it in `degraded_feature` (`target.route_missing`); that mode is decided from
answers, so it lives with the health of the target, not here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

_PARTS: Final = 4  # Kavita tags look like 0.9.1.4
_RAW: Final = re.compile(r"v?(\d+(?:\.\d+){1,3})")


class TargetMode(StrEnum):
    FULL = "full"
    DEGRADED_FEATURE = "degraded_feature"  # a listed route is gone: its features are cut
    DEGRADED_READONLY = "degraded_readonly"  # version out of range: preview only, no write


@dataclass(frozen=True, order=True)
class KavitaVersion:
    parts: tuple[int, int, int, int]

    @classmethod
    def parse(cls, raw: str | None) -> KavitaVersion | None:
        """`0.9.1`, `v0.9.1.4`: at least major.minor, padded with zeros; else None."""
        match = _RAW.fullmatch((raw or "").strip())
        if match is None:
            return None
        numbers = [int(part) for part in match.group(1).split(".")]
        padded = (*numbers, *([0] * (_PARTS - len(numbers))))
        return cls((padded[0], padded[1], padded[2], padded[3]))

    def __str__(self) -> str:
        shown = list(self.parts)
        while len(shown) > 3 and shown[-1] == 0:
            shown.pop()
        return ".".join(map(str, shown))


MINIMUM_VERSION: Final = KavitaVersion((0, 9, 1, 0))
# The API has no version prefix and no deprecation policy (26 routes gone between 0.8.9.1 and
# 0.9.1.4): a new minor line is out of range until its openapi is frozen and its routes verified.
MAXIMUM_EXCLUSIVE: Final = KavitaVersion((0, 10, 0, 0))


def minimum_version_for(max_age_rating: int | None) -> KavitaVersion:
    """The minimum is the same for every shelf, adult or family (passe 12, M9)."""
    return MINIMUM_VERSION


@dataclass(frozen=True)
class KnownBad:
    """A vulnerable release line: warned about, never refused (ENF-COMP-001)."""

    advisory: str
    category: str  # "cve" | "auth" | "access_control" ...
    below: KavitaVersion  # exclusive upper bound of the affected versions

    def affects(self, version: KavitaVersion) -> bool:
        return version < self.below


KNOWN_BAD_VERSIONS: tuple[KnownBad, ...] = ()


@dataclass(frozen=True)
class Assessment:
    raw: str
    version: KavitaVersion | None
    mode: TargetMode
    known_bad: tuple[KnownBad, ...] = ()


def assess_version(
    raw: str | None, *, known_bad: tuple[KnownBad, ...] = KNOWN_BAD_VERSIONS
) -> Assessment:
    version = KavitaVersion.parse(raw)
    in_range = version is not None and MINIMUM_VERSION <= version < MAXIMUM_EXCLUSIVE
    warnings = tuple(k for k in known_bad if version is not None and k.affects(version))
    mode = TargetMode.FULL if in_range else TargetMode.DEGRADED_READONLY
    return Assessment(raw or "", version, mode, warnings)
