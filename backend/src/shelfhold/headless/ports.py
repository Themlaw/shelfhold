"""What the headless configuration needs from the network, as ports (adapters in `adapters.py`)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

__all__ = ["ChannelValidator", "ExchangedKey", "KavitaKeyExchange", "SuwayomiProbe"]


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


class SuwayomiProbe(Protocol):
    def auth_mode(self, *, url: str, username: str | None, password: str | None) -> str: ...


class ChannelValidator(Protocol):
    def validate(self, url: str) -> str:
        """The URL when its scheme and destination are allowed; else `ValueError`."""
        ...
