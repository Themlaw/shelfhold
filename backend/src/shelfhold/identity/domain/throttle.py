"""Login attempt limiting: backoff per address and per account, never a hard lock (ENF-SEC-008).

- Per address: 10 failures in a sliding minute, then 429 until the oldest failure is a minute old.
  IPv6 addresses count per /64 (one subscriber usually owns a whole /64).
- Per account: 30 failures in a sliding minute, from any address. It is three times what one
  address may try, so a single address can never keep someone else's account out; the backoff
  ends one minute after the burst. A distributed attacker can still delay the web login of that
  account while the burst lasts (residual risk, docs/admin/durcir.md): sessions already open,
  API keys and the host CLI are unaffected.
- An attempt is counted **before** its password is checked, under a lock (`reserve`), so that
  concurrent requests cannot all slip under the limit; a success withdraws that one attempt
  (`release`) and nothing else: an attacker who owns one account must not be able to reset the
  counter of their address between guesses at another. A throttled attempt is not counted
  (retrying does not extend the wait) and its password is not checked (no oracle).
- A browser that already logged in to the account (signed device cookie) skips the per-account
  counter, never the per-address one: a third party cannot keep the owner out (OWASP "device
  cookies").

State lives in memory: one process per instance (`/data/.lock`); a restart forgets it.
"""

import ipaddress
import threading
from collections import OrderedDict, deque
from datetime import datetime, timedelta

WINDOW = timedelta(minutes=1)
PER_ADDRESS = 10
PER_ACCOUNT = 30
DEFAULT_MAX_KEYS = 10_000


def address_key(ip: str) -> str:
    try:
        parsed = ipaddress.ip_address(ip)
    except ValueError:
        return f"ip:{ip[:64]}"
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.ipv4_mapped is not None:
            return f"ip:{parsed.ipv4_mapped}"
        return "ip:" + str(ipaddress.ip_network(f"{parsed}/64", strict=False))
    return f"ip:{parsed}"


class LoginThrottle:
    def __init__(
        self,
        *,
        per_address: int = PER_ADDRESS,
        per_account: int = PER_ACCOUNT,
        window: timedelta = WINDOW,
        max_keys: int = DEFAULT_MAX_KEYS,
    ) -> None:
        self._limits = {"ip": per_address, "account": per_account}
        self._window = window
        self._max_keys = max_keys
        self._failures: OrderedDict[str, deque[datetime]] = OrderedDict()
        self._lock = threading.Lock()

    def _keys(self, ip: str, account: str) -> tuple[tuple[str, int], tuple[str, int]]:
        return (
            (address_key(ip), self._limits["ip"]),
            ("account:" + account.casefold()[:256], self._limits["account"]),
        )

    def _recent(self, key: str, now: datetime) -> deque[datetime] | None:
        stamps = self._failures.get(key)
        if stamps is None:
            return None
        while stamps and now - stamps[0] >= self._window:
            stamps.popleft()
        if not stamps:
            del self._failures[key]
            return None
        return stamps

    def _wait(self, ip: str, account: str, now: datetime, *, skip_account: bool) -> float | None:
        waits: list[float] = []
        for key, limit in self._keys(ip, account)[: 1 if skip_account else 2]:
            stamps = self._recent(key, now)
            if stamps is not None and len(stamps) >= limit:
                waits.append((stamps[-limit] + self._window - now).total_seconds())
        return max(waits) if waits else None

    def _record(self, ip: str, account: str, now: datetime, *, skip_account: bool = False) -> None:
        for key, _limit in self._keys(ip, account)[: 1 if skip_account else 2]:
            stamps = self._failures.setdefault(key, deque(maxlen=max(self._limits.values())))
            stamps.append(now)
            self._failures.move_to_end(key)
        while len(self._failures) > self._max_keys:
            self._failures.popitem(last=False)  # the least recently failed key goes first

    def retry_after(
        self, *, ip: str, account: str, now: datetime, skip_account: bool = False
    ) -> float | None:
        """Seconds to wait before the next attempt, or None when it may proceed."""
        with self._lock:
            return self._wait(ip, account, now, skip_account=skip_account)

    def reserve(
        self, *, ip: str, account: str, now: datetime, skip_account: bool = False
    ) -> float | None:
        """Atomically: refuse (seconds to wait) or count this attempt as a failure (None)."""
        with self._lock:
            wait = self._wait(ip, account, now, skip_account=skip_account)
            if wait is None:
                self._record(ip, account, now, skip_account=skip_account)
            return wait

    def release(self, *, ip: str, account: str, at: datetime, skip_account: bool = False) -> None:
        """Withdraw the attempt reserved at `at` (its password was right), from the same keys."""
        with self._lock:
            for key, _limit in self._keys(ip, account)[: 1 if skip_account else 2]:
                stamps = self._failures.get(key)
                if stamps is not None and at in stamps:
                    stamps.remove(at)
