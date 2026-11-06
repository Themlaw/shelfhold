"""Who is behind a headless call, for the audit (ENF-SEC-013).

Same convention as the other audit rows of the identity context: the user for a session or a
key, `local_cli:<uid>` for the host client, plus the session and its last password entry.
"""

from dataclasses import dataclass
from datetime import datetime

__all__ = ["Actor"]


@dataclass(frozen=True)
class Actor:
    actor_id: str | None
    session_id: str | None = None
    auth_time: datetime | None = None
