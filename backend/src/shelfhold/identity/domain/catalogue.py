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
    # The way out of `restored_behind` for a root that will not come back (M2 L31): demoted to
    # read-only, typed, audited; a fresh session, never a key
    _action("reconcile.forget_root", Effect.REINITS_STORAGE),
    _action("root.takeover", Effect.REINITS_STORAGE),
    _action("root.reinit", Effect.REINITS_STORAGE),
    _action("store.takeover", Effect.REINITS_STORAGE),
    _action("store.reinit", Effect.REINITS_STORAGE),
    _action("config.path_map", Effect.REINITS_STORAGE),  # which folders are written (`path_map.*`)
    # File-system journal
    _action("intents.resolve", Effect.SETTLES_FS_INTENT),  # EF-INT-014
    # Exported files replaced by a source's revision (EF-BIB-043); `force` lifts the guard of
    # EF-BIB-024 for a quarantined one
    _action("revisions.apply", Effect.REWRITES_EXPORTED_FILES),
    _action("revisions.force", Effect.REWRITES_EXPORTED_FILES),
    # Identity keys
    _action("normalize.migrate", Effect.MIGRATES_IDENTITY),  # EF-BIB-054, ENF-OPS-021
    # `edition merge`: one Edition takes the Units of another (EF-BIB-049); it also decides the
    # age and the filing of what it takes, so it is a parental decision too
    _action("edition.merge", Effect.MIGRATES_IDENTITY, Effect.PARENTAL_CONTROL),
    # `coverage resplit propose|apply`: a re-cut chapter is covered by its old Unit (EF-BIB-044)
    _action("coverage.resplit", Effect.CHANGES_COVERAGE),
    # `contributor set-died-for-france --yes|--no|--unknown` (M2 L15b, EF-SRC-024)
    _action("contributor.set_died_for_france", Effect.RECORDS_LEGAL_FACT),
    # The open export and its import (M3 L37, EF-INT-016): every profile's data leaves, or enters
    _action("export.all", Effect.MOVES_INSTANCE_DATA),
    _action("import.all", Effect.MOVES_INSTANCE_DATA),
    # Parental control
    _action("shelf.max_age_rating", Effect.PARENTAL_CONTROL),
    _action("shelf.allowed_profiles", Effect.PARENTAL_CONTROL),
    _action("profile.kavita_mapping", Effect.PARENTAL_CONTROL),  # EF-USR-017
    _action("target.recheck_child_exposure", Effect.PARENTAL_CONTROL),
    _action("shelf.choose_family", Effect.PARENTAL_CONTROL),  # EF-BIB-036: file on a family shelf
    _action("shelf.withdraw", Effect.PARENTAL_CONTROL),  # EF-BIB-037: apply a removal plan
    # Household profiles (M3 L05, EF-USR-001, EF-USR-005): the role, the ceiling, the right to
    # classify and the per-source policy; a fresh session in every profile, never a key
    _action("profile.create", Effect.MANAGES_PROFILES),
    _action("profile.update", Effect.MANAGES_PROFILES),
    _action("profile.remove", Effect.MANAGES_PROFILES),
    _action("profile.source_policy", Effect.MANAGES_PROFILES),
    # Classing needs a fresh session in every profile (porteur, 2026-11-11), and is refused to
    # every key and token
    _action("rate", Effect.RATES_CONTENT),
    # Without freshness by default (P3), still refused to every key and token
    _action("request.unmask", Effect.UNMASKS_REQUEST),
    _action("keys.create", Effect.MANAGES_CREDENTIALS),
    _action("keys.rotate", Effect.MANAGES_CREDENTIALS),
    _action("keys.revoke", Effect.MANAGES_CREDENTIALS),
    _action(
        "restore.acknowledge", Effect.ACKNOWLEDGES_RESTORE
    ),  # passkeys, passwords after a restore
    _action("device_token.manage", Effect.MANAGES_CREDENTIALS),
    # A passkey is a persistent way in: adding one needs a fresh proof in every profile (porteur,
    # 2026-11-11); retiring one keeps the rule of the other credential actions (hardened only)
    _action("passkey.register", Effect.ENROLS_PASSKEY),
    _action("passkey.revoke", Effect.MANAGES_CREDENTIALS),
    _action("admin_risk.accept", Effect.ACCEPTS_ADMIN_RISK),
    # Maintenance mode (EF-INT-018): every write of the instance stops, so entering it and leaving
    # it are the admin's, on a fresh session or from the host CLI, never a key
    _action("maintenance.enter", Effect.FREEZES_INSTANCE),
    _action("maintenance.exit", Effect.FREEZES_INSTANCE),
    # The queue by hand (EF-INT-017): pause, resume, cancel, priority, replay
    _action("queue.control", Effect.CONTROLS_QUEUE),
    # Cancelling what protects the library (a security repair): fresh in every profile
    _action("queue.cancel_repair", Effect.FREEZES_INSTANCE),
    # Trust boundary (EF-EXP-065, D15): a third-party plugin's declared trust value; for
    # `access_partitioning` it is also parental control (which shelves may be bound to it)
    _action("plugin.attest", Effect.ATTESTS_TRUST),
    _action("plugin.attest_partitioning", Effect.ATTESTS_TRUST, Effect.PARENTAL_CONTROL),
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
    _exit("target reconnect", "library_type_changed"),  # EF-EXP-049: once Kavita's type is back
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
    _exit("reconcile --forget-root", "restored_behind", "reconcile.forget_root"),
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
    # M3
    _exit("maintenance off", "maintenance", "maintenance.exit"),
)
