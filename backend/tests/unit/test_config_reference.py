"""EF-INT-024: `docs/reference/config.md` is generated from the settings, and kept up to date."""

import re
from pathlib import Path

import pytest

from shelfhold.config import ENV_PREFIX, Settings
from shelfhold.docgen.config_reference import render
from shelfhold.headless.keys import KEYS

REFERENCE = Path(__file__).resolve().parents[3] / "docs" / "reference" / "config.md"
REGENERATE = "python -m shelfhold.docgen.config_reference > docs/reference/config.md"


@pytest.mark.req("EF-INT-024")
def test_config_reference_is_up_to_date() -> None:
    assert REFERENCE.read_text(encoding="utf-8") == render(), f"regenerate it with: {REGENERATE}"


@pytest.mark.req("EF-INT-024")
def test_config_reference_names_every_setting_and_every_key() -> None:
    text = REFERENCE.read_text(encoding="utf-8")
    for name in Settings.model_fields:
        assert f"`{ENV_PREFIX}{name.upper()}`" in text, name
    for key in KEYS:
        assert f"`{key}`" in text, key
    assert "`path_map.<cible>`" in text


@pytest.mark.req("EF-INT-024")
def test_config_reference_follows_a_new_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The file is a function of the definition: a key added there appears here."""
    from dataclasses import replace

    changed = dict(KEYS)
    changed["demo.key"] = replace(KEYS["public_url"], name="demo.key")
    monkeypatch.setattr("shelfhold.docgen.config_reference.KEYS", changed)
    assert "`demo.key`" in render()
    assert "`demo.key`" not in REFERENCE.read_text(encoding="utf-8")


@pytest.mark.req("EF-INT-024")
def test_config_reference_is_deterministic_and_machine_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    assert render() == REFERENCE.read_text(encoding="utf-8")
    assert not re.search(r"/home/|/Users/", render())
