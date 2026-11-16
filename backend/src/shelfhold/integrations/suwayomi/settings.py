"""The local source path of Suwayomi, read from its settings (EF-EXP-010, M4 L07).

The adapter asks `query { settings { localSourcePath } }` (one read, the root field it already
uses); this module reads the answer. A server that does not expose the setting, or answers with
anything but an absolute path (relative, empty, a control character, a list, a very long string),
gives `None`: the guard then has nothing to compare and says so, it never invents a path.
"""

from typing import Any, Final

__all__ = ["LOCAL_SOURCE_QUERY", "parse_local_source_path"]

LOCAL_SOURCE_QUERY: Final = "query { settings { localSourcePath } }"
_MAX_PATH: Final = 4096


def parse_local_source_path(settings: Any) -> str | None:
    """The path the server reports, as it wrote it, or None."""
    path = settings.get("localSourcePath") if isinstance(settings, dict) else None
    if not isinstance(path, str) or not path.startswith("/") or len(path) > _MAX_PATH:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        return None
    return path
