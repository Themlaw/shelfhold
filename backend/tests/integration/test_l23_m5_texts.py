"""M5 L23: the texts of M5 — every new code in both languages, and the pages that explain them.

The error catalog is the contract of the problem cards (EF-UX-003): every code the code declares
carries a `title`, a `cause` and an `action` in French and in English, collected from the
declaration itself, never from a list copied by hand. The M5 features (the integrated reader, the
device registry, OIDC, `/lite`, the `/content` route, the offline shell and the e-readers) also get
their pages: the administrator's and the family's, each saying only what the branch really does — a
surface that does not exist yet is written « pas encore disponible », never invented.

The e-reader pages carry the two promises of ENF-COMP-002 that no code can hold: the certificate
stores of old readers, and the residual risk R66 (a file that left through OPDS or `/lite` stays on
the reader and cannot be recalled).
"""

from __future__ import annotations

import importlib.util
import re
import sys
import tomllib
from pathlib import Path

import pytest

from shelfhold.i18n import load_catalog
from shelfhold.interfaces.http_api.catalog import all_error_codes

ROOT = Path(__file__).resolve().parents[3]
ADMIN = ROOT / "docs" / "admin"
FAMILY = ROOT / "docs" / "famille"
TEMPLATES = ROOT / ".github" / "ISSUE_TEMPLATE"
NEW_PAGES = [
    ADMIN / "lite.md",
    ADMIN / "contenu.md",
    ADMIN / "liseuses-tls.md",
    FAMILY / "lecteur.md",
]
TOUCHED_PAGES = [
    *NEW_PAGES,
    ADMIN / "clients.md",
    ADMIN / "oidc.md",
    ADMIN / "jetons-appareil.md",
]

# The codes the M5 lots added: the reader and its positions (L06, L07, L13), the device registry
# (L02), OIDC (L03, L04), the renditions (L20a, L20b), the OPDS feed (L18, L19), the saved
# selections (L21) and kosync (L09). A code added by a later M5 lot belongs here too.
M5_CODES = (
    "no_readable_copy",
    "excerpt_unavailable",
    "content_changed",
    "full_read_not_allowed",
    "excerpt_writes_no_position",
    "invalid_locator",
    "client_ts_in_future",
    "client_ts_out_of_range",
    "conflict_stale",
    "settings_limit_reached",
    "progress_busy",
    "device_tls_required",
    "device_token_not_found",
    "device_rotation_conflict",
    "oidc_unavailable",
    "oidc_conflict",
    "oidc_child_profile",
    "oidc_admin_target",
    "oidc_link_expired",
    "oidc_not_linked",
    "rendition_busy",
    "rendition.format_unsupported",
    "rendition.remote_refused_watermarked",
    "not_acceptable",
    "bundle_busy",
    "bundle_gone",
    "too_many_streams",
    "selection.query_refused",
    "selection.full",
    "selection.too_slow",
    "kosync_registration_disabled",
)


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


@pytest.mark.req("ENF-I18N-001", "EF-UX-003")
def test_the_m5_codes_name_their_own_cause_and_action() -> None:
    """Each M5 code says what happened and what to do, in both languages.

    A code that falls back on the generic cause of its audience (`errors.cause_default.*`) tells
    the reader nothing: the M5 codes carry their own, and it is not a copy of the title.
    """
    declared = {spec.code for spec in all_error_codes()}
    missing = [code for code in M5_CODES if code not in declared]
    assert not missing, f"codes of M5 no longer declared: {missing}"
    for catalog, language in ((load_catalog("fr"), "fr"), (load_catalog("en"), "en")):
        for code in M5_CODES:
            title = str(catalog.get(f"errors.{code}.title", "")).strip()
            cause = str(catalog.get(f"errors.{code}.cause", "")).strip()
            action = str(catalog.get(f"errors.{code}.action", "")).strip()
            assert title, f"errors.{code}.title ({language})"
            assert cause, f"errors.{code}.cause ({language}): the generic cause is not enough"
            assert action, f"errors.{code}.action ({language})"
            assert cause != title, f"errors.{code}.cause ({language}) repeats the title"


@pytest.mark.req("ENF-LEG-009", "EF-SRC-012")
def test_the_m5_texts_name_no_site_and_no_circumvention_tool() -> None:
    """The M5 texts say « fichier protégé », never the acronym of a protection."""
    banned = ("drm",)  # the generic acronym only: a site or a tool is never named, even here
    for path in (ROOT / "backend" / "src" / "shelfhold" / "locales" / "fr.toml",
                 ROOT / "backend" / "src" / "shelfhold" / "locales" / "en.toml"):  # fmt: skip
        table = tomllib.loads(_text(path))["errors"]
        for code in M5_CODES:
            entry = table[code]
            words = " ".join(str(entry.get(part, "")) for part in ("title", "cause", "action"))
            for brand in banned:
                assert brand not in words.lower(), (path.name, code, brand)


# --------------------------------------------------------------------------- `/lite`


@pytest.mark.req("EF-LEC-010", "ENF-COMP-002", "ENF-SEC-010")
def test_lite_page_matches_the_code() -> None:
    """The `/lite` page names the routes that exist, the one credential, and what it never does."""
    text = _text(ADMIN / "lite.md")
    assert "/lite" in text
    for route in ("/lite/sign-in", "/lite/sign-out", "/lite/shelves", "/lite/series",
                  "/lite/works", "/lite/recent", "/lite/search", "/lite/covers/",
                  "/lite/editions/", "/lite/kepub/"):  # fmt: skip
        assert route in text, route
    # server-rendered HTML, no script at all: the browser of a Kobo runs no ES6
    assert "sans JavaScript" in text or "aucun `<script>`" in text
    assert "WebKit 538.1" in text and "5.12.4" in text
    # the one credential is the secret of a device token, and the cookie holds no secret
    assert "jeton d'appareil" in text and "secret" in text
    assert "__Secure-lite" in text and "Path" in text and "HttpOnly" in text
    assert "SameSite=Strict" in text
    # signing out is not a revocation: the answer to a lost reader is to remove the device
    assert "n'est pas une révocation" in text
    assert "Retirer" in text or "retirer l'appareil" in text
    # HTTPS is required, and the one way out is named with its warning
    assert "device_tls_required" in text
    assert "SHELFHOLD_DEVICE_ALLOW_PLAIN_HTTP" in text
    assert "SHELFHOLD_HTTP_TRUSTED_PROXIES" in text
    # what a reader downloads stays on the reader (R66)
    assert "R66" in text
    # the neighbouring pages
    assert "[jetons-appareil.md](jetons-appareil.md)" in text
    assert "[liseuses-tls.md](liseuses-tls.md)" in text
    assert "[clients.md](clients.md)" in text


@pytest.mark.req("EF-LEC-010", "ENF-COMP-002")
def test_lite_page_says_what_does_not_exist_yet() -> None:
    """A surface that is not delivered is written as such, never invented."""
    text = _text(ADMIN / "lite.md")
    assert "pas encore disponible" in text
    # the offline copy of a book is L16b: made in the web app, never in `/lite`; the page says so
    assert "copies-hors-ligne.md" in text


# --------------------------------------------------------------------------- `/content`


@pytest.mark.req("EF-LEC-005", "ENF-SEC-004")
def test_content_page_matches_the_code() -> None:
    """The `/content` page names the route, the ticket, the separate origin and the refusals."""
    text = _text(ADMIN / "contenu.md")
    assert "GET /content/{ticket}/{path}" in text
    assert "SHELFHOLD_CONTENT_ORIGIN" in text
    assert "origine séparée" in text
    # the ticket: ten minutes, bound to the profile, the carrier, the scope and the session
    assert "10 minutes" in text or "600" in text
    assert "profil" in text and "session" in text
    # the headers of every answer
    assert "sandbox" in text and "attachment" in text
    assert "ETag" in text
    # one answer for everything that is refused
    assert "404" in text
    assert "content_changed" in text
    assert "credential_not_allowed" in text
    # the bytes are the neutralised derivative, never the original of an untrusted source
    assert "neutralis" in text
    assert "[neutraliseur.md](neutraliseur.md)" in text


# --------------------------------------------------------------------------- e-readers and TLS


@pytest.mark.req("ENF-COMP-002")
def test_ereaders_tls_page_matches_the_code() -> None:
    """The e-reader page says which readers cannot reach a modern TLS, and what to do instead."""
    text = _text(ADMIN / "liseuses-tls.md")
    # the readers whose certificate store is frozen
    assert "magasin de certificats" in text.lower()
    assert "Kobo" in text and "Kindle" in text and "KOReader" in text
    assert "WebKit 538.1" in text and "5.12.4" in text
    # the refusal of the code, and the one way out with its warning
    assert "device_tls_required" in text
    assert "SHELFHOLD_DEVICE_ALLOW_PLAIN_HTTP" in text
    assert "réseau local de confiance" in text
    assert "SHELFHOLD_HTTP_TRUSTED_PROXIES" in text
    # a reader that cannot join at all is sent to KOReader
    assert "KOReader" in text and "conseillé" in text
    # the residual risk, explained: a file that left cannot be recalled
    assert "R66" in text
    assert "ne peut pas" in text and "reprendre" in text
    # hosting on a subdomain that shares its parent domain with an untrusted service
    assert "sous-domaine" in text
    # the device report is the only way into the matrix
    assert "rapport d'appareil" in text
    assert "[clients.md](clients.md)" in text


# --------------------------------------------------------------------------- the device registry


@pytest.mark.req("EF-USR-008", "ENF-SEC-013")
def test_devices_page_matches_the_code() -> None:
    """The device page names the registry, the rotation and the removal, and their codes."""
    text = _text(ADMIN / "jetons-appareil.md")
    assert "GET /api/v1/devices" in text
    assert "POST /api/v1/devices/{id}/rotate" in text
    assert "DELETE /api/v1/devices/{id}" in text
    assert "device_token.rotate" in text and "device.remove" in text
    assert "device_rotation_conflict" in text
    assert "device_token_not_found" in text
    assert "device_tls_required" in text
    # the registry is M5, and the page says what a reader of another profile gets
    assert "registre d'appareils" in text.lower()
    assert "appareil inconnu" in text


# --------------------------------------------------------------------------- OIDC


@pytest.mark.req("EF-USR-004", "EF-USR-024")
def test_oidc_page_gives_the_holder_the_compose_steps() -> None:
    """The OIDC page carries the compose steps, the callback address and the freshness rule."""
    text = _text(ADMIN / "oidc.md")
    assert "compose" in text.lower()
    assert "SHELFHOLD_OIDC_ISSUER" in text and "SHELFHOLD_OIDC_CLIENT_ID" in text
    assert "SHELFHOLD_OIDC_CLIENT_SECRET" in text and "SHELFHOLD_OIDC_MAX_AGE_S" in text
    assert "/api/v1/auth/oidc/callback" in text
    assert "SHELFHOLD_PUBLIC_URL" in text
    # the codes of the feature, each with its own cause and action
    for code in ("oidc_unavailable", "oidc_conflict", "oidc_child_profile",
                 "oidc_admin_target", "oidc_link_expired", "oidc_not_linked"):  # fmt: skip
        assert code in text, code
    # the freshness of a session opened by the provider
    assert "prompt=login" in text and "auth_time" in text
    assert "reauth_required" in text


# --------------------------------------------------------------------------- the family page


@pytest.mark.req("EF-LEC-001", "EF-LEC-008", "ENF-DOC-001")
def test_family_reader_page() -> None:
    """The family page about the reader speaks to the reader and keeps the household wording."""
    text = _text(FAMILY / "lecteur.md")
    assert "Lire" in text and "Lire un extrait" in text
    assert "Reprendre la lecture" in text or "reprendre la lecture" in text
    assert "votre progression" in text.lower() or "votre place" in text.lower()
    # the keyboard, and the settings with the scope asked
    assert "Échap" in text and "flèches" in text.lower()
    assert "Pour cet appareil" in text and "Partout" in text
    # an excerpt is a fifth of the book at most, and writes no position
    assert "extrait" in text and "cinquième" in text
    # the household wording: never "partager", never a personal path or an address
    assert not re.search(r"\bpartag\w*", text, re.I)
    assert not [m for m in re.findall(r"/home/|/Users/|\b(?:\d{1,3}\.){3}\d{1,3}\b", text)]
    # a problem goes to the administrator
    assert "l'administrateur" in text
    assert "[../admin/clients.md](../admin/clients.md)" in text


# --------------------------------------------------------------------------- the clients matrix


@pytest.mark.req("ENF-COMP-002")
def test_clients_matrix_covers_m5() -> None:
    """The matrix no longer promises `/lite` for later: it says what M5 delivered, and R66."""
    text = _text(ADMIN / "clients.md")
    assert "/lite" in text
    assert "à venir" not in text  # M5 delivered it
    assert "KEPUB" in text  # the rendition offered to a Kobo
    assert "OPDS 2.0" in text
    assert "PWA" in text and "hors ligne" in text.lower()
    assert "R66" in text
    assert "rapport d'appareil" in text
    # the offline column still answers every row with a fact or "non vérifié"
    header = next(line for line in text.splitlines() if line.startswith("| Client"))
    assert "Hors ligne" in header


# --------------------------------------------------------------------------- the device report


@pytest.mark.req("ENF-COMP-002")
def test_device_report_template_asks_for_what_the_matrix_needs() -> None:
    """The device report template asks for the TLS, `/lite`, the KEPUB and the offline column."""
    (template,) = TEMPLATES.glob("rapport-*.yml")
    body = _text(template)
    assert "User-Agent" in body
    assert "TLS" in body or "certificat" in body
    assert "/lite" in body
    assert "KEPUB" in body
    assert "hors ligne" in body.lower() or "Hors ligne" in body
    # the reminder of the wording: no site name, no key
    assert "pas de nom de site" in body


# --------------------------------------------------------------------------- index, links, lint


@pytest.mark.req("ENF-DOC-001")
def test_new_pages_are_indexed() -> None:
    admin = _text(ADMIN / "README.md")
    for page in ("lite.md", "contenu.md", "liseuses-tls.md"):
        assert page in admin, page
    assert "lecteur.md" in _text(FAMILY / "README.md")


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
        "vocabulary_lint_l23", ROOT / "tools" / "vocabulary_lint.py"
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
