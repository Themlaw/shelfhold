"""M6 L11: the plugin author path — publishing, index, hosting, lifecycle, migrations, isolation.

The author template is executed in test_m3_docs; here we check that the M6 chapters exist and
that the template depends on the published SDK.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PLUGINS = ROOT / "docs" / "plugins"
TEMPLATE = PLUGINS / "gabarit"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.req("EF-SRC-030", "ENF-DOC-001", "ENF-LEG-003")
def test_plugin_author_docs_cover_index_hosting_and_migrations() -> None:
    pages = [
        PLUGINS / "parcours.md",
        PLUGINS / "index.md",
        PLUGINS / "heberger-un-index.md",
        PLUGINS / "cycle-de-vie.md",
        PLUGINS / "compatibilite.md",
        PLUGINS / "isolation.md",
        PLUGINS / "kit.md",
    ]
    text = "\n".join(_text(p) for p in pages)
    for part in (
        "minisign",
        "PyPI",
        "wheel",
        "index",
        "migrate_state",
        "deprecated",
        "replaced_by",
        "min_host",
        "isolé",
        "licence",
        "ENF-LEG-003",
        "payload_schema",
        "state_newer",
        "plugin.incompatible",
        "rotation",
        "next_pubkey",
    ):
        assert part in text, part
    assert "state_newer" in text
    assert "90" in text


@pytest.mark.req("EF-SRC-030", "ENF-LEG-003", "ENF-LEG-006", "ENF-LEG-008", "ENF-DOC-001")
def test_docs_vocabulary_lint() -> None:
    spec = importlib.util.spec_from_file_location(
        "vocabulary_lint_m6", ROOT / "tools" / "vocabulary_lint.py"
    )
    assert spec is not None and spec.loader is not None
    lint = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = lint
    spec.loader.exec_module(lint)
    assert "docs/plugins" in lint.DOCS
    assert lint.scan(ROOT) == []


@pytest.mark.req("EF-SRC-030", "ENF-QUAL-003")
def test_plugin_template_depends_on_published_sdk() -> None:
    text = _text(TEMPLATE / "pyproject.toml")
    assert "shelfhold-sdk" in text
    assert ">=" in text
    assert "path" not in text.lower() or "shelfhold-sdk>=2.0" in text
    assert "2.0" in text
