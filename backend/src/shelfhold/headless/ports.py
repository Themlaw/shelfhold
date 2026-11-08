"""What the headless configuration needs from the network, as ports (adapters in `adapters.py`)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

__all__ = [
    "ChannelValidator",
    "ExchangedKey",
    "KavitaKeyExchange",
    "ShelfLinkPort",
    "SuwayomiProbe",
]


@dataclass(frozen=True)
class ExchangedKey:
    """The named, expiring key of the dedicated Kavita account, and what Kavita said of itself."""

    value: str
    expires_at: datetime | None
    version: str | None
    key_id: int | None = None  # Kavita's own id of the key, to name it if it must be revoked

    def __repr__(self) -> str:
        return f"ExchangedKey(<redacted>, expires_at={self.expires_at!r})"


class KavitaKeyExchange(Protocol):
    def exchange(
        self, *, url: str, username: str, password: str, key_name: str, expires_days: int
    ) -> ExchangedKey: ...

    def revoke(self, *, url: str, username: str, password: str, key: str, key_name: str) -> bool:
        """Delete the old key at Kavita (found by value, else by name); False when not found."""


class SuwayomiProbe(Protocol):
    roles_verified: bool  # `ui_login`: the roles of the account were read (False: not exposed)
    blocking_filters: list[str]  # the global update settings that leave out unread series

    def auth_mode(self, *, url: str, username: str | None, password: str | None) -> str: ...


class ChannelValidator(Protocol):
    def validate(self, url: str) -> str:
        """The URL when its scheme and destination are allowed; else `ValueError`."""
        ...


class ShelfLinkPort(Protocol):
    """Links a new destination to a Kavita library, audited first (EF-EXP-002, EF-EXP-055).

    Raises when children can read the library (`AdoptionRefused`)."""

    def link(self, target_id: str, destination_id: str, *, library_name: str) -> str: ...

    def make_folder(self, local: str) -> None:
        """Creates the folder of a destination, through the sole writer of the disk."""
