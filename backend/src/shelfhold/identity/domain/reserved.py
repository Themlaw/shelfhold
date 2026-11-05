"""Reserved actions, defined by property (EF-USR-024, passes 12 M4 and 13 N2, arbitrages P3, P4).

An action is reserved because of what it does (`Effect`), never because it appears in a list:
whoever writes a route that deletes for good, erases `.shelfhold/`, touches secrets or parental
control, confirms a post-restore deletion, rates, unmasks, manages credentials or accepts an
admin risk declares the effect, and the rule follows.

- Any API key (`admin` scope included) and any device token: 403 `credential-not-allowed` (P4).
- A session: allowed; fresh (`auth_time` within `fresh_auth_s`, 900 s by default) only for the
  irreversible or secret-revealing effects, plus parental control when a restricted profile
  exists (P3). The hardened profile asks for freshness on every reserved action.
- The host CLI: allowed.

The rule applies to actions triggered by a credential. The closed list `SYSTEM_EMITTERS` names the
only internal emitters exempt from it (N2); each acts only on paths the host created itself.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from shelfhold.identity.domain.credentials import Credential, Principal

DEFAULT_FRESH_AUTH_S = 900


class Effect(StrEnum):
    DELETES_PERMANENTLY = "deletes_permanently"  # emits an FsOp unlink or empties a trash
    ERASES_RESERVED_DIR = "erases_reserved_dir"  # `.shelfhold/` of a root
    TOUCHES_SECRETS = "touches_secrets"  # reveals, exports or rotates a secret
    CONFIRMS_RESTORE_DELETION = "confirms_restore_deletion"  # EF-BIB-053
    PARENTAL_CONTROL = "parental_control"  # max_age_rating, allowed_profiles, profile mapping
    RATES_CONTENT = "rates_content"
    UNMASKS_REQUEST = "unmasks_request"
    MANAGES_CREDENTIALS = "manages_credentials"  # create, rotate or revoke a key or a token
    ACCEPTS_ADMIN_RISK = "accepts_admin_risk"  # `--accept-admin-risk`


# P3: re-authentication only for what cannot be undone or reveals a secret.
REAUTH_EFFECTS = frozenset(
    {
        Effect.DELETES_PERMANENTLY,
        Effect.ERASES_RESERVED_DIR,
        Effect.TOUCHES_SECRETS,
        Effect.CONFIRMS_RESTORE_DELETION,
    }
)

# N2: closed list. Adding a name here is a security decision, reviewed as such.
SYSTEM_EMITTERS = frozenset(
    {
        "staging_orphan_cleanup",  # EF-EXP-006, ENF-OPS-009
        "probe_file_cleanup",  # `.shelfhold-probe-*`, EF-EXP-054
        "trash_retention_purge",  # trash older than 30 days, ENF-OPS-009
        "disk_guard_purge",  # EF-BIB-030 (M3), ENF-OPS-006
    }
)


@dataclass(frozen=True)
class ReservedAction:
    name: str
    effects: frozenset[Effect]

    def __post_init__(self) -> None:
        if not self.effects:
            raise ValueError("reserved_action_without_effect")

    def needs_fresh_auth(self, *, restricted_profile_exists: bool, hardened: bool) -> bool:
        if hardened or self.effects & REAUTH_EFFECTS:
            return True
        return Effect.PARENTAL_CONTROL in self.effects and restricted_profile_exists


@dataclass(frozen=True)
class Allowed:
    pass


@dataclass(frozen=True)
class CredentialNotAllowed:
    credential: Credential


@dataclass(frozen=True)
class ReauthRequired:
    auth_age_s: int
    max_age_s: int


Verdict = Allowed | CredentialNotAllowed | ReauthRequired


def authorize_reserved(
    principal: Principal,
    action: ReservedAction,
    *,
    now: datetime,
    fresh_auth_s: int = DEFAULT_FRESH_AUTH_S,
    restricted_profile_exists: bool = False,
    hardened: bool = False,
) -> Verdict:
    match principal.credential:
        case Credential.LOCAL_CLI:
            return Allowed()
        case Credential.SESSION:
            pass
        case _:  # keys and device tokens, whatever their scope
            return CredentialNotAllowed(principal.credential)
    if not action.needs_fresh_auth(
        restricted_profile_exists=restricted_profile_exists, hardened=hardened
    ):
        return Allowed()
    if principal.auth_time is None:
        return ReauthRequired(auth_age_s=-1, max_age_s=fresh_auth_s)
    age = int((now - principal.auth_time).total_seconds())
    if 0 <= age <= fresh_auth_s:
        return Allowed()
    return ReauthRequired(auth_age_s=age, max_age_s=fresh_auth_s)
