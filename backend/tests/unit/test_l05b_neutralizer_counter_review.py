"""Relecture adverse indépendante de la PR #365 (`xhtml-neutralizer@v2`), rien n'est corrigé ici.

Chaque test `xfail(strict=True)` prouve un trou que R1 à R4 n'ont pas couvert ; il est rouge
exprès jusqu'à la correction. Les assertions ne réutilisent rien du neutraliseur : elles cherchent
dans la sortie la cible que le document hostile veut faire charger.

* C1 (haute, CSS) : une chaîne CSS coupée par un saut de ligne. Pour le neutraliseur, une chaîne
  court jusqu'au guillemet fermant, sauts de ligne compris ; pour un navigateur (CSS Syntax 3,
  « consume a string token »), un saut de ligne non échappé la termine en `bad-string`, et la
  suite est relue comme du CSS. Tout ce que l'analyse ne cherche que dans la copie « chaînes
  vidées » (`image-set(`, `@import`, `image(`, `cross-fade(`…) passe donc caché « dans » une
  chaîne. `image-set('/api/…' 1x)` est une requête GET authentifiée vers l'instance (autorisée par
  `img-src 'self'` du lecteur) ; `//hôte` est un chargement distant. Vaut pour `style=`
  (`&#10;` dans l'attribut), `<style>` et les fichiers `.css`.
* C2 (moyenne, SVG) : un attribut de présentation SVG (`fill`, `mask`, `clip-path`…) est lu par
  le navigateur comme une valeur CSS, échappements compris : `u\\72l(` est le jeton `url(`. Le
  neutraliseur ne contrôle la valeur que si la copie « écrasée » contient `url` : avec
  l'échappement, la valeur part telle quelle, `mask="u\\72l(//hôte/m.svg#m)"` compris. Le
  contrôle CSS refuse bien `\\` dans `style=`, pas dans ces attributs.
"""

from __future__ import annotations

import pytest

from shelfhold.infrastructure.content_trust import neutralize

_PAGE = (
    '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>t</title></head><body>{}</body></html>'
)


def _page(body: str) -> str:
    return neutralize(_PAGE.format(body).encode(), "xhtml").decode()


def test_c1_style_attribute_hides_image_set_behind_a_newline_in_a_string() -> None:
    out = _page(
        "<p style=\"content:&quot;a&#10;;background-image:image-set('/api/session' 1x);"
        'x:&quot;">t</p>'
    )
    assert "/api/session" not in out


def test_c1_style_element_hides_image_set_behind_a_newline_in_a_string() -> None:
    out = _page(
        "<style>p{content:\"a\n;background-image:image-set('//evil.example/b.png' 1x);"
        'x:"}\n</style><p>t</p>'
    )
    assert "evil.example" not in out


def test_c1_style_sheet_file_hides_image_set_behind_a_newline_in_a_string() -> None:
    sheet = "p{content:\"a\n;background-image:image-set('//evil.example/c.png' 1x);x:\"}\n"
    out = neutralize(sheet.encode(), "css").decode()
    assert "evil.example" not in out


def test_c2_svg_presentation_attribute_hides_url_behind_a_css_escape() -> None:
    out = _page(
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<rect width="9" height="9" fill="u\\72l(//evil.example/p.svg#p)"'
        ' mask="u\\72l(/api/m.svg#m)" clip-path="u\\72l(//evil.example/c.svg#c)"/></svg>'
    )
    assert "evil.example" not in out
    assert "/api/m.svg" not in out


def test_c_holds_escaped_url_in_a_style_attribute() -> None:
    """Contrôle : le même échappement dans `style=` est déjà refusé (le `\\` y est interdit)."""
    out = _page('<p style="background:u\\72l(//evil.example/s.png)">t</p>')
    assert "evil.example" not in out
