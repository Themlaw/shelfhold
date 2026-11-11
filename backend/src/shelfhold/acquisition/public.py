"""What the interfaces and the composition root may use from the acquisition context."""

from shelfhold.acquisition.application.acquire import (
    ACQUIRE_JOB_KIND,
    AcquireOutcome,
    AcquireRefused,
    NativeAcquirer,
    Prepared,
    SourceDirectory,
    acquire_dedupe_key,
)
from shelfhold.acquisition.application.backfill import Backfill, BackfillConfirmationRequired
from shelfhold.acquisition.application.discovery import decline_pairing
from shelfhold.acquisition.application.failures import FailureLedger, Pause
from shelfhold.acquisition.application.follow import (
    FOLLOW_JOB_KIND,
    FollowCycle,
    FollowReport,
    follow_dedupe_key,
    follow_job,
    submit_follow,
)
from shelfhold.acquisition.application.follow_request import (
    FollowConfirmationRequired,
    FollowOutcome,
    FollowRefused,
    FollowRequests,
    ServiceLink,
    ServiceNotConnectedError,
)
from shelfhold.acquisition.application.import_target import IMPORT_PLUGIN, ImportTargets
from shelfhold.acquisition.application.lncrawl_ranges import (
    MAX_COVERED,
    MAX_TICKET_AGE,
    RangeAnswerRefused,
    epub_only,
    ticket_expired,
    verify_range_answer,
)
from shelfhold.acquisition.application.planning import pending
from shelfhold.acquisition.application.preference import TeamPreference
from shelfhold.acquisition.application.presence import present_for_acquisition, present_in
from shelfhold.acquisition.application.ranges import (
    RangeReport,
    RangeRequest,
    RangeTickets,
    RangeUnit,
)
from shelfhold.acquisition.application.staleness import StaleSeries, stale_series
from shelfhold.acquisition.application.tickets import (
    CycleAbandoned,
    DownloadTickets,
    LeasedReceiver,
    Placement,
    Receiver,
    TicketReport,
    bounded_receiver,
    oriented_name,
)
from shelfhold.acquisition.application.unit_follow import suppress_follow
from shelfhold.acquisition.application.upstream import (
    Detacher,
    DetachReport,
    DetachScope,
    SnapshotRefusedError,
    TakeReport,
    UpstreamSnapshots,
)
from shelfhold.acquisition.domain.backfill import BackfillEstimate
from shelfhold.acquisition.domain.failures import Disposition, classify, disposition_of
from shelfhold.acquisition.domain.freshness import DEFAULT_K, cadence, is_stale, stale_threshold
from shelfhold.acquisition.domain.requests import RequestBudget
from shelfhold.acquisition.domain.team_policy import TeamChoice, TeamStats, choose_team
from shelfhold.acquisition.domain.watermark import Watermark

__all__ = [
    "ACQUIRE_JOB_KIND",
    "DEFAULT_K",
    "FOLLOW_JOB_KIND",
    "IMPORT_PLUGIN",
    "MAX_COVERED",
    "MAX_TICKET_AGE",
    "AcquireOutcome",
    "AcquireRefused",
    "Backfill",
    "BackfillConfirmationRequired",
    "BackfillEstimate",
    "CycleAbandoned",
    "DetachReport",
    "DetachScope",
    "Detacher",
    "Disposition",
    "DownloadTickets",
    "FailureLedger",
    "FollowConfirmationRequired",
    "FollowCycle",
    "FollowOutcome",
    "FollowRefused",
    "FollowReport",
    "FollowRequests",
    "ImportTargets",
    "LeasedReceiver",
    "NativeAcquirer",
    "Pause",
    "Placement",
    "Prepared",
    "RangeAnswerRefused",
    "RangeReport",
    "RangeRequest",
    "RangeTickets",
    "RangeUnit",
    "Receiver",
    "RequestBudget",
    "ServiceLink",
    "ServiceNotConnectedError",
    "SnapshotRefusedError",
    "SourceDirectory",
    "StaleSeries",
    "TakeReport",
    "TeamChoice",
    "TeamPreference",
    "TeamStats",
    "TicketReport",
    "UpstreamSnapshots",
    "Watermark",
    "acquire_dedupe_key",
    "bounded_receiver",
    "cadence",
    "choose_team",
    "classify",
    "decline_pairing",
    "disposition_of",
    "epub_only",
    "follow_dedupe_key",
    "follow_job",
    "is_stale",
    "oriented_name",
    "pending",
    "present_for_acquisition",
    "present_in",
    "stale_series",
    "stale_threshold",
    "submit_follow",
    "suppress_follow",
    "ticket_expired",
    "verify_range_answer",
]
