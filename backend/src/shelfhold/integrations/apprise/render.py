"""The text of a notification (ENF-SEC-015, EF-INT-006).

Our own wording is fixed in code and its identifiers are shown as code. Text from upstream
(a series name, a source's message) is never stored in an event: the dispatcher asks a resolver for
it by identifier at send time and it goes through `sanitize_upstream_text(..., MARKDOWN)`.

That function escapes Markdown syntax; it cannot stop the recipient from turning *plain* text into
something live. Two cases are handled here, by marking them explicitly in a code span (shown to the
reader, never linked, never resolved) rather than by hiding them with an invisible character:

* a bare URL (`https://...`, `www....`, `host.tld/path`), auto-linked by chat clients;
* a mention (`@everyone`, `@here`, `@name`, `<@id>`, `<@&id>`, `<#id>`, `<!channel>`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from shelfhold.infrastructure.content_trust import Destination, sanitize_upstream_text

MAX_UPSTREAM_CHARS: Final = 300
_ESC: Final = r"(?:\\.|[^\s\\])"  # a character, or a backslash-escaped one
_URL_LIKE: Final = rf"""
    (?:
        (?:[a-z][a-z0-9+.-]*)://{_ESC}+            # scheme://anything
      | www\.{_ESC}+                               # www.host
      | (?:[a-z0-9-]+\.)+[a-z]{{2,24}}(?::\d+)?/{_ESC}*   # host.tld/path
      | mailto:{_ESC}+                               # the one scheme without // that clients link
    )
    """
_MENTION: Final = r"""
    (?:\\?<\\?[@!\#&][^>\s]*\\?>)     # <@id> <@&id> <#id> <!here> (possibly backslash-escaped)
  | @[\w.-]+                # @everyone @here @name
    """
_LIVE: Final = re.compile(f"{_URL_LIKE}|{_MENTION}", re.IGNORECASE | re.VERBOSE)
_UNESCAPE: Final = re.compile(r"\\(.)")


@dataclass(frozen=True)
class Message:
    title: str
    body: str


def _code_span(escaped: str) -> str:
    """The text as it was before escaping, in a code span that cannot be closed from inside."""
    raw = _UNESCAPE.sub(r"\1", escaped).replace("`", "'")
    return f"`{raw}`"


def neutralize_markdown(text: str, *, max_length: int = MAX_UPSTREAM_CHARS) -> str:
    """Upstream text, safe to put in a Markdown notification."""
    escaped = sanitize_upstream_text(text, destination=Destination.MARKDOWN, max_length=max_length)
    # one pass: a second one would find `@` inside the code span of a URL and break out of it
    return _LIVE.sub(lambda m: _code_span(m.group(0)), escaped)


_HEADLINES: Final = {
    "backup.failed": "A backup failed",
    "backup.stale": "No recent backup",
    "disk.low": "Disk space is low",
    "job.dead_lettered": "Jobs gave up",
    "target.unreachable": "A destination is unreachable",
    "target.version_unsupported": "A destination has an unsupported version",
    "reconcile.drift": "The library drifted from its destination",
    "secret.undecryptable": "A secret cannot be decrypted",
    "mount.sentinel_missing": "A mount looks empty or missing",
    "source.broken": "A source is broken",
}


def render_notification(
    event_type: str,
    payload: Mapping[str, object],
    *,
    upstream: Mapping[str, str] | None = None,
    count: int = 1,
) -> Message:
    """Title and Markdown body. `count` > 1 is an aggregate (one line for several events)."""
    headline = _HEADLINES.get(event_type, "Operating event")
    lines = [f"**{headline}** (`{event_type}`)"]
    if count > 1:
        lines.append(f"Occurrences: {count}")
    for key in sorted(payload):
        value = payload[key]
        if isinstance(value, bool | int | float) or value is None:
            lines.append(f"- {key}: {value}")
        else:
            lines.append(f"- {key}: `{str(value).replace('`', '')[:80]}`")
    for label in sorted(upstream or {}):
        lines.append(f"- {label}: {neutralize_markdown((upstream or {})[label])}")
    return Message(title=f"Shelfhold - {event_type}", body="\n".join(lines))
