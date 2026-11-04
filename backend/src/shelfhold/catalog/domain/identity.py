"""Identity of Editions and Teams (EF-BIB-001, EF-BIB-035, EF-BIB-049).

Pure rules, no storage: which discriminant a translation uses, how the `text_version` part of
an Edition key is derived from a reference, and how a team label is normalized.
"""

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from shelfhold.catalog.domain.normalizers import Normalized, default_registry

TEXT_VERSION_NORMALIZER = "text_version"
TEAM_LABEL_NORMALIZER = "team_label"

# Controlled labels of a `text_version` that names no translator (EF-BIB-049). Adding one is a
# code change, never a free string typed by a source.
TEXT_VERSION_LABELS = frozenset({"abridged", "youth", "revised"})
BOOK_KINDS = frozenset({"novel", "light_novel", "ebook"})

_REGISTRY = default_registry()
_ULID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")


class Discriminant(StrEnum):
    """What tells two translations of one Work apart."""

    TEAM = "team"  # one Edition, a Team and a Variant per release
    TEXT_VERSION = "text_version"  # a new Edition


def translation_discriminant(kind: str, nature: str) -> Discriminant:
    """The declared nature decides; only `unknown` and `machine` fall back on the kind.

    A community translation is a Team whatever the kind (a web novel relayed by fan teams stays
    one Edition); a published translation is a `text_version`.
    """
    if nature == "community":
        return Discriminant.TEAM
    if nature == "published":
        return Discriminant.TEXT_VERSION
    return Discriminant.TEXT_VERSION if kind in BOOK_KINDS else Discriminant.TEAM


@dataclass(frozen=True)
class TextVersionRef:
    """A reference, never a name string: a translator (`contributor`) or a controlled label."""

    contributor_id: str | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        if (self.contributor_id is None) == (self.label is None):
            raise ValueError("a text version references exactly one of contributor or label")
        if self.contributor_id is not None and not _ULID.fullmatch(self.contributor_id):
            raise ValueError(f"not a contributor id: {self.contributor_id!r}")
        if self.label is not None and self.label not in TEXT_VERSION_LABELS:
            raise ValueError(
                f"{self.label!r} is not a controlled label ({sorted(TEXT_VERSION_LABELS)})"
            )

    @property
    def key(self) -> str:
        """The value that enters the Edition key, derived from the reference."""
        return (
            f"contributor:{self.contributor_id}" if self.contributor_id else f"label:{self.label}"
        )

    @property
    def normalized_by(self) -> str:
        return str(_REGISTRY.current(TEXT_VERSION_NORMALIZER))


def normalize_team_label(raw: str) -> Normalized:
    """`(value, raw, team_label@version)`; an empty label is not a team."""
    normalized = _REGISTRY.normalize(TEAM_LABEL_NORMALIZER, raw)
    if not normalized.value:
        raise ValueError("empty team label")
    return normalized


def sort_title(title: str) -> str:
    return " ".join(unicodedata.normalize("NFC", title).casefold().split())
