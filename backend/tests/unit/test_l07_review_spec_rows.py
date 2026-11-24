"""Relecture adverse de M6 L07 (fusion de main): the requirement table keeps one row per line.

The conflict on EF-USR-024 in `01-cahier-des-charges.md` was resolved by dropping the newline
before EF-USR-025: the passkeys row now hangs off the end of EF-USR-024 (`P3, P4 || EF-USR-025`).
The traceability tool reads one requirement per line, so EF-USR-025 vanishes from the registry
and EF-USR-024 takes the milestones of the passkeys row.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
SPEC = ROOT / "docs" / "spec" / "01-cahier-des-charges.md"

pytestmark = pytest.mark.req("EF-USR-024", "EF-USR-025")


def _tool() -> ModuleType:
    tool = ROOT / "tools" / "traceability.py"
    spec = importlib.util.spec_from_file_location("traceability_l07", tool)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["traceability_l07"] = module
    spec.loader.exec_module(module)
    return module


def test_passkeys_row_is_still_its_own_requirement() -> None:
    text = SPEC.read_text(encoding="utf-8")
    assert "|| EF-USR-025" not in text, "the EF-USR-025 row is glued to the end of EF-USR-024"
    reqs = _tool().parse_requirements(text)
    assert "EF-USR-025" in reqs
    assert "M6" in reqs["EF-USR-024"]
