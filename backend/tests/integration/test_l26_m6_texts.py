"""M6 L26: the texts of M6 — every error code has its cause and action in both languages, and
the admin and family pages of M6 exist, are indexed, link to pages that exist and keep the
vocabulary."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from shelfhold.i18n import load_catalog
from shelfhold.interfaces.http_api.catalog import all_error_codes

ROOT = Path(__file__).resolve().parents[3]
ADMIN = ROOT / "docs" / "admin"
FAMILY = ROOT / "docs" / "famille"

NEW_PAGES = [
    ADMIN / "index.md",
    ADMIN / "trackers.md",
    ADMIN / "kobo.md",
    ADMIN / "caches.md",
    FAMILY / "hors-ligne.md",
]
TOUCHED = [
    ADMIN / "plugins-isoles.md",
    ADMIN / "index.md",
    ADMIN / "trackers.md",
    ADMIN / "kobo.md",
    ADMIN / "caches.md",
    ADMIN / "webhooks.md",
    FAMILY / "hors-ligne.md",
]


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8")


@pytest.mark.req("ENF-I18N-001", "EF-UX-003")
def test_every_error_code_has_cause_and_action_fr_en() -> None:
    codes = all_error_codes()
    assert codes
    for catalog, language in ((load_catalog("fr"), "fr"), (load_catalog("en"), "en")):
        for spec in codes:
            for key in (spec.title_key, spec.cause_key, spec.action_key):
                assert str(catalog.get(key, "")).strip(), f"{key} ({language})"


@pytest.mark.req("ENF-DOC-001")
def test_the_m6_pages_exist() -> None:
    for p in TOUCHED:
        assert p.is_file(), p


@pytest.mark.req("ENF-DOC-001")
def test_the_m6_pages_are_indexed_in_their_readme() -> None:
    admin = _text(ADMIN / "README.md")
    for name in ("index.md", "trackers.md", "kobo.md", "caches.md"):
        assert name in admin, name
    assert "hors-ligne.md" in _text(FAMILY / "README.md")


@pytest.mark.req("ENF-DOC-001")
def test_the_m6_pages_link_to_pages_that_exist() -> None:
    link = re.compile(r"\]\(([^)#?]+?)(?:#[^)]*)?\)")
    for page in TOUCHED:
        for target in link.findall(_text(page)):
            if target.startswith(("http", "/")):
                continue
            assert (page.parent / target).is_file(), f"{page.name}: {target}"


@pytest.mark.req("ENF-LEG-006", "ENF-LEG-008")
def test_the_m6_pages_keep_the_vocabulary() -> None:
    spec = importlib.util.spec_from_file_location("vl_l26", ROOT / "tools" / "vocabulary_lint.py")
    lint = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = lint
    spec.loader.exec_module(lint)
    assert lint.scan(ROOT) == []


@pytest.mark.req("ENF-DOC-001")
def test_the_m6_pages_use_the_project_terms() -> None:
    assert "minisign" in _text(ADMIN / "index.md")
    assert "tracker" in _text(ADMIN / "trackers.md").lower()
    assert "pas livré" in _text(ADMIN / "kobo.md")
    assert "quota" in _text(ADMIN / "caches.md").lower()
    assert "hors ligne" in _text(FAMILY / "hors-ligne.md").lower()
