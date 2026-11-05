"""What the interfaces and the composition root may use from the acquisition context."""

from shelfhold.acquisition.application.backfill import Backfill, BackfillConfirmationRequired
from shelfhold.acquisition.application.failures import FailureLedger, Pause
from shelfhold.acquisition.application.follow import (
    FOLLOW_JOB_KIND,
    FollowCycle,
    FollowReport,
    follow_dedupe_key,
    follow_job,
    submit_follow,
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
from shelfhold.acquisition.application.tickets import (
    DownloadTickets,
    Placement,
    Receiver,
    TicketReport,
    bounded_receiver,
)
from shelfhold.acquisition.application.unit_follow import suppress_follow
from shelfhold.acquisition.domain.backfill import BackfillEstimate
from shelfhold.acquisition.domain.failures import Disposition, classify, disposition_of
from shelfhold.acquisition.domain.freshness import DEFAULT_K, cadence, is_stale, stale_threshold
from shelfhold.acquisition.domain.requests import RequestBudget
from shelfhold.acquisition.domain.team_policy import TeamChoice, TeamStats, choose_team
from shelfhold.acquisition.domain.watermark import Watermark

__all__ = [
    "DEFAULT_K",
    "FOLLOW_JOB_KIND",
    "Backfill",
    "BackfillConfirmationRequired",
    "BackfillEstimate",
    "Disposition",
    "DownloadTickets",
    "FailureLedger",
    "FollowCycle",
    "FollowReport",
    "Pause",
    "Placement",
    "RangeReport",
    "RangeRequest",
    "RangeTickets",
    "RangeUnit",
    "Receiver",
    "RequestBudget",
    "TeamChoice",
    "TeamPreference",
    "TeamStats",
    "TicketReport",
    "Watermark",
    "bounded_receiver",
    "cadence",
    "choose_team",
    "classify",
    "disposition_of",
    "follow_dedupe_key",
    "follow_job",
    "is_stale",
    "pending",
    "present_for_acquisition",
    "present_in",
    "stale_threshold",
    "submit_follow",
    "suppress_follow",
]
