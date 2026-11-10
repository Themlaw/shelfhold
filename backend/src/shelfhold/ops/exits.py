"""The exit commands: one API route per command of the catalogue (EF-INT-023).

`identity.domain.catalogue.EXIT_ROUTES` says which command leaves which waiting state and whether
it is reserved. This module says where each command is served (method and path) and which states
belong to M1a; a test checks the two tables against each other and against the OpenAPI document,
so a state or a command cannot appear in one only.
"""

from types import MappingProxyType
from typing import Final

from shelfhold.events.waiting_state import WaitingState

__all__ = [
    "CLI_PENDING",
    "ENDPOINTS",
    "M1A_STATES",
    "M1B_SERVED_STATES",
    "UNSERVED",
    "bare",
    "endpoint_of",
]

# command (as in EXIT_ROUTES) -> (method, path)
ENDPOINTS: Final = MappingProxyType(
    {
        "missing resolve": ("POST", "/api/v1/missing/{destination_id}/{export_item_id}/resolve"),
        "series resume": ("POST", "/api/v1/series/{export_series_id}/resume"),
        "jobs list": ("GET", "/api/v1/jobs"),
        "jobs retry": ("POST", "/api/v1/jobs/{job_id}/retry"),
        "target reconnect": ("POST", "/api/v1/targets/{target_id}/reconnect"),
        "target recheck": ("POST", "/api/v1/targets/{target_id}/recheck"),
        "target recheck [child_exposure]": ("POST", "/api/v1/targets/{target_id}/recheck"),
        "target rotate-key": ("POST", "/api/v1/targets/{target_id}/rotate-key"),
        "source set-secret": ("POST", "/api/v1/sources/suwayomi/secret"),
        "source recheck": ("POST", "/api/v1/sources/suwayomi/recheck"),
        "source reconnect": ("POST", "/api/v1/sources/suwayomi/reconnect"),
        "adopt --confirm-migration": (
            "POST", "/api/v1/adoption/{destination_id}/confirm-migration",
        ),
        "adopt set-follow-from": ("POST", "/api/v1/adoption/{destination_id}/follow-from"),
        "edition set-lang": ("POST", "/api/v1/editions/{edition_id}/language"),
        "edition set-series-name": ("POST", "/api/v1/editions/{edition_id}/series-name"),
        "reconcile --adopt-after-restore": ("POST", "/api/v1/reconcile/adopt-after-restore"),
        "reconcile --adopt-after-restore [confirm-deletion]": (
            "POST", "/api/v1/reconcile/adopt-after-restore",
        ),
        "secrets import": ("POST", "/api/v1/secrets/import"),
        "tombstone accept-lost-keyring": ("POST", "/api/v1/tombstones/accept-lost-keyring"),
        "tombstone set-aside-journal": ("POST", "/api/v1/tombstones/set-aside-journal"),
        "tombstone flush": ("POST", "/api/v1/tombstones/flush"),
        "rate": ("POST", "/api/v1/works/{work_id}/rating"),
        "root takeover": ("POST", "/api/v1/roots/{root}/takeover"),
        "root reinit": ("POST", "/api/v1/roots/{root}/reinit"),
        "root restored": ("POST", "/api/v1/roots/{root}/restored"),
        "store reinit": ("POST", "/api/v1/store/reinit"),
    }
)  # fmt: skip

# States whose exit is served in M1a. The rest of the enumeration is declared for later milestones
# (the exit arrives with the milestone where the state appears) or has no exit yet (M3).
M1A_STATES: Final = frozenset(
    {
        WaitingState.MISSING,
        WaitingState.PAUSED_FOREIGN_WRITER,
        WaitingState.DEAD_LETTER,
        WaitingState.INSTANCE_CHANGED,
        WaitingState.TARGET_FORBIDDEN,
        WaitingState.KEY_EXPIRING,
        WaitingState.NOT_INDEXED,
        WaitingState.AUTH_EXPIRED,
        WaitingState.ADOPTION_REFUSED,
        WaitingState.PERMISSION_DENIED,
        WaitingState.CHILD_EXPOSURE,
        WaitingState.AUTH_MODE_UNSUPPORTED,
        WaitingState.COVERAGE_UNKNOWN,
        WaitingState.LANGUAGE_UNKNOWN,
        WaitingState.RESTORED_BEHIND,
        WaitingState.TOMBSTONE_KEY_UNKNOWN,
        WaitingState.TOMBSTONE_LINES_OWED,
        WaitingState.ROUTE_MISSING,
        WaitingState.SERIES_NAME_COLLISION,
        WaitingState.MOUNT_SENTINEL_MISSING,
    }
)

# The M1b states whose exit is served here, in a table of their own so that the M1a partition stays
# what it was: `rate` leaves both "À classer" states (EF-INT-023, EF-EXP-048). The other M1b states
# arrive with their lots; `shelf withdraw` (L10) is served by its own router.
M1B_SERVED_STATES: Final = frozenset(
    {
        WaitingState.RATING_PENDING,
        WaitingState.FOLLOW_SUSPENDED,
        WaitingState.SOURCE_INSTANCE_CHANGED,  # `source reconnect` (L18)
        WaitingState.FOREIGN_ROOT,  # `root takeover|reinit` (L25, EF-EXP-064)
        WaitingState.ROOT_LOST,
        WaitingState.ROOT_BEHIND,  # `root restored`
        WaitingState.STORE_LOST,  # `store reinit`
        WaitingState.SAFEGUARD_BACKLOG,  # `jobs list`: the window job plays it (L16)
        WaitingState.TOMBSTONE_JOURNAL_TRUNCATED,  # `tombstone set-aside-journal` (L26, D12)
    }
)

# Commands whose CLI module belongs to a lot still in flight (the route and the service exist
# here). The lot that lands the CLI removes its entry: a test fails while it is listed and the
# command exists.
CLI_PENDING: Final = MappingProxyType[str, str]({})  # L41: every command of the table exists


# Declared by the catalogue, served by no code yet. Empty since `secrets import` (M1b L06) landed;
# the table stays for the next exit that arrives before its service.
UNSERVED: Final = MappingProxyType[str, str]({})


def bare(command: str) -> str:
    """`target recheck [child_exposure]` -> `target recheck`: the case is not typed."""
    return command.split(" [", 1)[0]


def command_of(*words: str) -> str:
    return " ".join(words)


def endpoint_of(command: str) -> tuple[str, str]:
    return ENDPOINTS[command]
