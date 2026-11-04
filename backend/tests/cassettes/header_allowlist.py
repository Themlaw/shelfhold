"""Header allowlist for recorded cassettes (ENF-LEG-002).

Cassettes are JSON: {"interactions": [{"request": {...}, "response": {...}}]},
each side carrying a "headers" mapping. Only allowlisted headers may appear;
credential-bearing headers are always rejected. Re-record with a scrubber that
drops everything else.
"""

from __future__ import annotations

from typing import Any

ALLOWED_HEADERS = frozenset(
    {
        "accept",
        "accept-language",
        "accept-encoding",
        "content-type",
        "content-length",
        "content-language",
        "user-agent",
        "cache-control",
        "etag",
        "last-modified",
        "retry-after",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
    }
)
FORBIDDEN_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
)


def check_cassette(cassette: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    for i, interaction in enumerate(cassette.get("interactions", [])):
        for side in ("request", "response"):
            for name in interaction.get(side, {}).get("headers", {}):
                lower = name.lower()
                if lower in FORBIDDEN_HEADERS:
                    violations.append(f"#{i} {side}: forbidden header {name}")
                elif lower not in ALLOWED_HEADERS:
                    violations.append(f"#{i} {side}: header not allowlisted: {name}")
    return violations
