"""ENF-LEG-008: docs and UI say "libre de droits aux États-Unis", never "source légale"."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.req("ENF-LEG-008")


def _lint():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "vocabulary_lint", ROOT / "tools" / "vocabulary_lint.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_ui_says_us_public_domain_not_legal_source() -> None:
    lint = _lint()
    assert lint.scan(ROOT) == []  # README, docs, fr.toml and fr.json as they stand
    toml = (ROOT / "backend/src/shelfhold/locales/fr.toml").read_text(encoding="utf-8")
    json_ = (ROOT / "frontend/src/lib/i18n/fr.json").read_text(encoding="utf-8")
    assert "libre de droits aux états-unis" in toml.casefold()
    assert "libre de droits aux états-unis" in json_.casefold()
    assert "domaine-public.md" in (ROOT / "docs/admin/README.md").read_text(encoding="utf-8")


def test_lint_flags_a_legal_source_wording(tmp_path: Path) -> None:
    lint = _lint()
    ui = tmp_path / "backend/src/shelfhold/locales"
    ui.mkdir(parents=True)
    (ui / "fr.toml").write_text('a = "Gutenberg est une source légale."\n', encoding="utf-8")
    docs = tmp_path / "docs/admin"
    docs.mkdir(parents=True)
    (docs / "x.md").write_text(
        "Standard Ebooks : sources légales.\nGutenberg est libre de droits.\n", encoding="utf-8"
    )
    found = lint.scan(tmp_path)
    assert len(found) == 3  # the UI line, the doc line naming it, the doc line missing the country


@pytest.mark.req("EF-SRC-013")
def test_proxy_never_documented_as_circumvention(tmp_path: Path) -> None:
    """J4, U2, ENF-LEG-006: the docs and the UI never present a proxy as a way to reach a blocked
    site or to get around a blocking measure; it routes the household's network, nothing else."""
    lint = _lint()
    assert lint.scan(ROOT) == []
    sources = (ROOT / "docs/admin/sources.md").read_text(encoding="utf-8")
    assert "proxy" in sources.casefold() and "réservé" in sources.casefold()
    ui = tmp_path / "backend/src/shelfhold/locales"
    ui.mkdir(parents=True)
    (ui / "fr.toml").write_text(
        'a = "Le proxy de la source permet de contourner un blocage."\n'
        'b = "Le proxy sert au routage réseau du foyer."\n',
        encoding="utf-8",
    )
    docs = tmp_path / "docs/admin"
    docs.mkdir(parents=True)
    (docs / "x.md").write_text(
        "Un proxy par source sert à accéder à un site bloqué.\n"
        "Configurez un proxy pour débloquer un catalogue géo-restreint.\n"
        "Use the proxy to bypass a block.\n"
        "Le proxy inverse fait le HTTPS.\n",
        encoding="utf-8",
    )
    found = lint.scan(tmp_path)
    assert len(found) == 4, found  # one UI line and three doc lines; the plain uses are fine
    assert all("proxy" in line for line in found)
