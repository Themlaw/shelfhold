"""EF-BIB-054 (10), M1b L26: `shelfhold tombstone rekey` through the host CLI, over a real
instance (real purge, real roots, real keyring)."""

import json
from pathlib import Path

import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.secrets.diagnostics import store_for
from shelfhold.library.tombstones.journal import parse_journal
from tests.auth_env import run_cli
from tests.l50_env import seeded
from tests.l53_env import status_json

pytestmark = pytest.mark.req("EF-BIB-054")


def _journals(tmp_path: Path) -> list[Path]:
    return sorted(
        p for p in tmp_path.rglob("tombstones") if p.parent.name == ".shelfhold" and p.is_file()
    )


def test_tombstone_rekey_from_the_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series("Purged")
        unit = env.add_chapter(series, "1")
        assert (
            run_cli(monkeypatch, settings, "purge", "--now", "--unit", unit, "--confirm", unit) == 0
        )
        store = store_for(settings)
        leaked = store.load().active.kid
        journals = _journals(tmp_path)
        assert journals, "a purge writes the journal of its roots"
        assert {ln.kid for p in journals for ln in parse_journal(p.read_bytes()).lines} == {leaked}

        # the active key cannot be the compromised one: rotate first
        assert run_cli(monkeypatch, settings, "tombstone", "rekey", "--kid", leaked) != 0
        assert store.load().find(leaked).state == "active"  # type: ignore[union-attr]
        assert run_cli(monkeypatch, settings, "secrets", "rotate") == 0
        active = store.load().active.kid
        assert active != leaked

        capsys.readouterr()
        assert run_cli(monkeypatch, settings, "tombstone", "rekey", "--kid", leaked) == 0

        assert store.load().find(leaked).state == "compromised"  # type: ignore[union-attr]
        for path in journals:
            assert {ln.kid for ln in parse_journal(path.read_bytes()).lines} == {active}
        report = status_json(monkeypatch, settings, capsys)
        codes = {w["code"] for w in report["warnings"]}
        assert "keyring.key_compromised" in codes, json.dumps(sorted(codes))
        assert run_cli(monkeypatch, settings, "tombstone", "rekey", "--kid", "k-unknown") != 0
