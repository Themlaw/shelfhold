"""How a page of `/lite` is made: five templates, one stylesheet, and no script anywhere.

Every value that reaches a template is escaped here (`esc`), except a fragment this package built
itself (a placeholder named `html_…`); a template holds no logic and a title is never a link, an
attribute name or a class. The stylesheet is the page's only inline code: the CSP of every answer
names it by hash (`default-src 'none'`, no `'unsafe-inline'`, no `script-src`), so even a page
that were made to hold a tag would run nothing. `/lite` loads nothing from the front and takes no
theme (02 §11 bis).
"""

import re
from html import escape
from pathlib import Path
from string import Template
from urllib.parse import urlencode

from fastapi.responses import HTMLResponse

from shelfhold.i18n import t_lang
from shelfhold.infrastructure.http_headers import source_hash

__all__ = ["HEADERS", "Page", "Paths", "esc", "fragment", "page_response", "render_fragment", "t"]

_TEMPLATES = Path(__file__).parent / "templates"
STYLE = (_TEMPLATES / "style.css").read_text("utf-8")
CSP = Template((_TEMPLATES / "csp.txt").read_text("utf-8")).substitute(
    style_hash=source_hash(STYLE)
)
# What depends on the profile is never kept (a cache, a back button on a shared e-reader, a proxy)
HEADERS = {
    "Cache-Control": "no-store",
    "Vary": "Cookie",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": CSP,
}
_cache: dict[str, Template] = {}
_fragments: dict[str, Template] = {}
_SECTION = re.compile(r"<!--@(\w+)-->\n")


def esc(value: object) -> str:
    return escape(str(value), quote=True)


def _template(name: str) -> Template:
    if name not in _cache:
        _cache[name] = Template((_TEMPLATES / f"{name}.html").read_text("utf-8"))
    return _cache[name]


def render_fragment(name: str, **values: str) -> str:
    """A template filled with escaped values; a key that starts with `html_` is a fragment made
    here, already safe."""
    return _template(name).substitute(
        {key: value if key.startswith("html_") else esc(value) for key, value in values.items()}
    )


def t(key: str, page: "Page", /, **params: object) -> str:
    """A sentence of the catalog, in the language of the page."""
    return t_lang(key, language=page.lang, **params)


def fragment(name: str, **values: str) -> str:
    """A small piece of markup of `fragments.html`, filled the same way (`html_…` is safe)."""
    if not _fragments:
        parts = _SECTION.split((_TEMPLATES / "fragments.html").read_text("utf-8"))
        for key, text in zip(parts[1::2], parts[2::2], strict=True):
            _fragments[key] = Template(text)
    return _fragments[name].substitute(
        {key: value if key.startswith("html_") else esc(value) for key, value in values.items()}
    )


class Paths:
    """The hrefs of `/lite`, all under `{prefix}/lite` (`path_safety.mount_prefix`)."""

    def __init__(self, prefix: str) -> None:
        self.base = f"{prefix}/lite"

    def _with(self, path: str, **query: object) -> str:
        wanted = {k: v for k, v in query.items() if v not in (None, "", 0)}
        return f"{self.base}{path}" + (f"?{urlencode(wanted)}" if wanted else "")

    def home(self) -> str:
        return self.base

    def sign_in(self) -> str:
        return self._with("/sign-in")

    def sign_out(self) -> str:
        return self._with("/sign-out")

    def shelves(self) -> str:
        return self._with("/shelves")

    def shelf(self, shelf_id: str, offset: int = 0) -> str:
        return self._with(f"/shelves/{shelf_id}", offset=offset)

    def series_list(self, offset: int = 0) -> str:
        return self._with("/series", offset=offset)

    def series(self, series_id: str, offset: int = 0) -> str:
        return self._with(f"/series/{series_id}", offset=offset)

    def works(self, offset: int = 0) -> str:
        return self._with("/works", offset=offset)

    def recent(self, offset: int = 0) -> str:
        return self._with("/recent", offset=offset)

    def work(self, work_id: str) -> str:
        return self._with(f"/works/{work_id}")

    def search(self, q: str = "", offset: int = 0) -> str:
        return self._with("/search", q=q, offset=offset)

    def cover(self, content_hash: str, size: int) -> str:
        return self._with(f"/covers/{content_hash}/{size}")

    def complete(self, edition_id: str, part: int = 1) -> str:
        return self._with(f"/editions/{edition_id}/complete", part=part if part > 1 else None)


class Page:
    """What a page needs to put itself together: the language, the paths, and (signed in) the
    token of its forms."""

    def __init__(self, lang: str, paths: Paths, csrf: str | None = None) -> None:
        self.lang, self.paths, self.csrf = lang, paths, csrf

    def header(self, query: str = "") -> str:
        if self.csrf is None:
            return ""
        return fragment(
            "header",
            home=self.paths.home(),
            home_label=t("lite.home", self),
            search_action=self.paths.search(),
            search_label=t("lite.search_label", self),
            query=query,
            search_button=t("lite.search_button", self),
            sign_out_action=self.paths.sign_out(),
            csrf=self.csrf,
            sign_out_label=t("lite.sign_out", self),
        )


def page_response(
    page: Page,
    *,
    title: str,
    body: str,
    status: int = 200,
    query: str = "",
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    html = _template("page").substitute(
        lang=esc(page.lang), title=esc(title), style=STYLE, header=page.header(query), body=body
    )
    return HTMLResponse(html, status_code=status, headers={**HEADERS, **(headers or {})})
