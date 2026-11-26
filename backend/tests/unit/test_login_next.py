"""The return path of the sign-in page: only a relative path of this site is ever followed."""

import pytest

from shelfhold.interfaces.http_api.login import login_page_html, safe_next

pytestmark = pytest.mark.req("EF-USR-021", "ENF-SEC-004")


@pytest.mark.parametrize(
    "raw",
    [
        "/",
        "/lite",
        "/bibliotheque",
        "/oeuvres/01HZ?tab=chapitres#haut",
        "/a/b%20c",
        "/%252Fx",
        "/a?next=//b",
        # a browser keeps these in the path: still this site
        "/\uff0fevil.example",
        "/\u2215evil.example",
    ],
)
def test_safe_next_keeps_an_internal_path(raw: str) -> None:
    assert safe_next(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "//evil.example",
        "///evil.example",
        "https://evil.example/",
        "http://evil.example",
        "javascript:alert(1)",
        "evil.example",
        "relative/path",
        "/\\evil.example",
        "\\evil.example",
        "\\\\evil.example",
        "/%2Fevil.example",
        "/%2fevil.example",
        "/%5Cevil.example",
        "/%5cevil.example",
        "/%0d%0aLocation:%20https://evil.example",
        "/ok\r\nSet-Cookie: a=b",
        "/ok\tx",
        "/ok\x00",
        "/ok\x7f",
        "/" + "a" * 3000,
        " //evil.example",
        "/%09/evil.example",
        "/%0a/evil.example",
        "/%2F%2Fevil.example",
        "HTTPS://evil.example",
        "/\t/evil.example",
        "/.//evil.example",
        "/a//b",
        "/./x",
        "/a/../b",
        "/%2E%2E/x",
    ],
)
def test_safe_next_refuses_anything_else(raw: str | None) -> None:
    assert safe_next(raw) == "/"


def test_login_page_carries_the_path_escaped_and_no_inline_code() -> None:
    html = login_page_html("https://code.example.org/", next_path='/a?x="1"&y=<2>')
    assert 'data-next="/a?x=&quot;1&quot;&amp;y=&lt;2&gt;"' in html
    assert "<2>" not in html


def test_login_page_names_the_provider_only_when_it_is_configured() -> None:
    plain = login_page_html("https://code.example.org/", oidc=True)
    named = login_page_html("https://code.example.org/", oidc=True, provider="sso.example.org")
    assert "Se connecter avec un fournisseur d&#x27;identité" in plain
    assert "Se connecter avec sso.example.org" in named
    assert "sso.example.org" not in login_page_html("https://code.example.org/")


def test_login_page_is_neutral_and_describes_its_field() -> None:
    html = login_page_html("https://code.example.org/").lower()
    assert 'href="#help"' in html and "mot de passe oublié" in html
    assert 'id="password-error"' in html and 'aria-describedby="password-error"' in html
    assert 'id="reveal"' in html and "hidden" in html  # the eye: hidden until the script is here
    assert "<img" not in html and "<svg" not in html and "<h2" not in html
