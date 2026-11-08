"""The Sablier adapter of the `WakeHook` port: the *blocking* strategy of its API.

`GET <url>/api/strategies/blocking?names=<n>&session_duration=<s>&timeout=<t>` starts the named
containers (or a `group`) and answers once they are ready; asking again for the same session
extends it. Sablier answers 200 with the status of each instance, whether they are ready or not
at the end of the delay, so the status is read, not only the code.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import structlog

from shelfhold.infrastructure.fetcher.errors import FetchTimeout
from shelfhold.infrastructure.logging import elapsed_ms
from shelfhold.sdk.connected import WakeError, WakeTimeout
from shelfhold.sdk.fetcher import Fetcher

log = structlog.get_logger(__name__)

STRATEGY_PATH = "/api/strategies/blocking"
_READY = "ready"


@dataclass(frozen=True)
class SablierTarget:
    """What a label means to Sablier: container names, or a group (one of the two)."""

    names: tuple[str, ...] = ()
    group: str | None = None

    def __post_init__(self) -> None:
        if bool(self.names) == bool(self.group):
            raise ValueError("a Sablier target is either names or a group")


def _seconds(delay: timedelta) -> str:
    return f"{max(1, int(delay.total_seconds()))}s"


class SablierHook:
    def __init__(
        self,
        fetcher: Fetcher,
        base_url: str,
        targets: Mapping[str, SablierTarget],
        *,
        session_duration: timedelta,
        timeout: timedelta,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._url = base_url.rstrip("/") + STRATEGY_PATH
        self._targets = dict(targets)
        self._session = session_duration
        self._timeout = timeout
        self._headers = dict(headers or {})  # may carry a token: never logged, never in a repr

    def __repr__(self) -> str:
        return f"SablierHook(targets={sorted(self._targets)})"

    async def wake(self, target: str) -> None:
        await self._ask(target)

    async def renew(self, target: str) -> None:
        await self._ask(target)

    async def _ask(self, target: str) -> None:
        wanted = self._targets.get(target)
        if wanted is None:
            raise WakeError(f"no container is attached to {target!r}")
        params: dict[str, Any] = {
            "session_duration": _seconds(self._session),
            "timeout": _seconds(self._timeout),
        }
        if wanted.names:
            params["names"] = list(wanted.names)
        else:
            params["group"] = wanted.group
        started = time.perf_counter()
        try:
            response = await self._fetcher.get(self._url, params=params, headers=self._headers)
        except Exception as error:  # the Fetcher's refusals and failures: the token is not in them
            log.warning("sablier.failed", target=target, reason=type(error).__name__,
                        duration_ms=elapsed_ms(started))  # fmt: skip
            if isinstance(error, FetchTimeout):
                raise WakeTimeout("Sablier did not answer within the delay") from None
            raise WakeError(f"Sablier could not be reached ({type(error).__name__})") from None
        if not 200 <= response.status < 300:
            log.warning("sablier.refused", target=target, status=response.status)
            raise WakeError(f"Sablier answered {response.status}")
        state = _state_of(response.body)
        if state == "unrecoverable":
            log.warning("sablier.unrecoverable", target=target)
            raise WakeError("Sablier cannot start this container")
        if state != _READY:
            log.info("sablier.not_ready", target=target, duration_ms=elapsed_ms(started))
            raise WakeTimeout("the tool was not ready within the wake delay")
        log.debug("sablier.ready", target=target, duration_ms=elapsed_ms(started))


def _state_of(body: bytes) -> str:
    """`ready` when every instance reports it; the first other status otherwise. An answer whose
    shape says nothing about the instances (a 2xx of the blocking strategy) counts as ready."""
    try:
        document = json.loads(body)
    except ValueError:
        return _READY
    session = document.get("session") if isinstance(document, dict) else None
    instances = session.get("instances") if isinstance(session, dict) else None
    if not isinstance(instances, list):
        return _READY
    statuses = [
        str(item["instance"].get("status", ""))
        for item in instances
        if isinstance(item, dict) and isinstance(item.get("instance"), dict)
    ]
    if not statuses:
        return _READY
    return next((s for s in statuses if s != _READY), _READY)
