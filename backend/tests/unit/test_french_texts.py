"""EF-UX-003 / ENF-I18N-003: French texts for every error code, ICU plurals, narrow no-break spaces.

The checks walk the declarations (`all_error_codes()`) and the whole catalog: a code or a message
added later is covered without touching this file.
"""

import ast
import re
from pathlib import Path

import pytest

from shelfhold.export.domain.naming import NamingTemplate
from shelfhold.i18n import load_catalog, render, t, typeset
from shelfhold.interfaces.http_api.catalog import all_error_codes
from shelfhold.interfaces.http_api.problems import cause_of

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"
NNBSP = chr(0x202F)
NBSP = chr(0x00A0)
BEFORE_PUNCTUATION = re.compile(f"[ {NBSP}]+(?=[;:!?])")


def _outside_code_spans(text: str) -> str:
    return re.sub(r"`[^`]*`", "", text)


@pytest.mark.req("EF-UX-003")
def test_error_catalog_codes_have_fr_texts() -> None:
    """Every code has its own text, cause and action; nothing is left to a generic sentence."""
    catalog = load_catalog("fr")
    codes = all_error_codes()
    assert codes
    missing: list[str] = []
    for spec in codes:
        for key in (spec.title_key, spec.cause_key, spec.action_key):
            if not catalog.get(key, "").strip():
                missing.append(key)
    assert missing == []
    for spec in codes:
        title, cause, action = (
            t(spec.title_key),
            cause_of(spec),
            t(spec.action_key),
        )
        assert len({title, cause, action}) == 3, f"{spec.code}: two of the three texts are equal"
        for text in (title, cause, action):
            assert text.endswith((".", ")", "»", "`", ":")), f"{spec.code}: unfinished {text!r}"
        # A closed vocabulary: a cause names no runtime value, so it has no placeholder.
        assert "{" not in catalog[spec.cause_key], f"{spec.code}: placeholder in the cause"


@pytest.mark.req("EF-UX-003")
def test_error_texts_belong_to_a_declared_code() -> None:
    """Both ways: no section of the catalog for a code nobody declares."""
    declared = {spec.code for spec in all_error_codes()}
    in_catalog = {
        key.removeprefix("errors.").rsplit(".", 1)[0]
        for key in load_catalog("fr")
        if key.startswith("errors.") and not key.startswith("errors.cause_default.")
    }
    assert in_catalog - declared == set()


@pytest.mark.req("ENF-I18N-003")
def test_icu_plurals_and_narrow_nbsp() -> None:
    template = "{count, plural, =0 {aucun fichier} one {# fichier} other {# fichiers}}"
    assert render(template, count=0) == "aucun fichier"
    assert render(template, count=1) == "1 fichier"
    assert render(template, count=2) == "2 fichiers"
    # In French 0 is singular for the category `one`, an exact `=0` wins over it.
    assert render("{n, plural, one {# jour} other {# jours}}", n=0) == "0 jour"
    # The thousands separator is a narrow no-break space (Intl fr-FR).
    assert render(template, count=1500) == f"1{NNBSP}500 fichiers"
    assert render("{size:fr} octets", size=1234567) == f"1{NNBSP}234{NNBSP}567 octets"
    assert render("{size:fr} octets", size=12) == "12 octets"
    # `many` (millions) falls back to `other` when the message gives no `many`.
    assert render(template, count=1_000_000) == f"1{NNBSP}000{NNBSP}000 fichiers"
    # Placeholders and plurals nest.
    nested = "{n, plural, one {{who} a # point} other {{who} a # points}}"
    assert render(nested, n=3, who="Léa") == "Léa a 3 points"
    # Punctuation: a space before ; : ! ? is replaced by a narrow no-break space.
    assert (
        typeset("Bonjour : monde ; oui ! non ?")
        == f"Bonjour{NNBSP}: monde{NNBSP}; oui{NNBSP}! non{NNBSP}?"
    )
    assert typeset(f"déjà{NNBSP}: fait{NBSP}!") == f"déjà{NNBSP}: fait{NNBSP}!"


@pytest.mark.req("ENF-I18N-003")
def test_typography_leaves_code_urls_and_user_values_alone() -> None:
    assert (
        typeset("Utilisez `a : b` puis http://hote:8080")
        == "Utilisez `a : b` puis http://hote:8080"
    )
    assert typeset("Fichier « utilisateur:mot_de_passe » ; fini") == (
        f"Fichier « utilisateur:mot_de_passe »{NNBSP}; fini"
    )
    assert typeset('[{{"local": "/a"}}]') == '[{{"local": "/a"}}]'
    # What the user typed or what a source sent is never retypeset: only the catalog is.
    shown = render(
        "Série {name} : {count, plural, one {# fichier} other {# fichiers}}",
        name="Quoi : ça ?",
        count=2,
    )
    assert shown == f"Série Quoi : ça ?{NNBSP}: 2 fichiers"


@pytest.mark.req("ENF-I18N-003")
def test_every_catalog_message_is_typeset_and_free_of_plural_hacks() -> None:
    catalog = load_catalog("fr")
    spaced = [k for k, v in catalog.items() if BEFORE_PUNCTUATION.search(_outside_code_spans(v))]
    assert spaced == [], "a plain space before ; : ! ? survived load_catalog"
    assert any(NNBSP in v for v in catalog.values())
    # "fichier(s)", "retirée(s)": the plural is an ICU message, not a parenthesis.
    hacks = [
        k
        for k, v in catalog.items()
        if re.search(r"\w\((?:s|e|es|aux)\)", v.replace("http(s)", ""))
    ]
    assert hacks == []


@pytest.mark.req("ENF-I18N-003")
@pytest.mark.parametrize(
    ("key", "params", "expected"),
    [
        ("cli.shelf_withdraw_pending", {"count": 1}, "1 fichier reste à déplacer"),
        ("cli.shelf_withdraw_pending", {"count": 3}, "3 fichiers restent à déplacer"),
        ("cli.shelf_withdraw_nothing", {}, "Aucune Édition à retirer."),
        ("ops.sessions_revoked", {"count": 0}, "0 session fermée"),
        ("ops.sessions_revoked", {"count": 2}, "2 sessions fermées"),
    ],
)
def test_real_messages_use_icu_plurals(key: str, params: dict[str, int], expected: str) -> None:
    assert expected in t(key, **params)


@pytest.mark.req("ENF-I18N-003")
def test_tokens_read_by_kavita_never_go_through_the_catalog() -> None:
    """The naming tokens keep plain ASCII spaces and no export module imports the catalog."""
    template = NamingTemplate(template_id="t", version=1)
    for token in (template.chapter, template.volume, template.special, template.segment):
        assert NNBSP not in token and NBSP not in token
    assert (template.chapter, template.volume) == (
        "{series} - Ch.{number}",
        "{series} - Vol.{number}",
    )
    assert template.special_dir == "Specials"
    importers: list[str] = []
    for folder in ("export", "infrastructure/packaging"):
        for path in (SRC / folder).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                    if isinstance(node, ast.ImportFrom)
                    else []
                )
                if any(n == "shelfhold.i18n" or n.startswith("shelfhold.i18n.") for n in names):
                    importers.append(path.name)
    assert importers == []
