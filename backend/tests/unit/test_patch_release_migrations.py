"""ENF-OPS-002: no migration in a patch release (tools/check_patch_migrations.py)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

TOOL = Path(__file__).resolve().parents[3] / "tools" / "check_patch_migrations.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_patch_migrations", TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_patch_migrations"] = module
    spec.loader.exec_module(module)
    return module


tool = _load()

pytestmark = pytest.mark.req("ENF-OPS-002")

OLD = {"0001_initial.py", "0002_platform.py"}


def test_patch_release_with_a_new_migration_is_refused() -> None:
    assert tool.violation("v0.2.1", OLD, OLD | {"0003_catalog.py"}) == ["0003_catalog.py"]


def test_patch_release_without_new_migration_passes() -> None:
    assert tool.violation("v0.2.1", OLD, set(OLD)) == []


def test_minor_release_may_add_migrations() -> None:
    assert tool.violation("v0.3.0", OLD, OLD | {"0003_catalog.py"}) == []


def test_first_release_has_no_predecessor() -> None:
    assert tool.violation("v0.1.1", None, OLD) == []


def test_previous_tag_is_the_highest_lower_release() -> None:
    tags = ["v0.1.0", "v0.2.0", "v0.10.0", "v0.2.1", "latest"]
    assert tool.previous_tag("v0.2.1", tags) == "v0.2.0"
    assert tool.previous_tag("v0.10.1", tags) == "v0.10.0"
    assert tool.previous_tag("v0.1.0", tags) is None


def test_non_release_tag_is_rejected() -> None:
    with pytest.raises(ValueError):
        tool.parse("0.2.1")
