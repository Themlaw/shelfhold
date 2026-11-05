"""Socket paths longer than `sun_path` (108 bytes), without `chdir` (the process-wide cwd)."""

import os
from pathlib import Path

SUN_PATH_SAFE = 100  # a little under the 108 bytes of the kernel structure


def via_directory(directory_fd: int, name: str) -> str:
    """The name of a file inside an open directory, as a short path (Linux `/proc/self/fd`)."""
    return f"/proc/self/fd/{directory_fd}/{name}"


def fits(path: Path) -> bool:
    return len(os.fsencode(path)) <= SUN_PATH_SAFE
