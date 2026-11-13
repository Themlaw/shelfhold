"""M3 L51: the English catalogue is complete, and the language is chosen per request (ENF-I18N-001).

The English catalog carries exactly the keys of the French one, in the backend locales and in the
frontend UI catalogs; a missing key fails CI (`tools/i18n_parity.py`). The language of an API
answer is the profile preference of the signed-in person, else the `Accept-Language` of the
request (anonymous calls and the login page). The frontend starts from the browser languages
(the same `Accept-Language`), then follows the profile preference it reads from the server.
"""

import json
import re
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.i18n import load_catalog, negotiate, render, t_lang
from shelfhold.interfaces.http_api.catalog import error_code
from tests.auth_env import AuthEnv, auth_env, db_rows

ROOT = Path(__file__).resolve().parents[3]
UI_CATALOGS = ROOT / "frontend/src/lib/i18n"
LOCALES = Path(__file__).resolve().parents[1].parent / "src" / "shelfhold" / "locales"
PREFERENCES = "/api/v1/me/preferences"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> AuthEnv:
    with auth_env(monkeypatch, settings) as env:
        yield env


def _flatten(table: dict, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in table.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, dotted + "."))
        else:
            flat[dotted] = str(value)
    return flat


def _frontend(name: str) -> dict[str, str]:
    return json.loads((UI_CATALOGS / name).read_text(encoding="utf-8"))


@pytest.mark.req("ENF-I18N-001")
def test_i18n_catalogs_fr_en_same_keys() -> None:
    """The frontend catalogs hold the same keys; no English value is empty or left French."""
    fr, en = _frontend("fr.json"), _frontend("en.json")
    assert set(en) == set(fr), (
        f"missing in English: {sorted(set(fr) - set(en))}; "
        f"unknown in English: {sorted(set(en) - set(fr))}"
    )
    # a separator may be whitespace on purpose, but only when the French one is too
    empty = sorted(key for key, value in en.items() if not value.strip() and value != fr[key])
    assert empty == [], f"empty English values: {empty}"


@pytest.mark.req("ENF-I18N-001")
def test_backend_locales_fr_en_same_keys() -> None:
    """The backend locales hold the same keys; no English value is empty or left French."""
    fr = _flatten(tomllib.loads((LOCALES / "fr.toml").read_text(encoding="utf-8")))
    en = _flatten(tomllib.loads((LOCALES / "en.toml").read_text(encoding="utf-8")))
    assert set(en) == set(fr), (
        f"missing in English: {sorted(set(fr) - set(en))}; "
        f"unknown in English: {sorted(set(en) - set(fr))}"
    )
    # a separator may be whitespace on purpose, but only when the French one is too
    empty = sorted(key for key, value in en.items() if not value.strip() and value != fr[key])
    assert empty == [], f"empty English values: {empty}"


@pytest.mark.req("ENF-I18N-001")
def test_english_messages_render_like_the_french_ones() -> None:
    """Every English message is a readable ICU message: the placeholders of the French one are
    kept, and the renderer accepts it once they are filled."""
    fr = load_catalog("fr")
    en = load_catalog("en")
    for key, template in en.items():
        params: dict[str, object] = {}
        for name in _argument_names(fr[key]):
            if name in _plural_names(fr[key]):
                params[name] = 2
            elif re.search(rf"\{{{name}:[^}}]*[fdebg]\}}", fr[key]):
                params[name] = 1.5  # a number format: only a number can fill it
            else:
                params[name] = f"<{name}>"
        # raises on a malformed plural or a missing `other`
        render(template, language="en", **params)
        # the same arguments both ways: a dropped one (a truncated sentence) is as wrong as an extra
        assert _argument_names(template) == _argument_names(fr[key]), f"{key}: placeholders differ"
    assert any(en[key] != fr[key] for key in en), "the English catalog is the French one"


def _argument_names(template: str) -> list[str]:
    """Every name the message reads: a plural argument or a `str.format` placeholder."""
    return sorted(
        _plural_names(template) | set(re.findall(r"(?<!\{)\{(\w+)(?::[^{}]+)?\}", template))
    )


def _plural_names(template: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\s*,\s*plural", template))


@pytest.mark.req("ENF-I18N-001")
def test_ui_language_follows_profile_preference(api: AuthEnv) -> None:
    """The profile preference wins over the `Accept-Language` of the request."""
    assert api.login().status_code == 200
    body = api.client.get(PREFERENCES).json()
    assert body["language"] == "fr"  # the default, before any choice
    response = api.client.patch(
        PREFERENCES, json={"language": "en"}, headers={"X-CSRF-Token": api.csrf()}
    )
    assert response.status_code == 200, response.text
    assert response.json()["language"] == "en"
    assert db_rows(api.settings, "SELECT locale FROM user WHERE username = 'admin'") == [("en",)]
    # another device, another `Accept-Language`: the profile preference still applies
    other = api.other_client("203.0.113.9")
    assert api.login(client=other).status_code == 200
    title = _problem_title(other, {"Accept-Language": "fr"})
    assert title == t_lang("errors.not_found.title", language="en")
    assert title != t_lang("errors.not_found.title", language="fr")


@pytest.mark.req("ENF-I18N-001")
def test_ui_language_selection_refusals(api: AuthEnv) -> None:
    """A language is chosen only by a signed-in person, and only one that is delivered."""
    anonymous = api.client.patch(PREFERENCES, json={"language": "en"})
    assert anonymous.status_code == 401
    assert api.login().status_code == 200
    for value in ("de", "EN-US", "fr_FR", "", None, 3, ["en"]):
        response = api.client.patch(
            PREFERENCES, json={"language": value}, headers={"X-CSRF-Token": api.csrf()}
        )
        assert response.status_code == 422, value
    assert db_rows(api.settings, "SELECT locale FROM user WHERE username = 'admin'") == [("fr",)]


@pytest.mark.req("ENF-I18N-001")
def test_accept_language_selects_the_language_without_a_profile(api: AuthEnv) -> None:
    """Nobody is signed in: the `Accept-Language` of the request picks the language."""
    en = t_lang("errors.not_found.title", language="en")
    fr = t_lang("errors.not_found.title", language="fr")
    headers = {"Accept-Language": "en-GB,en;q=0.9,fr;q=0.8"}
    assert _problem_title(api.client, headers) == en
    assert _problem_title(api.client, {"Accept-Language": "fr-CA,fr;q=0.9"}) == fr


def _problem_title(client: TestClient, headers: dict[str, str]) -> str:
    response = client.get("/api/v1/not-a-route", headers=headers)
    assert response.status_code == 404, response.text
    body = response.json()
    assert body["code"] == error_code("not_found").code
    return str(body["title"])


@pytest.mark.req("ENF-I18N-001")
def test_accept_language_negotiation_is_pure() -> None:
    """A closed set of languages, the first preferred one that is spoken wins."""
    assert negotiate(None) == "fr"
    assert negotiate("") == "fr"
    assert negotiate("*") == "fr"
    assert negotiate("en-US,en;q=0.9,fr;q=0.8") == "en"
    assert negotiate("fr-CA,fr;q=0.9,en;q=0.5") == "fr"
    assert negotiate("de;q=1.0, fr;q=0.4, en;q=0.9") == "en"
    assert negotiate("de") == "fr"  # nothing that is spoken: the default
    assert negotiate("en;q=0") == "fr"  # an excluded language is not a choice
