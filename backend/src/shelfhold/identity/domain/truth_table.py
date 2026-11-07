"""The truth table of the reserved actions, generated from `RESERVED_ACTIONS` (ENF-SEC-010).

One row per action x role x credential (EF-USR-024), with the `fresh_auth_s` column (EF-USR-022,
K5): the age a session must prove to do the action under the given profile, `None` when it needs no
freshness. A new action in the registry gets its rows without anyone remembering to add them; the
test that holds the rows to a hand-written oracle fails when an action has no line there.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from shelfhold.identity.domain.catalogue import RESERVED_ACTIONS
from shelfhold.identity.domain.credentials import CREDENTIAL_AXIS, Credential, Principal
from shelfhold.identity.domain.reserved import (
    DEFAULT_FRESH_AUTH_S,
    ReservedAction,
    authorize_reserved,
)
from shelfhold.identity.domain.roles import RATE_ROLES, TABLE_ROLES


@dataclass(frozen=True)
class TruthRow:
    action: str
    role: str
    credential: str  # a label of `CREDENTIAL_AXIS`
    outcome: str  # the name of the verdict: Allowed, CredentialNotAllowed, RoleNotAllowed, ...
    fresh_auth_s: int | None


def sample_principal(role: str, axis: str, *, now: datetime, fresh_auth_s: int) -> Principal:
    """A principal standing on `axis`: the stale session proved its password 3 h ago, past any
    `fresh_auth_s` the settings accept (86 400 s at most is checked by the caller's range)."""

    def make(
        credential: Credential,
        *,
        session_id: str | None = None,
        auth_time: datetime | None = None,
        key_id: str | None = None,
        scopes: tuple[str, ...] = (),
    ) -> Principal:
        return Principal(
            user_id="table-user", username="table", role=role, credential=credential,
            session_id=session_id, auth_time=auth_time, key_id=key_id, scopes=scopes,
            can_rate=role in RATE_ROLES,
        )  # fmt: skip

    match axis.split(":"):
        case ["fresh_session"]:
            age = timedelta(seconds=min(30, fresh_auth_s // 2))
            return make(Credential.SESSION, session_id="table-session", auth_time=now - age)
        case ["session"]:
            age = timedelta(seconds=fresh_auth_s + 1)
            return make(Credential.SESSION, session_id="table-session", auth_time=now - age)
        case ["api_key", scope]:
            return make(Credential.API_KEY, key_id="table-key", scopes=(scope,))
        case ["device_token"]:
            return make(Credential.DEVICE_TOKEN, key_id="table-token")
        case ["local_cli"]:
            return make(Credential.LOCAL_CLI)
    raise ValueError(axis)


def truth_table(
    *,
    now: datetime,
    fresh_auth_s: int = DEFAULT_FRESH_AUTH_S,
    restricted_profile_exists: bool = False,
    hardened: bool = False,
    registry: Mapping[str, ReservedAction] | None = None,
) -> tuple[TruthRow, ...]:
    actions = RESERVED_ACTIONS if registry is None else registry
    rows = []
    for action in actions.values():
        needs = action.needs_fresh_auth(
            restricted_profile_exists=restricted_profile_exists, hardened=hardened
        )
        for role in TABLE_ROLES:
            for axis in CREDENTIAL_AXIS:
                verdict = authorize_reserved(
                    sample_principal(role, axis, now=now, fresh_auth_s=fresh_auth_s),
                    action,
                    now=now,
                    fresh_auth_s=fresh_auth_s,
                    restricted_profile_exists=restricted_profile_exists,
                    hardened=hardened,
                    registry=actions,
                )
                column = fresh_auth_s if needs else None
                rows.append(TruthRow(action.name, role, axis, type(verdict).__name__, column))
    return tuple(rows)
