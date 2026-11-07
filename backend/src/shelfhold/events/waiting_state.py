"""Every state in which Shelfhold waits for a human (EF-INT-023).

Each value has an API route and a CLI command that leaves it; a parametrised test checks it as
the exits arrive. Values are the stable, snake_case wire names. Plugin states live in the
`plugin.<key>.*` namespace and are not members of this enumeration.
"""

from enum import StrEnum


class WaitingState(StrEnum):
    # M1a
    MISSING = "missing"
    PAUSED_FOREIGN_WRITER = "paused_foreign_writer"
    DEAD_LETTER = "dead_letter"
    INSTANCE_CHANGED = "instance_changed"
    TARGET_FORBIDDEN = "target_forbidden"
    KEY_EXPIRING = "key_expiring"
    NOT_INDEXED = "not_indexed"
    AUTH_EXPIRED = "auth_expired"
    ADOPTION_REFUSED = "adoption_refused"
    PERMISSION_DENIED = "permission_denied"
    CHILD_EXPOSURE = "child_exposure"
    AUTH_MODE_UNSUPPORTED = "auth_mode_unsupported"
    COVERAGE_UNKNOWN = "coverage_unknown"
    LANGUAGE_UNKNOWN = "language_unknown"
    RESTORED_BEHIND = "restored_behind"
    TOMBSTONE_KEY_UNKNOWN = "tombstone_key_unknown"
    TOMBSTONE_LINES_OWED = "tombstone_lines_owed"
    ROUTE_MISSING = "route_missing"
    SERIES_NAME_COLLISION = "series_name_collision"
    MOUNT_SENTINEL_MISSING = "mount_sentinel_missing"  # event `mount.sentinel_missing`
    # M1b
    RATING_PENDING = "rating_pending"
    FOLLOW_SUSPENDED = "follow_suspended"
    SAFEGUARD_BACKLOG = "safeguard_backlog"
    FOREIGN_ROOT = "foreign_root"
    ROOT_LOST = "root_lost"
    ROOT_BEHIND = "root_behind"
    STORE_LOST = "store_lost"
    SOURCE_INSTANCE_CHANGED = "source_instance_changed"
    # M3
    LIBRARY_TYPE_CHANGED = "library_type_changed"
