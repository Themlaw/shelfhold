"""Golden diagnostic bundle (M2 L30): a synthetic instance planted with trap values gives a
bundle where none of them survives, in clear, percent-encoded, in base64 or inside a compressed
member; and the shape of what the bundle holds is pinned by a syrupy snapshot."""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import pytest
from syrupy.assertion import SnapshotAssertion

from shelfhold.config import Settings
from tests.auth_env import run_cli
from tests.diagnostic_env import leaks, log_file, trap_instance

_VOLATILE = (
    (re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$"), "<ulid>"),
    (re.compile(r"^sha256:[0-9a-f]{64}$"), "<sha256>"),
    (re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d)?$"), "<timestamp>"),
    (re.compile(r"^[0-9a-f]{12}$"), "<revision>"),
)


_RELEASED = {"schema_revision", "schema_head", "shelfhold_version"}  # move with each release


def _shape(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: f"<{key}>" if key in _RELEASED else _shape(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, list):
        return [_shape(item) for item in value]
    if isinstance(value, str):
        for pattern, placeholder in _VOLATILE:
            if pattern.match(value):
                return placeholder
    return value


@pytest.mark.req("ENF-LEG-009", "EF-BIB-041")
def test_diagnostic_bundle_golden_no_trap_value_in_any_form(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path, snapshot: SnapshotAssertion
) -> None:
    traps = trap_instance(monkeypatch, settings, tmp_path)
    out = tmp_path / "bundle.zip"
    logs = log_file(tmp_path / "shelfhold.log", traps)
    assert run_cli(monkeypatch, settings, "diagnostic", "bundle", "--out", str(out),
                   "--log-file", str(logs)) == 0  # fmt: skip
    assert leaks(out.read_bytes(), traps) == []
    with zipfile.ZipFile(io.BytesIO(out.read_bytes())) as archive:
        members = {name: archive.read(name).decode("utf-8") for name in archive.namelist()}
    assert members["schema.sql"].startswith("-- shelfhold schema")
    shaped = {
        name: (
            [_shape(json.loads(line)) for line in text.splitlines()]
            if name.endswith(".jsonl")
            else _shape(json.loads(text))
        )
        for name, text in sorted(members.items())
        if name.endswith((".json", ".jsonl")) and name != "status.json"
    }
    assert shaped == snapshot
