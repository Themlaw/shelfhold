"""Per-series freshness (EF-SRC-026 c): when is a followed series stale?

Stale means no chapter discovered for `k` times the observed cadence (default k = 3), or for 14
days when the series has no measurable cadence. A cadence needs at least two gaps between distinct
discovery instants: a first import discovers a whole backlog at one instant and says nothing about
the rhythm of the series.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from itertools import pairwise
from statistics import median_low

DEFAULT_K = 3
FLOOR = timedelta(days=14)
MIN_GAPS = 2


def cadence(discoveries: Sequence[datetime]) -> timedelta | None:
    instants = sorted(set(discoveries))
    gaps = [later - earlier for earlier, later in pairwise(instants)]
    return median_low(gaps) if len(gaps) >= MIN_GAPS else None


def stale_threshold(observed: timedelta | None, k: int = DEFAULT_K) -> timedelta:
    return observed * k if observed is not None else FLOOR


def is_stale(discoveries: Sequence[datetime], now: datetime, k: int = DEFAULT_K) -> bool:
    if not discoveries:
        return False
    return now - max(discoveries) > stale_threshold(cadence(discoveries), k)
