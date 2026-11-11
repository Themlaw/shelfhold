"""The declared reserved actions and the classed exit commands (EF-USR-024, EF-INT-023, N2).

`RESERVED_ACTIONS` is the registry the truth table is generated from: a route or command that is
reserved declares its action here with its effects, and the rule follows from the effects. The list
is the enumeration of the specification (P3 and the other reserved effects); a routes' declaration
elsewhere must use these objects, so the name and the effects cannot drift apart.

`EXIT_ROUTES` classes the commands that leave a waiting state (EF-INT-023): reserved (fresh
session or local CLI, never a key) or not. A conditional exit gets one route per case.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from shelfhold.identity.domain.reserved import Effect, ReservedAction


def _action(name: str, *effects: Effect) -> ReservedAction:
    return ReservedAction(name, frozenset(effects))


_DECLARED = (
    # Permanent deletion
    _action("purge.now", Effect.DELETES_PERMANENTLY),
    _action("trash.empty", Effect.DELETES_PERMANENTLY),  # EF-BIB-030
    _action("unit.delete", Effect.DELETES_PERMANENTLY),  # EF-BIB-031: every managed copy
    _action("detach.full", Effect.DELETES_PERMANENTLY),  # EF-EXP-031
    # EF-SRC-009: turning on the erasure of the upstream download after a verified export. The
    # nightly deed acts only on a shelf where this was said, and still obeys EF-BIB-038.
    _action("shelf.purge_upstream", Effect.DELETES_PERMANENTLY),
    _action("destination.remove_all", Effect.DELETES_PERMANENTLY),
    _action("reserved_dir.erase", Effect.ERASES_RESERVED_DIR),
    _action("reconcile.confirm_deletion", Effect.CONFIRMS_RESTORE_DELETION),  # EF-BIB-053
    # Secrets
    _action("secrets.export", Effect.TOUCHES_SECRETS),
    _action("secrets.rotate", Effect.TOUCHES_SECRETS),
    _action("secrets.import", Effect.TOUCHES_SECRETS),
    _action("source.set_secret", Effect.TOUCHES_SECRETS),
    _action("target.rotate_key", Effect.TOUCHES_SECRETS),
    # Tombstones and storage
    _action("tombstone.lift", Effect.TOUCHES_TOMBSTONES),
    _action("tombstone.list", Effect.TOUCHES_TOMBSTONES),
    _action("tombstone.accept_lost_keyring", Effect.TOUCHES_TOMBSTONES),
    _action("tombstone.set_aside_journal", Effect.TOUCHES_TOMBSTONES),
    _action("tombstone.rekey", Effect.TOUCHES_TOMBSTONES),  # EF-BIB-054 (10): after a key leak
    _action("root.takeover", Effect.REINITS_STORAGE),
    _action("root.reinit", Effect.REINITS_STORAGE),
    _action("store.takeover", Effect.REINITS_STORAGE),
    _action("store.reinit", Effect.REINITS_STORAGE),
    _action("config.path_map", Effect.REINITS_STORAGE),  # which folders are written (`path_map.*`)
    # File-system journal
    _action("intents.resolve", Effect.SETTLES_FS_INTENT),  # EF-INT-014
    # Identity keys
    _action("normalize.migrate", Effect.MIGRATES_IDENTITY),  # EF-BIB-054, ENF-OPS-021
    # Parental control
    _action("shelf.max_age_rating", Effect.PARENTAL_CONTROL),
    _action("shelf.allowed_profiles", Effect.PARENTAL_CONTROL),
    _action("profile.kavita_mapping", Effect.PARENTAL_CONTROL),  # EF-USR-017
    _action("target.recheck_child_exposure", Effect.PARENTAL_CONTROL),
    _action("shelf.choose_family", Effect.PARENTAL_CONTROL),  # EF-BIB-036: file on a family shelf
    _action("shelf.withdraw", Effect.PARENTAL_CONTROL),  # EF-BIB-037: apply a removal plan
    # Without freshness by default (P3), still refused to every key and token
    _action("rate", Effect.RATES_CONTENT),
    _action("request.unmask", Effect.UNMASKS_REQUEST),
    _action("keys.create", Effect.MANAGES_CREDENTIALS),
    _action("keys.rotate", Effect.MANAGES_CREDENTIALS),
    _action("keys.revoke", Effect.MANAGES_CREDENTIALS),
    _action(
        "restore.acknowledge", Effect.MANAGES_CREDENTIALS
    ),  # passkeys, passwords after a restore
    _action("device_token.manage", Effect.MANAGES_CREDENTIALS),
    # A passkey is a persistent way in: adding or retiring one needs a fresh proof in the hardened
    # profile only (review PR #95; the default is the porteur's call, docs/decisions/M1b.md)
    _action("passkey.register", Effect.MANAGES_CREDENTIALS),
    _action("passkey.revoke", Effect.MANAGES_CREDENTIALS),
    _action("admin_risk.accept", Effect.ACCEPTS_ADMIN_RISK),
)

RESERVED_ACTIONS: Mapping[str, ReservedAction] = MappingProxyType(
    {action.name: action for action in _DECLARED}
)
if len(RESERVED_ACTIONS) != len(_DECLARED):  # pragma: no cover (import-time guard)
    raise RuntimeError("two reserved actions share a name")


@dataclass(frozen=True)
class ExitRoute:
    command: str  # as typed; a conditional exit carries its case in brackets
    waiting_state: str  # a `WaitingState` value
    reserved: ReservedAction | None  # None: not reserved (a key may run it)


def _exit(command: str, state: str, action: str | None = None) -> ExitRoute:
    return ExitRoute(command, state, RESERVED_ACTIONS[action] if action else None)


EXIT_ROUTES: tuple[ExitRoute, ...] = (
    # M1a
    _exit("missing resolve", "missing"),
    _exit("series resume", "paused_foreign_writer"),
    _exit("jobs list", "dead_letter"),
    _exit("jobs retry", "dead_letter"),
    _exit("target reconnect", "instance_changed"),
    _exit("target reconnect", "target_forbidden"),
    _exit("target rotate-key", "key_expiring", "target.rotate_key"),
    _exit("jobs retry", "not_indexed"),
    _exit("source set-secret", "auth_expired", "source.set_secret"),
    _exit("adopt --confirm-migration", "adoption_refused"),
    _exit("target recheck", "permission_denied"),
    _exit("target recheck [child_exposure]", "child_exposure", "target.recheck_child_exposure"),
    _exit("source recheck", "auth_mode_unsupported"),
    _exit("adopt set-follow-from", "coverage_unknown"),
    _exit("edition set-lang", "language_unknown"),
    _exit("reconcile --adopt-after-restore", "restored_behind"),
    _exit(
        "reconcile --adopt-after-restore [confirm-deletion]",
        "restored_behind",
        "reconcile.confirm_deletion",
    ),
    _exit("secrets import", "tombstone_key_unknown", "secrets.import"),
    _exit(
        "tombstone accept-lost-keyring", "tombstone_key_unknown", "tombstone.accept_lost_keyring"
    ),
    _exit("tombstone set-aside-journal", "restored_behind", "tombstone.set_aside_journal"),
    _exit("tombstone flush", "tombstone_lines_owed"),
    _exit("target recheck", "route_missing"),
    _exit("edition set-series-name", "series_name_collision"),
    _exit("target recheck", "mount_sentinel_missing"),
    # M1b
    _exit("rate", "rating_pending", "rate"),
    _exit("rate", "follow_suspended", "rate"),
    _exit("jobs list", "safeguard_backlog"),
    _exit("root takeover", "foreign_root", "root.takeover"),
    _exit("root reinit", "foreign_root", "root.reinit"),
    _exit("root takeover", "root_lost", "root.takeover"),
    _exit("root reinit", "root_lost", "root.reinit"),
    _exit("root takeover", "root_behind", "root.takeover"),
    _exit("root reinit", "root_behind", "root.reinit"),
    _exit("root restored", "root_behind"),
    _exit(
        "tombstone set-aside-journal", "tombstone_journal_truncated", "tombstone.set_aside_journal"
    ),  # D12: the journal is written again from the table
    _exit("store reinit", "store_lost", "store.reinit"),
    _exit("source reconnect", "source_instance_changed"),
    _exit("shelf withdraw", "withdrawal_pending", "shelf.withdraw"),
    _exit("restore acknowledge", "restored_credentials", "restore.acknowledge"),
)

# States whose exit command is not specified yet (M3): the test fails when one gets routes.
EXIT_STATES_LATER = frozenset({"library_type_changed"})
