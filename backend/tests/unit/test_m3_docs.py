"""M3 L55: the plugin author journey, platforms, clients matrix, family and uninstall guides.

The author template is executed here: a template whose example does not pass the kit is a lie.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import typing
from pathlib import Path

import pytest

from shelfhold.sdk.fsops import FsOp
from shelfhold.sdk.models import SDK_VERSION
from shelfhold.sdk.testing import FakeFetcher, assert_conforms

ROOT = Path(__file__).resolve().parents[3]
PLUGINS = ROOT / "docs" / "plugins"
ADMIN = ROOT / "docs" / "admin"
FAMILY = ROOT / "docs" / "famille"
TEMPLATE = PLUGINS / "gabarit"
NEW_PAGES = [
    PLUGINS / "parcours.md",
    PLUGINS / "source.md",
    PLUGINS / "destination.md",
    PLUGINS / "kit.md",
    ADMIN / "plateformes.md",
    ADMIN / "clients.md",
    ADMIN / "nas-energie.md",
    ADMIN / "desinstaller.md",
    FAMILY / "README.md",
]


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.req("EF-SRC-030", "ENF-DOC-001")
def test_plugin_author_docs_cover_sources_and_targets() -> None:
    journey = _text(PLUGINS / "parcours.md")
    for part in ("gabarit", "kit de conformité", "exception de plugin", "liste d'admission",
                 "règle de stabilité", "FsOp", "source", "destination"):  # fmt: skip
        assert part in journey, part
    assert (
        "ENF-LEG-010" in journey and "AGPL" in journey
    )  # the licence under the provisional exception
    # the destination page names every operation of the frozen plan and says who may unlink
    destination = _text(PLUGINS / "destination.md")
    operations = typing.get_args(FsOp.model_fields["op"].annotation)
    assert operations, "FsOp.op is no longer a Literal"
    for op in operations:
        assert f"`{op}`" in destination, op
    assert re.search(r"unlink[^\n]*(hôte|l'hôte)", destination)
    assert ".shelfhold" in destination and "check_destination_plan" in destination
    # the kit page does not promise a destination kit that does not exist yet (M4)
    kit = _text(PLUGINS / "kit.md")
    assert "M4" in kit and "run_source_conformance" in kit and "run_metadata_conformance" in kit
    for page in (PLUGINS / "source.md", PLUGINS / "metadata-provider.md", PLUGINS / "kit.md"):
        assert f"SDK {SDK_VERSION}" in _text(page), page.name  # no page stuck on an older SDK


@pytest.mark.req("EF-SRC-030", "EF-SRC-036")
def test_stability_rule_is_documented_with_the_kit_case() -> None:
    text = _text(PLUGINS / "parcours.md") + _text(PLUGINS / "kit.md")
    assert "remote_unit_id" in text and "SeriesRef" in text
    assert "réinstallation" in text and "simulated_reinstall" in text


def _load(path: Path, name: str):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.req("EF-SRC-030", "ENF-QUAL-003")
def test_author_template_example_passes_the_kit() -> None:
    module = _load(TEMPLATE / "src" / "plugin_gabarit" / "source.py", "gabarit_source")
    fetcher = FakeFetcher.from_cassette(TEMPLATE / "tests" / "cassettes" / "catalogue.json")
    report = assert_conforms(module.ExampleSource(), fetcher)
    assert report.ok
    assert fetcher.unscripted == []
    # the template ships the files an author needs and its own test
    for name in ("README.md", "pyproject.toml", "tests/test_conformance.py", "NOTICE-LICENCE.md"):
        assert (TEMPLATE / name).is_file(), name


@pytest.mark.req("EF-SRC-030", "ENF-QUAL-003")
def test_author_template_own_test_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test the template hands to authors is the one that passes here."""
    monkeypatch.syspath_prepend(str(TEMPLATE / "src"))
    own = _load(TEMPLATE / "tests" / "test_conformance.py", "gabarit_test")
    own.test_the_example_source_conforms()


@pytest.mark.req("EF-SRC-030", "ENF-LEG-003")
def test_author_template_attacks_the_kit_with_a_broken_source() -> None:
    """An example that reaches the network on its own must fail the kit (negative test)."""
    module = _load(TEMPLATE / "src" / "plugin_gabarit" / "source.py", "gabarit_source_neg")

    class Leaky(module.ExampleSource):  # type: ignore[misc]
        async def list_units(self, f, ref):  # type: ignore[no-untyped-def]
            await f.get("https://not-in-the-script.invalid/")
            return await super().list_units(f, ref)

    fetcher = FakeFetcher.from_cassette(TEMPLATE / "tests" / "cassettes" / "catalogue.json")
    with pytest.raises(AssertionError):
        assert_conforms(Leaky(), fetcher)


@pytest.mark.req("ENF-COMP-002", "ENF-DOC-001")
def test_clients_matrix_has_offline_column() -> None:
    text = _text(ADMIN / "clients.md")
    header = next(line for line in text.splitlines() if line.startswith("| Client"))
    assert "Hors ligne" in header and "Formats d'image" in header
    for client in ("Navigateurs", "Safari iOS", "Boox", "Kindle", "Kobo", "KOReader", "Kover"):
        assert client in text, client
    assert "SPA au mieux" in text and "/lite" in text and "M5" in text
    assert "compte restreint" in text.lower() and "OPDS" in text
    assert "détection de capacités" in text.lower() and "User-Agent" in text
    # every row of the matrix answers the offline column with a fact or "non vérifié"
    rows = [r for r in text.splitlines() if r.startswith("| ") and "---" not in r][1:]
    matrix = [r for r in rows if r.split("|")[1].strip().startswith(
        ("Navigateurs", "Safari", "Boox", "Kindle", "Kobo", "KOReader", "Kover"))]  # fmt: skip
    assert len(matrix) >= 7
    for row in matrix:
        cells = [c.strip() for c in row.strip("|").split("|")]
        offline = cells[header.strip("|").split("|").index(next(
            h for h in header.strip("|").split("|") if "Hors ligne" in h))]  # fmt: skip
        assert offline, row
        assert "non vérifié" in offline.lower() or "source :" in offline.lower(), row


@pytest.mark.req("ENF-COMP-002")
def test_clients_matrix_unverified_facts_are_not_invented() -> None:
    """No cell claims a device was tested: the device report template is the only way in."""
    text = _text(ADMIN / "clients.md")
    assert "rapport d'" in text
    assert not re.search(r"\btesté sur\b|\bvérifié sur\b", text, re.I)
    (template,) = (ROOT / ".github" / "ISSUE_TEMPLATE").glob("rapport-*.yml")
    body = _text(template)
    assert "User-Agent" in body


@pytest.mark.req("ENF-PORT-003", "ENF-DOC-001")
def test_platform_docs_list_prerequisites() -> None:
    text = _text(ADMIN / "plateformes.md")
    for platform in ("Unraid", "TrueNAS", "Synology", "Podman", "Debian"):
        assert platform in text, platform
    assert "amd64" in text and "arm64" in text
    assert "minimum" in text.lower() and "recommandé" in text.lower()
    assert "Go" in text and "SSD" in text and "NFS" in text
    assert "1000" in text  # the uid the image runs as
    power = _text(ADMIN / "nas-energie.md")
    assert "Sablier" in power and "disque" in power.lower()


@pytest.mark.req("ENF-DOC-001")
def test_uninstall_guide_never_deletes_store_without_warning() -> None:
    text = _text(ADMIN / "desinstaller.md")
    assert "/store" in text and "sauvegarde" in text.lower() and "kit de secours" in text.lower()
    # no command that removes /store or /data without the warning before it
    for match in re.finditer(r"rm\s+-[a-zA-Z]*r[a-zA-Z]*\s+[^\n]*", text):
        command = match.group(0)
        before = text[: match.start()]
        assert "/store" not in command or "irréversible" in before.split("```")[-2].lower() or (
            "ATTENTION" in before[-800:]
        ), command  # fmt: skip
    # the destructive step is last and conditional
    assert text.index("ATTENTION") > text.index("sauvegarde")
    assert "à venir" in text.lower()  # the recovery kit is not shipped yet


@pytest.mark.req("ENF-DOC-001")
def test_family_docs_do_not_describe_missing_screens_as_existing() -> None:
    text = _text(FAMILY / "README.md")
    assert "à venir" in text.lower()
    assert "L56" in text  # the skeleton is flagged for the debts lot
    assert "bibliothèque personnelle du foyer" in text
    # every heading that describes a screen of M3 carries the mark
    for heading in re.findall(r"^##+ .*$", text, re.M):
        if re.search(r"écran|assistant|demande|profil", heading, re.I):
            assert "à venir" in heading.lower(), heading


@pytest.mark.req("ENF-LEG-006", "ENF-LEG-008", "ENF-DOC-001")
def test_docs_vocabulary_lint() -> None:
    """The vocabulary lint covers the new pages, and they obey it and the household wording."""
    spec = importlib.util.spec_from_file_location(
        "vocabulary_lint_m3", ROOT / "tools" / "vocabulary_lint.py"
    )
    assert spec is not None and spec.loader is not None
    lint = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = lint
    spec.loader.exec_module(lint)
    assert "docs/plugins" in lint.DOCS and "docs/famille" in lint.DOCS
    assert lint.scan(ROOT) == []
    broadcast = re.compile(r"\bpartag\w*", re.I)
    leaks = re.compile(r"/home/|/Users/|\.ts\.net|\b(?:\d{1,3}\.){3}\d{1,3}\b")
    for page in [*NEW_PAGES, *TEMPLATE.rglob("*.md"), *TEMPLATE.rglob("*.py")]:
        text = _text(page)
        assert not broadcast.search(text), page.name  # "partager" is never "diffuser"
        assert not [m for m in leaks.findall(text) if not m.startswith(("127.", "0."))], page.name


@pytest.mark.req("ENF-LEG-006", "ENF-LEG-008")
def test_docs_vocabulary_lint_flags_a_new_plugin_page(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "vocabulary_lint_neg", ROOT / "tools" / "vocabulary_lint.py"
    )
    assert spec is not None and spec.loader is not None
    lint = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = lint
    spec.loader.exec_module(lint)
    for folder in ("plugins", "famille"):
        (tmp_path / "docs" / folder).mkdir(parents=True)
        (tmp_path / "docs" / folder / "x.md").write_text(
            "Gutenberg est une source légale.\n", encoding="utf-8"
        )
    assert len(lint.scan(tmp_path)) == 2
