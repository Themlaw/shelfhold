"""Header allowlist for recorded cassettes (ENF-LEG-002).

Cassettes are JSON: {"interactions": [{"request": {...}, "response": {...}}]},
each side carrying a "headers" mapping. Only allowlisted headers may appear;
credential-bearing headers are always rejected. Re-record with a scrubber that
drops everything else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

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
        "date",
        "server",
        "vary",
        "content-encoding",
    }
)
ALLOWED_HEADER_PREFIXES = ("x-ratelimit-",)
SENSITIVE_QUERY_PARAMS = frozenset({"api_key", "token", "key", "password", "auth"})
ALLOWED_FILES = frozenset({"header_allowlist.py", "__init__.py"})
FORBIDDEN_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key"}
)


def is_allowed_file(path: Path) -> bool:
    return path.suffix == ".json" or path.name in ALLOWED_FILES


def _check_headers(where: str, headers: object) -> list[str]:
    if not isinstance(headers, dict):
        return [f"{where}: headers must be a dict"]
    out: list[str] = []
    for name in headers:
        lower = str(name).lower()
        if lower in FORBIDDEN_HEADERS:
            out.append(f"{where}: forbidden header {name}")
        elif lower not in ALLOWED_HEADERS and not lower.startswith(ALLOWED_HEADER_PREFIXES):
            out.append(f"{where}: header not allowlisted: {name}")
    return out


def check_cassette(cassette: dict[str, Any]) -> list[str]:
    interactions = cassette.get("interactions")
    if not isinstance(interactions, list):
        return ["'interactions' missing or not a list"]
    violations: list[str] = []
    for i, interaction in enumerate(interactions):
        if not isinstance(interaction, dict):
            violations.append(f"#{i}: interaction must be a dict")
            continue
        for side in ("request", "response"):
            part = interaction.get(side)
            where = f"#{i} {side}"
            if not isinstance(part, dict):
                violations.append(f"{where}: must be a dict")
                continue
            violations += _check_headers(where, part.get("headers", {}))
        request = interaction.get("request")
        uri = request.get("uri", "") if isinstance(request, dict) else ""
        for key, _ in parse_qsl(urlsplit(str(uri)).query, keep_blank_values=True):
            if key.lower() in SENSITIVE_QUERY_PARAMS:
                violations.append(f"#{i} request: sensitive query parameter {key}")
    return violations
