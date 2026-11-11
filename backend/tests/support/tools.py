"""External programs a test needs: skipped on a machine without them, never in CI."""

import os
import shutil

import pytest


def require_tool(name: str) -> str:
    """The path of `name`. Missing: skip with a clear reason, except under CI (`CI=true`),
    where the tool is installed and its absence is a failure, not a silent skip."""
    found = shutil.which(name)
    if found is not None:
        return found
    if os.environ.get("CI", "").lower() == "true":
        pytest.fail(f"`{name}` is required in CI and is not installed")
    pytest.skip(f"`{name}` is not installed on this machine (CI has it and runs this test)")
