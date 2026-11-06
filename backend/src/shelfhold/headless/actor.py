"""Who is behind a headless call, for the audit (ENF-SEC-013).

Same convention as the other audit rows of the identity context: the user for a session or a
key, `local_cli:<uid>` for the host client, plus the session and its last password entry.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

__all__ = ["Actor", "actor_of"]


@dataclass(frozen=True)
class Actor:
    actor_id: str | None
    session_id: str | None = None
    auth_time: datetime | None = None


def actor_of(principal: Any) -> Actor:
    """From a `Principal` (duck-typed: the headless layer does not import the identity one)."""
    local = getattr(principal.credential, "value", None) == "local_cli"
    return Actor(
        principal.actor_id if local else principal.user_id,
        principal.session_id,
        principal.auth_time,
    )
