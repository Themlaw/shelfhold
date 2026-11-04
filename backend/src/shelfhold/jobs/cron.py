"""Five-field cron (minute hour day-of-month month day-of-week), read in Europe/Paris.

Local wall-clock rules around a change of hour: a time that does not exist (02:30 on the spring
day) runs once, shifted past the gap; a time that happens twice (02:30 on the autumn day) runs
once, the first time, unless the cron runs at least hourly (`frequent`), which keeps the real
cadence and runs in both passes. Results are UTC instants.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from shelfhold.jobs.window import PARIS

_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_SEARCH_DAYS = 366 * 5  # enough for "February 29"


class CronError(ValueError):
    """The expression is not a valid five-field cron."""


def _field(text: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        body, _, step_text = part.partition("/")
        if step_text and not step_text.isdigit():
            raise CronError(f"bad step in {part!r}")
        step = int(step_text) if step_text else 1
        if step < 1:
            raise CronError(f"step must be positive in {part!r}")
        if body == "*":
            first, last = low, high
        else:
            lo_text, dash, hi_text = body.partition("-")
            if not lo_text.isdigit() or (dash and not hi_text.isdigit()):
                raise CronError(f"bad value {part!r}")
            first = int(lo_text)
            last = int(hi_text) if dash else (high if step_text else first)
        if not (low <= first <= last <= high):
            raise CronError(f"{part!r} is outside {low}-{high} or reversed")
        values.update(range(first, last + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class CronSpec:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    days_any: bool
    weekdays_any: bool

    @classmethod
    def parse(cls, text: str) -> "CronSpec":
        parts = text.split()
        if len(parts) != 5:
            raise CronError("expected five fields: minute hour day month weekday")
        minute, hour, day, month, weekday = (
            _field(part, low, high) for part, (low, high) in zip(parts, _BOUNDS, strict=True)
        )
        return cls(
            minute,
            hour,
            day,
            month,
            frozenset(d % 7 for d in weekday),
            days_any=parts[2] == "*",
            weekdays_any=parts[4] == "*",
        )

    def _day_matches(self, day_of_month: int, weekday_sunday_zero: int) -> bool:
        in_days = day_of_month in self.days
        in_weekdays = weekday_sunday_zero in self.weekdays
        if self.days_any and self.weekdays_any:
            return True
        if self.days_any:
            return in_weekdays
        if self.weekdays_any:
            return in_days
        return in_days or in_weekdays  # both restricted: either, as classic cron

    @property
    def frequent(self) -> bool:
        """Runs at least hourly: its cadence is real time, so a repeated hour runs twice."""
        return len(self.minutes) > 1 or len(self.hours) == 24

    def _day_instants(self, day: date, tz: ZoneInfo) -> set[datetime]:
        instants: set[datetime] = set()
        for h in self.hours:
            for m in self.minutes:
                local = datetime.combine(day, time(h, m), tzinfo=tz)
                instants.add(local.astimezone(UTC))
                if self.frequent:  # autumn: the second pass through 02:00-03:00 local
                    second = local.replace(fold=1).astimezone(UTC)
                    if second.astimezone(tz).replace(tzinfo=None) == local.replace(tzinfo=None):
                        instants.add(second)  # skipped for a time that does not exist
        return instants

    def next_after(self, after: datetime, tz: ZoneInfo = PARIS) -> datetime:
        """The first occurrence strictly after `after`, as a UTC instant."""
        after = after.astimezone(UTC)
        day = after.astimezone(tz).date()
        for _ in range(_SEARCH_DAYS):
            if day.month in self.months and self._day_matches(day.day, (day.weekday() + 1) % 7):
                found = sorted(self._day_instants(day, tz))
                for instant in found:
                    if instant > after:
                        return instant
            day += timedelta(days=1)
        raise CronError("no occurrence in the next five years")
