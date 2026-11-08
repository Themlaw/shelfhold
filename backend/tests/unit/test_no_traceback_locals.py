"""Review of #108: no local variable of a frame ever reaches a log or a traceback.

A frame of the Suwayomi wrapper holds a token or a password; `capture_locals` / `show_locals`
would print it, past every scrubbing rule that knows only the values it was told.
"""

import ast
from pathlib import Path

import pytest
import structlog

from shelfhold.infrastructure.logging import configure_logging

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"
FORBIDDEN = {"capture_locals", "show_locals", "include_local_variables", "locals_max_length"}


@pytest.mark.req("ENF-SEC-005", "ENF-OBS-001")
def test_no_source_file_turns_on_locals_in_tracebacks() -> None:
    found = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            names = []
            if isinstance(node, ast.keyword) and node.arg:
                names.append(node.arg)
            if isinstance(node, ast.Attribute):
                names.append(node.attr)
            if isinstance(node, ast.Name):
                names.append(node.id)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.extend(w for w in FORBIDDEN if w in node.value)
            found += [f"{path.relative_to(SRC)}:{getattr(node, 'lineno', 0)} {n}"
                      for n in names if n in FORBIDDEN]  # fmt: skip
    assert found == []


@pytest.mark.req("ENF-SEC-005", "ENF-OBS-001")
def test_the_configured_processors_do_not_show_locals() -> None:
    configure_logging(level="DEBUG", log_format="json")
    try:
        processors = list(structlog.get_config()["processors"])
        for processor in processors:
            assert not getattr(processor, "show_locals", False)
            assert not getattr(processor, "capture_locals", False)
            for inner in getattr(processor, "processors", []) or []:
                assert not getattr(inner, "show_locals", False)
    finally:
        structlog.reset_defaults()
