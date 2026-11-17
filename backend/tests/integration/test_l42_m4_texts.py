"""M4 L42: the texts of M4 — error codes in both languages, and the pages that explain them.

The error catalog is the contract of the problem cards (EF-UX-003): every code the code declares
carries a `title`, a `cause` and an `action` in French and in English, collected from the
declaration itself, never from a list copied by hand. The M4 features (destinations, progress
sync, notification destinations, metrics, the Mihon import) also get their pages: the
administrator's, the family's and the plugin author's, each saying only what the branch really
does — a surface that does not exist yet is written « pas encore disponible », never invented.
"""

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
PLUGINS = ROOT / "docs" / "plugins"
NEW_PAGES = [
    ADMIN / "destinations.md",
    ADMIN / "sync.md",
    ADMIN / "webhooks.md",
    ADMIN / "metrics.md",
    FAMILY / "progression.md",
]
TOUCHED_PAGES = [*NEW_PAGES, ADMIN / "import-mihon.md", PLUGINS / "destination.md"]


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- the error codes


@pytest.mark.req("ENF-I18N-001", "EF-UX-003")
def test_every_error_code_has_cause_and_action_fr_en() -> None:
    """Every code the code declares carries a title, a cause and an action, in both languages.

    The codes are collected from the declaration (`all_error_codes`), not from a list: a code
    added without its texts fails here, whatever its context.
    """
    codes = all_error_codes()
    assert codes, "the catalog is empty: the collector is broken, not the catalog"
    for catalog, language in ((load_catalog("fr"), "fr"), (load_catalog("en"), "en")):
        for spec in codes:
            for key in (spec.title_key, spec.cause_key, spec.action_key):
                assert str(catalog.get(key, "")).strip(), f"{key} ({language})"


# --------------------------------------------------------------------------- the admin pages


@pytest.mark.req("ENF-DOC-001")
def test_destinations_page_matches_the_code() -> None:
    """The destinations page names the kinds that exist, the doors that create them, and the
    ones that do not exist yet."""
    text = _text(ADMIN / "destinations.md")
    for word in ("Kavita", "dossier natif", "Komga", "CWA"):
        assert word in text, word
    # the doors that exist, one line each, as the code declares them
    assert "target add kavita" in text and "--accept-admin-risk" in text
    assert "/api/v1/targets/native-folder" in text
    assert "POST /api/v1/shelves/{id}/destinations" in text  # the fan-out (L06a)
    assert "/api/v1/profiles/{id}/destination-accounts" in text  # the member keys (L12)
    assert "target remove" in text and "--keep-files" in text and "--delete-files" in text
    # what does not exist yet is said as such, never invented
    assert "pas encore disponible" in text
    # the holder's steps and the detail pages of each destination
    assert "compose" in text.lower()
    assert "../destinations/cwa-drop-box.md" in text and "../destinations/komga-routes.md" in text
    assert "[sync.md](sync.md)" in text and "[famille.md](famille.md)" in text


@pytest.mark.req("ENF-DOC-001")
def test_sync_page_matches_the_code() -> None:
    """The sync page explains the job that runs, what it never does, and where its state is
    read; enabling it per destination has no interface yet."""
    text = _text(ADMIN / "sync.md")
    assert "progress.sync" in text  # the job, every 10 minutes
    assert "10 min" in text
    assert "GET /api/v1/sync-status" in text
    assert "renommage" in text  # the progress kept through a rename (L16)
    assert "kosync" in text
    assert "pas encore disponible" in text  # no interface turns sync_progress on
    # the contract and the neighbouring pages
    assert "../reference/progress-sync.md" in text
    assert "../reference/read-state-push.md" in text and "[kosync.md](kosync.md)" in text


@pytest.mark.req("ENF-DOC-001")
def test_webhooks_page_matches_the_code() -> None:
    """The webhooks page says what leaves the instance today (the notification channels, the
    per-profile destination) and that the outbox and its signed webhooks are not there yet."""
    text = _text(ADMIN / "webhooks.md")
    assert "/api/v1/me/notification-destination" in text  # M4 L39
    assert "[notifications.md](notifications.md)" in text  # the admin channels live there
    for scheme in ("ntfy", "gotify", "matrix", "discord", "slack", "tgram", "pover"):
        assert scheme in text, scheme  # the closed list of the allowed schemes
    assert "digest" in text and "immediate" in text  # notify_mode
    assert "HMAC" in text and "pas encore disponible" in text  # the outbox (EF-INT-005)
    assert "pas encore d'écran" in text  # the profile destination is API-only (L39)


@pytest.mark.req("ENF-DOC-001")
def test_metrics_page_matches_the_code() -> None:
    """The metrics page names the switch, the only credential that reads the route, and the
    families the scrape really renders."""
    text = _text(ADMIN / "metrics.md")
    assert "/metrics" in text
    assert "SHELFHOLD_METRICS_ENABLED" in text
    assert "metrics" in text and "admin keys create" in text  # the key of scope `metrics`
    assert "404" in text  # off: even the right key learns nothing
    for family in ("shelfhold_build_info", "shelfhold_jobs", "shelfhold_units", "shelfhold_works"):
        assert family in text, family
    assert "[historique-et-stockage.md](historique-et-stockage.md)" in text


@pytest.mark.req("ENF-DOC-001")
def test_import_mihon_page_stays_true_to_the_code() -> None:
    """The Mihon import page (written with L26) keeps its two doors and its refusal codes."""
    text = _text(ADMIN / "import-mihon.md")
    assert "import tachibk" in text and "POST /api/v1/imports/tachibk" in text
    for code in ("import.tachibk_invalid", "import.tachibk_schema"):
        assert code in text, code
    assert "--profile" in text  # the markers go to one profile only


# --------------------------------------------------------------------------- the family page


@pytest.mark.req("ENF-DOC-001")
def test_family_progression_page() -> None:
    """The family page about progress speaks to the reader, keeps the household wording and
    sends every problem to the administrator."""
    text = _text(FAMILY / "progression.md")
    assert "Kavita" in text
    assert "votre compte" in text  # one account per person, the progress is yours
    assert "l'administrateur" in text  # who fixes a wrong progress
    # the household wording: never "partager", never a personal path or an address
    assert not re.search(r"\bpartag\w*", text, re.I)
    assert not [m for m in re.findall(r"/home/|/Users/|\b(?:\d{1,3}\.){3}\d{1,3}\b", text)]


# --------------------------------------------------------------------------- the author page


@pytest.mark.req("ENF-DOC-001", "EF-EXP-058")
def test_destination_plugin_page_names_the_delivered_kit() -> None:
    """The destination page no longer says the kit is coming: M4 delivered it, and `push`
    passes it as soon as the destination declares `content_body`."""
    text = _text(PLUGINS / "destination.md")
    assert "destination-kit.md" in text and "assert_destination_conforms" in text
    assert "à venir" not in text  # the kit is there
    assert "reste refusé par le kit" not in text  # `push` is open behind the kit
    assert "content_body" in text
    # what stays unstable is still said to the author
    assert "ExportItem" in text


# --------------------------------------------------------------------------- index, links, lint


@pytest.mark.req("ENF-DOC-001")
def test_new_pages_are_indexed() -> None:
    admin = _text(ADMIN / "README.md")
    for page in ("destinations.md", "sync.md", "webhooks.md", "metrics.md"):
        assert page in admin, page
    assert "progression.md" in _text(FAMILY / "README.md")


@pytest.mark.req("ENF-DOC-001")
def test_no_relative_link_of_the_new_pages_is_broken() -> None:
    link = re.compile(r"\]\(([^)#?]+?)(?:#[^)]*)?\)")
    for page in TOUCHED_PAGES:
        for target in link.findall(_text(page)):
            assert target.startswith(("http", "/")) or (page.parent / target).is_file(), (
                f"{page.name}: {target}"
            )


@pytest.mark.req("ENF-LEG-006", "ENF-LEG-008")
def test_new_pages_pass_the_vocabulary_lint() -> None:
    """The vocabulary lint covers the new pages, and they obey it and the household wording."""
    spec = importlib.util.spec_from_file_location(
        "vocabulary_lint_l42", ROOT / "tools" / "vocabulary_lint.py"
    )
    assert spec is not None and spec.loader is not None
    lint = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = lint
    spec.loader.exec_module(lint)
    assert "docs/admin" in lint.DOCS and "docs/famille" in lint.DOCS
    assert lint.scan(ROOT) == []
    leaks = re.compile(r"/home/|/Users/|\.ts\.net|\b(?:\d{1,3}\.){3}\d{1,3}\b")
    for page in NEW_PAGES:
        text = _text(page)
        assert not [m for m in leaks.findall(text) if not m.startswith(("127.", "0."))], page.name
