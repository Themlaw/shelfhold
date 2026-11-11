"""robots.txt for native sources (ENF-LEG-003, ENF-LEG-007; RFC 9309).

A source whose manifest says `respects_robots` gets a `RobotsPolicy`; the Fetcher asks it before
every hop. The file is fetched through the Fetcher itself (so the same guards apply), parsed once
and kept for a day. Missing file (4xx): everything is allowed. Unavailable (5xx, network error):
nothing is, until the next attempt. The group read is the one for the `Shelfhold` agent token.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final
from urllib.robotparser import RobotFileParser

from shelfhold.infrastructure.fetcher.errors import FetchError, RobotsDisallowed
from shelfhold.sdk.fetcher import Response

ROBOTS_AGENT: Final = "Shelfhold"
ROBOTS_PATH: Final = "/robots.txt"
_TTL_S: Final = 24 * 3600
_RETRY_S: Final = 300
_MAX_BYTES: Final = 512 * 1024

# Fetches an origin's robots.txt; supplied by the Fetcher (None body = could not be read).
RobotsFetch = Callable[[str], Awaitable[Response]]


@dataclass
class _Rules:
    parser: RobotFileParser | None  # None: unavailable, everything refused
    expires: float


@dataclass
class RobotsPolicy:
    clock: Callable[[], float] = time.time
    max_bytes: int = _MAX_BYTES
    _rules: dict[str, _Rules] = field(default_factory=dict)

    async def check(self, origin: str, path_and_query: str, fetch: RobotsFetch) -> None:
        """`origin`: scheme://authority. Raises RobotsDisallowed when the path is not allowed."""
        rules = self._rules.get(origin)
        if rules is None or rules.expires <= self.clock():
            rules = await self._load(origin, fetch)
            self._rules[origin] = rules
        if rules.parser is None or not rules.parser.can_fetch(ROBOTS_AGENT, path_and_query):
            raise RobotsDisallowed(f"robots.txt of {origin} does not allow this path")

    async def _load(self, origin: str, fetch: RobotsFetch) -> _Rules:
        try:
            response = await fetch(origin + ROBOTS_PATH)
        except FetchError:
            return _Rules(None, self.clock() + _RETRY_S)
        if 500 <= response.status < 600:
            return _Rules(None, self.clock() + _RETRY_S)
        parser = RobotFileParser()
        if response.status == 200:
            parser.parse(response.body.decode("utf-8", "replace").splitlines())
        else:  # 4xx: no robots.txt, nothing is restricted
            parser.parse([])
        return _Rules(parser, self.clock() + _TTL_S)
