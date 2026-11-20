"""The update checker (ENF-GOV-004, ADR-0029, ADR-0030): off by default, never in the way.

It reads one small JSON document, `{"version": "X.Y.Z"}`, at the address the administrator gave
(`update_check_url`). Nothing is sent that names the instance: the address as given (no query, no
suffix), no header of its own, no body, and not even the running version (the comparison is made
here, not there). The answer is data: only a plain `X.Y.Z` is kept, the rest is ignored. Whatever
goes wrong (the forge is gone, the host is blocked, the answer is not what was expected) is one
`info` line and a status `failed`: the checker never raises into the application, never blocks, and
installs nothing (it informs; the update itself is the administrator's `docker compose pull`).

The client is the host's guarded Fetcher (SSRF guard at every hop), with a User-Agent of its own
that carries no version (`UPDATE_USER_AGENT`) and a size bound applied while the body is read, not
after (`update_fetcher`)."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Final, Literal, Protocol

import structlog
from shelfhold_sdk.fetcher import Response

from shelfhold.config import Settings
from shelfhold.infrastructure.fetcher import FetchLimits, GuardedFetcher, Network

log = structlog.get_logger()

MAX_BYTES: Final = 16 * 1024  # a version document, not a page
DEFAULT_TIMEOUT_S: Final = 10.0
# The product, nothing else: no version, no contact, nothing of the instance (ADR-0030). The
# Fetcher of the metadata providers says who is asking and which version; this one must not.
UPDATE_USER_AGENT: Final = "shelfhold-update-check"
_VERSION: Final = re.compile(r"(\d{1,4})\.(\d{1,4})\.(\d{1,4})")

State = Literal["off", "ok", "failed"]


class _Reader(Protocol):
    async def get(self, url: str) -> Response: ...


@dataclass(frozen=True)
class UpdateStatus:
    state: State
    latest: str | None = None
    newer: bool = False


def update_fetcher(network: Network) -> GuardedFetcher:
    """The one client of the update check: guarded, neutral agent, bounded while it reads."""
    limits = FetchLimits(max_bytes=MAX_BYTES, total_timeout=DEFAULT_TIMEOUT_S, max_redirects=2)
    return GuardedFetcher(
        network.resolver,
        limits=limits,
        transport=network.transport,
        user_agent=UPDATE_USER_AGENT,
        quiet_failures=True,  # a failure is an `info` line here, not an incident
    )


def update_check_enabled(settings: Settings) -> bool:
    return settings.update_check_url is not None


def _triple(version: str) -> tuple[int, int, int] | None:
    found = _VERSION.fullmatch(version)
    return None if found is None else (int(found[1]), int(found[2]), int(found[3]))


def _core(version: str) -> str:
    """`1.1.0rc2` counts as 1.1.0 for the comparison: a candidate is not older than its release."""
    return re.split(r"[^0-9.]", version, maxsplit=1)[0]


def _failed(reason: str) -> UpdateStatus:
    log.info("update_check.failed", reason=reason)
    return UpdateStatus("failed")


async def check_for_update(
    fetcher: _Reader,
    url: str | None,
    *,
    current: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> UpdateStatus:
    """One read of the version document. `url=None` (the default): no request at all."""
    if url is None:
        return UpdateStatus("off")
    try:
        response = await asyncio.wait_for(fetcher.get(url), timeout=timeout_s)
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        return _failed("timeout")
    except Exception as exc:  # the forge is gone, the host is refused, the network is down
        return _failed(type(exc).__name__)
    if response.status != 200:
        return _failed(f"status {response.status}")
    if len(response.body) > MAX_BYTES:
        return _failed("too large")
    try:
        document = json.loads(response.body)
        version = document.get("version") if isinstance(document, dict) else None
        latest = _triple(version) if isinstance(version, str) else None
        if latest is None or not isinstance(version, str):
            return _failed("no version")
        mine = _triple(_core(current))
    except Exception as exc:  # not JSON, too deeply nested (RecursionError), not text: all silent
        return _failed(type(exc).__name__)
    return UpdateStatus("ok", latest=version, newer=mine is not None and latest > mine)
