"""The trust card of a module: three lines, Officiel / Communauté / Inconnu (EF-UX-011).

The card always has the same three lines, in that order, and marks the one that applies:

- **official**: the module ships with the host (first-party);
- **community**: a third-party module whose publisher trust the admin attested for the version that
  is installed (an attestation lapses with the version, see `attestation`);
- **unknown**: everything else. A third-party module is unknown until someone vouched for it, and
  the key of one of the host's own plugins gives it nothing: only the origin decided by the host
  (the distribution that ships it) makes a module official.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import Connection

from shelfhold.i18n import t
from shelfhold.plugin_host.attestation import FIRST_PARTY_PLUGIN_KEYS, publisher_attested
from shelfhold.plugin_host.registry import PluginOrigin, PluginRecord

__all__ = ["TrustCard", "TrustLevel", "TrustLine", "trust_card", "trust_level"]


class TrustLevel(StrEnum):
    OFFICIAL = "official"
    COMMUNITY = "community"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TrustLine:
    level: TrustLevel
    label: str
    current: bool


@dataclass(frozen=True)
class TrustCard:
    level: TrustLevel
    lines: tuple[TrustLine, TrustLine, TrustLine]

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "lines": [
                {"level": x.level.value, "label": x.label, "current": x.current} for x in self.lines
            ],
        }


def trust_level(record: PluginRecord, connection: Connection) -> TrustLevel:
    if record.origin is PluginOrigin.FIRST_PARTY:
        return TrustLevel.OFFICIAL
    # a key that is one of the host's own is no proof for a plugin from outside
    if record.key not in FIRST_PARTY_PLUGIN_KEYS and publisher_attested(connection, record.key):
        return TrustLevel.COMMUNITY
    return TrustLevel.UNKNOWN


def _label(level: TrustLevel) -> str:
    match level:
        case TrustLevel.OFFICIAL:
            return t("trust_card.official")
        case TrustLevel.COMMUNITY:
            return t("trust_card.community")
        case TrustLevel.UNKNOWN:
            return t("trust_card.unknown")


def trust_card(record: PluginRecord, connection: Connection) -> TrustCard:
    level = trust_level(record, connection)

    def line(of: TrustLevel) -> TrustLine:
        return TrustLine(of, _label(of), of is level)

    return TrustCard(
        level, (line(TrustLevel.OFFICIAL), line(TrustLevel.COMMUNITY), line(TrustLevel.UNKNOWN))
    )
