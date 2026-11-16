"""The local source path of Suwayomi, read from its settings (EF-EXP-010, M4 L07).

The adapter asks `query { settings { localSourcePath } }` (one read, the root field it already
uses); this module reads the answer. A server that does not expose the setting, or answers with
anything but an absolute path (relative, empty, a control character, a list, a very long string),
gives `None`: the guard then has nothing to compare and says so, it never invents a path.
"""

from typing import Any, Final

__all__ = ["LOCAL_SOURCE_QUERY", "NO_LOCAL_SOURCE", "parse_local_source_path", "read_local_source"]

LOCAL_SOURCE_QUERY: Final = "query { settings { localSourcePath } }"
_MAX_PATH: Final = 4096
NO_LOCAL_SOURCE: Final = ""  # the server answered, and has no folder of its own for it


def parse_local_source_path(settings: Any) -> str | None:
    """The path the server reports, as it wrote it, or None."""
    path = settings.get("localSourcePath") if isinstance(settings, dict) else None
    if not isinstance(path, str) or not path.startswith("/") or len(path) > _MAX_PATH:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        return None
    return path


def read_local_source(settings: Any) -> str | None:
    """What the answer says: an absolute path, `NO_LOCAL_SOURCE` when the field is there and empty
    (the server uses its own default), or None when it cannot be read (an absent field, a relative
    path, a control character, a list, a very long string). Unreadable is not "no local source":
    the guard keeps what it knew closed."""
    if isinstance(settings, dict) and settings.get("localSourcePath") == NO_LOCAL_SOURCE:
        return NO_LOCAL_SOURCE
    return parse_local_source_path(settings)
