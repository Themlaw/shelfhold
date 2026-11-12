"""The vocabulary lint extended to the UI catalogs that are not for the admin (M3 L02).

ENF-I18N-004: the words of the code never reach a catalog a household member can read.
ENF-LEG-006: nothing in those catalogs invites to broadcast ("partager" as in diffuser).
EF-UX-006: "Exporter" is never a verb of the family."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
FORBIDDEN = (
    "binding", "rendition", "scope", "unité", "variante", "manifeste", "staging", "montage",
    "cible",
)  # fmt: skip


def _lint():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "vocabulary_lint", ROOT / "tools" / "vocabulary_lint.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _catalog(root: Path, entries: dict[str, str], name: str = "fr.json") -> None:
    folder = root / "frontend/src/lib/i18n"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")


@pytest.mark.req("ENF-I18N-004")
def test_ui_vocabulary_lint_forbidden_words(tmp_path: Path) -> None:
    lint = _lint()
    assert lint.scan(ROOT) == []  # the catalogs as they stand
    for word in FORBIDDEN:
        for text in (f"Choisir le {word}", f"Les {word.upper()}s sont prêts", f"Un {word}."):
            _catalog(tmp_path, {"library.line": text})
            found = lint.scan(tmp_path)
            assert len(found) == 1, text  # one offence per line, naming the key
            assert "library.line" in found[0]
    # an admin key may speak the language of the code; a family key never may
    _catalog(tmp_path, {"admin.target": "La cible du staging", "library.ok": "Une étagère"})
    assert lint.scan(tmp_path) == []
    # the English catalog is held to the English words
    _catalog(tmp_path, {"library.line": "The binding of the rendition"}, "en.json")
    assert len(lint.scan(tmp_path)) == 1


@pytest.mark.req("ENF-LEG-006")
def test_ui_vocabulary_lint_refuses_sharing_as_broadcasting(tmp_path: Path) -> None:
    lint = _lint()
    for text in ("Partager avec le monde", "Vous pouvez partager ce livre", "Partagé en ligne"):
        _catalog(tmp_path, {"library.share": text})
        assert len(lint.scan(tmp_path)) == 1, text
    _catalog(tmp_path, {"library.share": "The share link", "library.x": "Share it"}, "en.json")
    assert len(lint.scan(tmp_path)) == 2
    # the same sentence in the English catalog of an admin key is allowed
    _catalog(tmp_path, {"admin.share": "Share the logs"}, "en.json")
    assert lint.scan(tmp_path) == []


@pytest.mark.req("EF-UX-006")
def test_verbs_contract_family_never_sees_export(tmp_path: Path) -> None:
    lint = _lint()
    for text in ("Exporter", "Pour exporter ce livre", "Vous pouvez Exporter.", "EXPORTEZ-le"):
        _catalog(tmp_path, {"family.verb": text})
        found = lint.scan(tmp_path)
        assert len(found) == 1, text
        assert "family.verb" in found[0]
    _catalog(tmp_path, {"family.verb": "Export it", "family.verb2": "Exporting"}, "en.json")
    assert len(lint.scan(tmp_path)) >= 1
    # the verbs of the contract stay free
    _catalog(
        tmp_path,
        {
            "family.add": "Ajouter à ma bibliothèque",
            "family.ask": "Demander",
            "family.open": "Ouvrir dans Kavita",
        },
    )
    assert lint.scan(tmp_path) == []
    # and the real family catalogs never carry it
    for name in ("fr.json", "en.json"):
        path = ROOT / "frontend/src/lib/i18n" / name
        if not path.is_file():
            continue
        for key, text in json.loads(path.read_text(encoding="utf-8")).items():
            if not key.startswith("admin."):
                assert "exporter" not in text.casefold(), key
