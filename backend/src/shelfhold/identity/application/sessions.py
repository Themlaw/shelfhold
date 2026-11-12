"""The list of sessions (EF-USR-011): a label for the device, the dates, never a token.

The `user_agent_label` of a session is a coarse label computed at sign-in ("Firefox · Linux"): the
raw header is never stored, so the list shows what lets a person recognise a device and nothing
that tells one browser build from another.
"""

import re
from dataclasses import dataclass
from datetime import datetime

MAX_LABEL = 80
_BROWSERS = (
    ("Edge", re.compile(r"\b(?:Edg|EdgA|EdgiOS)/")),
    ("Opera", re.compile(r"\b(?:OPR|Opera)/")),
    ("Firefox", re.compile(r"\b(?:Firefox|FxiOS)/")),
    ("Chrome", re.compile(r"\b(?:Chrome|CriOS)/")),
    ("Safari", re.compile(r"\bSafari/")),
    ("KOReader", re.compile(r"KOReader", re.IGNORECASE)),
)
_SYSTEMS = (
    ("iPhone", re.compile(r"\biPhone\b")),
    ("iPad", re.compile(r"\biPad\b")),
    ("Android", re.compile(r"\bAndroid\b")),
    ("Windows", re.compile(r"\bWindows\b")),
    ("macOS", re.compile(r"\bMac OS X\b|\bMacintosh\b")),
    ("Linux", re.compile(r"\bLinux\b|\bX11\b")),
)
_TOOL = re.compile(r"^([A-Za-z][A-Za-z0-9_-]{1,30})/[0-9][0-9.]*$")
_MAX_HEADER = 512  # what is looked at; a longer one is hostile or a mistake


def device_label(user_agent: str | None) -> str | None:
    """`Firefox · Linux`, `Safari · iPhone`, `curl`; None when nothing is recognised. Never a
    piece of the header, so no markup, no version and no unbounded text gets in."""
    if not user_agent:
        return None
    header = user_agent[:_MAX_HEADER]
    browser = next((name for name, pattern in _BROWSERS if pattern.search(header)), None)
    system = next((name for name, pattern in _SYSTEMS if pattern.search(header)), None)
    if browser and system:
        return f"{browser} · {system}"[:MAX_LABEL]
    if browser or system:
        return (browser or system or "")[:MAX_LABEL]
    tool = _TOOL.match(header.strip())
    return tool.group(1)[:MAX_LABEL] if tool else None


@dataclass(frozen=True)
class SessionView:
    """One live session of an account, as it is listed."""

    id: str
    user_id: str
    username: str
    device: str | None
    ip: str | None
    created_at: datetime
    auth_time: datetime
    last_seen_at: datetime
    remember: bool
