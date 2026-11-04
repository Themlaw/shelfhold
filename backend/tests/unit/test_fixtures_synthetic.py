"""ENF-LEG-002: synthetic fixtures with a hash manifest; cassette header allowlist."""

from __future__ import annotations

import importlib.util
import json
import zipfile
from pathlib import Path
from types import ModuleType

TESTS = Path(__file__).resolve().parents[1]
SYNTH = TESTS / "fixtures" / "synthetic"
CASSETTES = TESTS / "cassettes"
NON_FIXTURE = {"generate.py", "MANIFEST.sha256", "__pycache__"}


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _committed_files() -> dict[str, Path]:
    return {
        p.relative_to(SYNTH).as_posix(): p
        for p in sorted(SYNTH.rglob("*"))
        if p.is_file() and not (set(p.relative_to(SYNTH).parts) & NON_FIXTURE)
    }


def test_fixtures_are_synthetic_manifest_matches(tmp_path: Path) -> None:
    gen = _load(SYNTH / "generate.py", "synthetic_generate")
    manifest_lines = (SYNTH / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines()
    manifest = {}
    for line in manifest_lines:
        digest, name = line.split("  ", 1)
        manifest[name] = digest
    committed = _committed_files()
    # No file outside the manifest, no manifest entry without a file.
    assert set(manifest) == set(committed)
    assert manifest, "manifest must not be empty"
    assert any(n.endswith(".cbz") for n in manifest)
    assert any(n.endswith(".epub") for n in manifest)
    for name, path in committed.items():
        assert gen.sha256_file(path) == manifest[name], name
    # The generator is deterministic and reproduces the committed bytes.
    gen.generate(tmp_path)
    regenerated = {
        p.relative_to(tmp_path).as_posix(): p for p in sorted(tmp_path.rglob("*")) if p.is_file()
    }
    assert {k: v for k, v in regenerated.items() if k != "MANIFEST.sha256"}.keys() == set(committed)
    for name, path in regenerated.items():
        assert path.read_bytes() == (SYNTH / name).read_bytes(), name
    # Content sanity: CBZ carry a minimal ComicInfo.xml, EPUB has the mimetype first.
    for name, path in committed.items():
        if name.endswith(".cbz"):
            with zipfile.ZipFile(path) as z:
                assert "ComicInfo.xml" in z.namelist()
                assert z.testzip() is None
        if name.endswith(".epub"):
            with zipfile.ZipFile(path) as z:
                assert z.namelist()[0] == "mimetype"


def test_cassettes_header_allowlist() -> None:
    rules = _load(CASSETTES / "header_allowlist.py", "cassette_rules")
    files = sorted(CASSETTES.rglob("*.json"))
    assert files, "at least one synthetic example cassette is expected"
    for f in files:
        violations = rules.check_cassette(json.loads(f.read_text(encoding="utf-8")))
        assert violations == [], f"{f}: {violations}"
    # The rule really rejects forbidden and unknown headers.
    bad = {
        "interactions": [
            {
                "request": {"headers": {"Authorization": "x", "Cookie": "a=b", "Accept": "*/*"}},
                "response": {"headers": {"Set-Cookie": "a=b", "X-Unknown": "1"}},
            }
        ]
    }
    found = " ".join(rules.check_cassette(bad)).lower()
    for header in ("authorization", "cookie", "set-cookie", "x-unknown"):
        assert header in found
