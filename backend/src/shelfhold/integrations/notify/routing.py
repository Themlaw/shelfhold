"""When a notification leaves, as pure rules (EF-INT-021, D16): no database, no clock of its own.

A person is told in one of two ways. A **digest** leaves once a day at 08:00 in their own time
zone. An **immediate** one leaves at once, and only two things are ever immediate: "your request
arrived" and "an outage over one hour" (the latter for the admin alone, the one who can act). A
profile may choose the `immediate` mode for everything else, which is its own choice.

**Quiet hours** (21:00 to 07:00 unless the profile set its own) delay what is not an outage: a
digest whose hour falls inside them, and an arrival, wait for their end. An outage over one hour
is the exception, because it is the one thing that cannot wait for the morning.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Final, Literal
from zoneinfo import ZoneInfo

DIGEST_HOUR: Final = 8
DEFAULT_QUIET: Final = ("21:00", "07:00")
OUTAGE_AFTER: Final = timedelta(hours=1)
# Failures of one kind that are further apart than this are two outages: it recovered in between.
OUTAGE_GAP: Final = timedelta(hours=2)

Kind = Literal["request_arrived", "chapters", "outage"]

OUTAGE_EVENTS: Final = frozenset({"target.unreachable", "source.broken"})
_KINDS: Final[dict[str, Kind]] = {
    "ops.request_arrived": "request_arrived",
    "unit.stored": "chapters",
    **{event: "outage" for event in OUTAGE_EVENTS},
}


@dataclass(frozen=True)
class Recipient:
    user_id: str
    role: str
    timezone: str
    notify_mode: str
    quiet_start: str | None
    quiet_end: str | None
    username: str = ""


def classify(event_type: str) -> Kind | None:
    """What a person is told about an event of this type, or None: not a per-person notice."""
    return _KINDS.get(event_type)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # an unknown or malformed name reads as UTC, never as an error
        return ZoneInfo("UTC")


def _clock(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def effective_quiet(person: Recipient) -> tuple[str, str]:
    """The quiet hours in force: the profile's own, else the default of the instance."""
    if person.quiet_start and person.quiet_end:
        return person.quiet_start, person.quiet_end
    return DEFAULT_QUIET


def _local(at: datetime, zone: ZoneInfo, clock: time, *, day_offset: int = 0) -> datetime:
    """The UTC instant at which the wall clock of `zone` shows `clock` on the local day of `at`
    (plus `day_offset` days). The date is moved by the calendar, not by 24 hours, so that a change
    of summer time never shifts the hour people expect."""
    day = at.astimezone(zone).date() + timedelta(days=day_offset)
    return datetime.combine(day, clock, tzinfo=zone).astimezone(UTC)


def hold_until(person: Recipient, at: datetime) -> datetime | None:
    """The end of the quiet hours that cover `at`, or None when `at` is outside them. The start
    is closed and the end open: at the end hour sharp the person is awake."""
    zone = _zone(person.timezone)
    start_text, end_text = effective_quiet(person)
    start, end = _clock(start_text), _clock(end_text)
    now = at.astimezone(zone)
    wall = now.time().replace(second=0, microsecond=0, tzinfo=None)
    if start < end:
        inside = start <= wall < end
        ends = _local(at, zone, end)
    elif start > end:  # crosses midnight
        inside = wall >= start or wall < end
        ends = _local(at, zone, end, day_offset=1 if wall >= start else 0)
    else:
        return None  # a window of no length holds nothing
    return ends if inside else None


def next_digest_at(person: Recipient, now: datetime) -> datetime:
    """The next 08:00 of the person's local day, strictly after `now`."""
    zone = _zone(person.timezone)
    today = _local(now, zone, time(DIGEST_HOUR))
    return today if today > now else _local(now, zone, time(DIGEST_HOUR), day_offset=1)


def delivery_time(
    person: Recipient, *, urgent: bool, now: datetime, bypass_quiet: bool = False
) -> datetime:
    """When a notification due `now` (urgent) or at the next digest hour leaves, quiet hours
    included. `bypass_quiet` is for the outage only."""
    due = now if urgent else next_digest_at(person, now)
    if bypass_quiet:
        return due
    return hold_until(person, due) or due
