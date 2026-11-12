"""Reserved actions, defined by property (EF-USR-024, passes 12 M4 and 13 N2, arbitrages P3, P4).

An action is reserved because of what it does (`Effect`), never because it appears in a list:
whoever writes a route that deletes for good, erases `.shelfhold/`, touches secrets or parental
control, confirms a post-restore deletion, rates, unmasks, manages credentials or accepts an
admin risk declares the effect, and the rule follows.

- Any API key (`admin` scope included) and any device token: 403 `credential-not-allowed` (P4).
- A session: allowed; fresh (`auth_time` within `fresh_auth_s`, 900 s by default) only for the
  irreversible or secret-revealing effects, plus parental control when a restricted profile
  exists (P3). The hardened profile asks for freshness on every reserved action.
- A session of a role outside `allowed_roles` (default `admin`), a role the specification does
  not know, or a user without the right to classify for `rate`: 403 `forbidden`.
- An action that is not the registry's own object: refused (closed by default).
- The host CLI: allowed.

The rule applies to actions triggered by a credential. The closed list `SYSTEM_EMITTERS` names the
only internal emitters exempt from it (N2); each acts only on paths the host created itself.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from shelfhold.identity.domain.credentials import Credential, Principal
from shelfhold.identity.domain.roles import KNOWN_ROLES, holds_rate_right

DEFAULT_FRESH_AUTH_S = 900


class Effect(StrEnum):
    DELETES_PERMANENTLY = "deletes_permanently"  # emits an FsOp unlink or empties a trash
    ERASES_RESERVED_DIR = "erases_reserved_dir"  # `.shelfhold/` of a root
    TOUCHES_SECRETS = "touches_secrets"  # reveals, exports or rotates a secret
    CONFIRMS_RESTORE_DELETION = "confirms_restore_deletion"  # EF-BIB-053
    TOUCHES_TOMBSTONES = "touches_tombstones"  # `tombstone lift` (irreversible), `list` (reveals)
    REINITS_STORAGE = "reinits_storage"  # `root takeover|reinit`, `store takeover|reinit`
    PARENTAL_CONTROL = "parental_control"  # max_age_rating, allowed_profiles, profile mapping
    RATES_CONTENT = "rates_content"
    UNMASKS_REQUEST = "unmasks_request"
    MANAGES_CREDENTIALS = "manages_credentials"  # create, rotate or revoke a key or a token
    # `passkey.register`: adds a way in that outlives the session (and its revocation) that added it
    ENROLS_PASSKEY = "enrols_passkey"
    ACCEPTS_ADMIN_RISK = "accepts_admin_risk"  # `--accept-admin-risk`
    # `intents resolve`: completes, replays or gives up an interrupted file operation, which lifts
    # the protection its paths had from the planner and the reconciliation (EF-EXP-041)
    SETTLES_FS_INTENT = "settles_fs_intent"
    # `normalize migrate`: rewrites the identity keys of live rows (a Unit's place), and the check
    # against the tombstones is what stops it from bringing purged content back (EF-BIB-054)
    MIGRATES_IDENTITY = "migrates_identity"
    # `restore acknowledge`: says the passkeys and passwords of a restored database were looked at,
    # which lifts the suspension of passkey logins: the host CLI only, never a key or a session
    ACKNOWLEDGES_RESTORE = "acknowledges_restore"
    # `plugin attest`: a non-first-party plugin's declared trust value (`content_trust:
    # publisher`, `access_partitioning`, `egress`) applied on the admin's word (EF-EXP-065, D15)
    ATTESTS_TRUST = "attests_trust"
    # `revisions apply|force`: asks that exported files be replaced by the revision of a source
    # (the "to rewrite" queue, EF-BIB-043), or that the suspect-revision guard be lifted for one
    # (EF-BIB-024): KOReader positions are reset and the old version only survives in the bin
    REWRITES_EXPORTED_FILES = "rewrites_exported_files"
    # `coverage resplit apply`: says that chapters re-cut from a covered one are carried by its
    # file, which is what presence (I20) reads: no follow cycle or backfill asks for them after it
    # (EF-BIB-044). Nothing is removed, but a wrong answer hides a missing chapter, so it is
    # reserved and fresh like the other decisions on what a library holds
    CHANGES_COVERAGE = "changes_coverage"
    # `contributor set-died-for-france`: a legal fact about a person that decides whether the
    # rights warning of a book shows (EF-SRC-024). Nothing is removed or exposed, but a wrong
    # answer hides a warning, so it is reserved and fresh like the other decisions of the admin
    RECORDS_LEGAL_FACT = "records_legal_fact"


# P3 (inverted by the PR #43 review): freshness applies by default, so a new `Effect` that nobody
# classed is fresh-required (the safe side). Only these effects are listed as NOT needing it, and
# a test pins `irreversible | these == set(Effect)` by hand, so a new effect fails CI until it is
# placed in one of the two lists. PARENTAL_CONTROL is here but conditional: it needs freshness as
# soon as a restricted profile exists. RATES_CONTENT and ENROLS_PASSKEY are NOT here (porteur,
# 2026-11-11): a stolen session must neither file adult content as "0" nor add a passkey that
# survives the revocation of sessions, so both need a session fresher than 900 s in every profile.
NO_REAUTH_EFFECTS = frozenset(
    {
        Effect.PARENTAL_CONTROL,
        Effect.UNMASKS_REQUEST,
        Effect.MANAGES_CREDENTIALS,
        Effect.ATTESTS_TRUST,  # D15: freshness only in the hardened profile
    }  # not ACCEPTS_ADMIN_RISK: it takes a third party's admin password (L38 review)
)

DEFAULT_RESERVED_ROLES = frozenset({"admin"})

# N2: closed list. Adding a name here is a security decision, reviewed as such.
SYSTEM_EMITTERS = frozenset(
    {
        "staging_orphan_cleanup",  # EF-EXP-006, ENF-OPS-009
        "probe_file_cleanup",  # `.shelfhold-probe-*`, EF-EXP-054
        "trash_retention_purge",  # trash older than 30 days, ENF-OPS-009
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
        if hardened or self.effects - NO_REAUTH_EFFECTS:
            return True
        return Effect.PARENTAL_CONTROL in self.effects and restricted_profile_exists


@dataclass(frozen=True)
class Allowed:
    pass


@dataclass(frozen=True)
class CredentialNotAllowed:
    credential: Credential


@dataclass(frozen=True)
class RoleNotAllowed:
    role: str


@dataclass(frozen=True)
class ReauthRequired:
    auth_age_s: int
    max_age_s: int


@dataclass(frozen=True)
class ActionNotDeclared:
    name: str


Verdict = Allowed | CredentialNotAllowed | RoleNotAllowed | ReauthRequired | ActionNotDeclared


def authorize_reserved(
    principal: Principal,
    action: ReservedAction,
    *,
    now: datetime,
    fresh_auth_s: int = DEFAULT_FRESH_AUTH_S,
    restricted_profile_exists: bool = False,
    hardened: bool = False,
    allowed_roles: frozenset[str] = DEFAULT_RESERVED_ROLES,
    registry: Mapping[str, ReservedAction] | None = None,
) -> Verdict:
    """`registry` is `RESERVED_ACTIONS` unless a test brings synthetic actions of its own."""
    if registry is None:
        from shelfhold.identity.domain.catalogue import RESERVED_ACTIONS

        registry = RESERVED_ACTIONS
    if registry.get(action.name) != action:  # an undeclared action, or a look-alike of one
        return ActionNotDeclared(action.name)
    credential = principal.credential
    # `is`, never `==`: a look-alike value must not pass for the enum member
    if credential is not Credential.LOCAL_CLI and credential is not Credential.SESSION:
        return CredentialNotAllowed(credential)  # keys and device tokens, whatever their scope
    # review PR #43 (M4): `admin` unless the route says; the local CLI is held to it too (PR #49).
    # A role outside the specification is refused even when a route lists it (closed by default).
    if principal.role not in allowed_roles or principal.role not in KNOWN_ROLES:
        return RoleNotAllowed(principal.role)
    if Effect.RATES_CONTENT in action.effects and not holds_rate_right(
        principal.role, principal.can_rate
    ):
        return RoleNotAllowed(principal.role)  # EF-BIB-036: classify is a right, not a role
    if Effect.ACKNOWLEDGES_RESTORE in action.effects and credential is not Credential.LOCAL_CLI:
        # a restored instance answers to a password that may be an old, leaked one: the
        # acknowledgement lifts the passkey suspension, so it is made on the host, never over HTTP
        return CredentialNotAllowed(credential)
    if credential is Credential.LOCAL_CLI:
        return Allowed()
    if not action.needs_fresh_auth(
        restricted_profile_exists=restricted_profile_exists, hardened=hardened
    ):
        return Allowed()
    if principal.auth_time is None:
        return ReauthRequired(auth_age_s=-1, max_age_s=fresh_auth_s)
    age = int((now - principal.auth_time).total_seconds())
    if 0 <= age <= fresh_auth_s:
        return Allowed()
    # nothing but the two ages leaves: an `auth_time` in the future is reported as unknown (-1)
    return ReauthRequired(auth_age_s=age if age >= 0 else -1, max_age_s=fresh_auth_s)
