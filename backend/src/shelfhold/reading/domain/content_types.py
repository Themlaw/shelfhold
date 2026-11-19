"""What `/content` may call a file, and which names it may open (EF-LEC-005, ENF-SEC-004).

The type of an entry comes from its **extension, as the server reads it**, never from the manifest
of the book or from what the bytes look like. A closed list says what leaves as itself (rasters,
styles, fonts, XHTML and SVG for the reader); everything else is `application/octet-stream`, so
no answer of the route is a script, a module or a WebAssembly file, whatever the book calls it.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

OCTET: Final = "application/octet-stream"

ENTRY_TYPES: Final[Mapping[str, str]] = MappingProxyType(
    {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".avif": "image/avif",
        ".css": "text/css",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".otf": "font/otf",
        ".ttf": "font/ttf",
        ".xhtml": "application/xhtml+xml",
        ".html": "text/html",
        ".htm": "text/html",
        ".svg": "image/svg+xml",
    }
)


def media_type_of(entry: str) -> str:
    """The type of the entry named `entry`: its last extension in the list, else the neutral one."""
    name = entry.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    return ENTRY_TYPES.get(name[dot:].lower(), OCTET) if dot > 0 else OCTET


def safe_entry(entry: str) -> bool:
    """A plain relative name of an archive: no empty, `.` or `..` part, no backslash, no control
    character, no trailing slash. It is only ever compared with the names of the archive."""
    if not entry or len(entry) > 1024 or entry.startswith("/") or "\\" in entry:
        return False
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in entry):
        return False
    return all(part not in {"", ".", ".."} for part in entry.split("/"))
