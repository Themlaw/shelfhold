"""Tests of tools/traceability.py itself (exempted from the `req` marker, see
tools/traceability-exceptions.toml)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

TOOL = Path(__file__).resolve().parents[3] / "tools" / "traceability.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("traceability", TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["traceability"] = module
    spec.loader.exec_module(module)
    return module


tr = _load()

CDC = """\
| ID | Exigence | Critère | Jalon | Origine |
|---|---|---|---|---|
| EF-AAA-001 | texte avec \\| pipe | critère | M1a | A1 |
| EF-AAA-002 | x | y | M1a / M1b | A1 |
| EF-AAA-003 | x | y | M1b (garde) / M0 | A1 |
| EF-AAA-004 | x | y | Plus tard | A1 |
| ID | Sujet | Exigence | Vérification | Jalon | Origine |
| ENF-BBB-001 | s | e | v | M0 | — |
| ENF-BBB-002 | s | e | v | M0 → M6 | — |
| ENF-BBB-003 | s | e | v | M3 | — |
"""

ROADMAP = """\
### M0 — Fondations

**Exigences couvertes.** ENF-BBB-001, ENF-BBB-002 (volet docs), EF-AAA-003, ENF-ZZZ-009.

### M1a — Pont

**Exigences couvertes.**
- EF : EF-AAA-001, EF-AAA-002
- Décompte : EF-AAA-004

**Tests écrits d'abord.**
EF-AAA-004 hors couverture
"""


def test_requirements_parse_ids_and_milestones() -> None:
    reqs = tr.parse_requirements(CDC)
    assert set(reqs) == {
        "EF-AAA-001",
        "EF-AAA-002",
        "EF-AAA-003",
        "EF-AAA-004",
        "ENF-BBB-001",
        "ENF-BBB-002",
        "ENF-BBB-003",
    }
    assert reqs["EF-AAA-002"] == ["M1a", "M1b"]
    assert reqs["EF-AAA-004"] == []


def test_earliest_milestone_wins_even_when_listed_last() -> None:
    reqs = tr.parse_requirements(CDC)
    assert tr.earliest(reqs["EF-AAA-003"]) == "M0"
    assert tr.earliest(reqs["EF-AAA-002"]) == "M1a"
    assert tr.earliest(reqs["ENF-BBB-002"]) == "M0"
    assert tr.earliest(reqs["EF-AAA-004"]) is None


def test_roadmap_coverage_by_milestone_stops_at_next_block() -> None:
    cov = tr.parse_roadmap_coverage(ROADMAP)
    assert cov["M0"] == {"ENF-BBB-001", "ENF-BBB-002", "EF-AAA-003", "ENF-ZZZ-009"}
    assert cov["M1a"] == {"EF-AAA-001", "EF-AAA-002"}


def test_spec_discrepancies_are_reported_both_ways() -> None:
    reqs = tr.parse_requirements(CDC)
    cov = tr.parse_roadmap_coverage(ROADMAP)
    issues = tr.compare_specs(reqs, cov, "M0")
    assert any("ENF-ZZZ-009" in i and "inconnue" in i for i in issues)
    assert not any("ENF-BBB-001" in i for i in issues)
    cov["M0"].discard("ENF-BBB-002")
    assert any("ENF-BBB-002" in i for i in tr.compare_specs(reqs, cov, "M0"))


def test_python_markers_function_class_and_module_level(tmp_path: Path) -> None:
    f = tmp_path / "backend" / "tests" / "unit" / "test_x.py"
    f.parent.mkdir(parents=True)
    f.write_text(
        "import pytest\n"
        "pytestmark = pytest.mark.req('ENF-BBB-003')\n"
        "@pytest.mark.req('ENF-BBB-001', 'EF-AAA-001')\n"
        "def test_a(): pass\n"
        "def test_b(): pass\n"
        "@pytest.mark.req('ENF-BBB-002')\n"
        "class TestC:\n"
        "    def test_d(self): pass\n"
        "def helper(): pass\n",
        encoding="utf-8",
    )
    found = {t.name: t.reqs for t in tr.collect_python(tmp_path)}
    assert found["test_a"] == ["ENF-BBB-001", "EF-AAA-001", "ENF-BBB-003"]
    assert found["test_b"] == ["ENF-BBB-003"]
    assert found["TestC::test_d"] == ["ENF-BBB-002", "ENF-BBB-003"]
    assert "helper" not in found


def test_python_markers_are_also_collected_in_e2e(tmp_path: Path) -> None:
    f = tmp_path / "e2e" / "tests" / "test_s.py"
    f.parent.mkdir(parents=True)
    f.write_text("import pytest\n@pytest.mark.req('ENF-BBB-001')\ndef test_s(): pass\n")
    assert [(t.path, t.reqs) for t in tr.collect_python(tmp_path)] == [
        ("e2e/tests/test_s.py", ["ENF-BBB-001"])
    ]


def test_typescript_markers_comment_before_test_and_file_level(tmp_path: Path) -> None:
    f = tmp_path / "e2e" / "tests" / "a.spec.ts"
    f.parent.mkdir(parents=True)
    f.write_text(
        "// req-file: ENF-BBB-003\n"
        "// req: ENF-BBB-001, EF-AAA-001\n"
        "test('one', async () => {});\n"
        "\n"
        "it('two', () => {});\n"
        "describe('g', () => {\n"
        "  // req: ENF-BBB-002\n"
        "  it.skip(`three`, () => {});\n"
        "});\n",
        encoding="utf-8",
    )
    g = tmp_path / "frontend" / "node_modules" / "p" / "a.test.ts"
    g.parent.mkdir(parents=True)
    g.write_text("it('ignored', () => {});\n", encoding="utf-8")
    found = {t.name: t.reqs for t in tr.collect_typescript(tmp_path)}
    assert found == {
        "one": ["ENF-BBB-001", "EF-AAA-001", "ENF-BBB-003"],
        "two": ["ENF-BBB-003"],
        "three": ["ENF-BBB-002", "ENF-BBB-003"],
    }


def _mini_repo(tmp_path: Path, test_body: str, exceptions: str = "") -> Path:
    (tmp_path / "docs" / "spec").mkdir(parents=True)
    (tmp_path / "docs" / "spec" / "01-cahier-des-charges.md").write_text(CDC, encoding="utf-8")
    (tmp_path / "docs" / "spec" / "03-roadmap-tdd.md").write_text(ROADMAP, encoding="utf-8")
    t = tmp_path / "backend" / "tests" / "unit"
    t.mkdir(parents=True)
    (t / "test_x.py").write_text(test_body, encoding="utf-8")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "traceability-exceptions.toml").write_text(exceptions, encoding="utf-8")
    return tmp_path


FULL = (
    "import pytest\n"
    "@pytest.mark.req('ENF-BBB-001', 'ENF-BBB-002', 'EF-AAA-003')\n"
    "def test_all(): pass\n"
)


def test_main_succeeds_and_writes_the_matrix_when_everything_is_covered(
    tmp_path: Path,
) -> None:
    root = _mini_repo(tmp_path, FULL)
    out = root / "docs" / "traceability" / "M0.md"
    code = tr.main(["--milestone", "M0", "--root", str(root), "--output", str(out)])
    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert "| ENF-BBB-001 |" in text
    assert "test_x.py::test_all" in text
    assert "ENF-BBB-003" not in text


def test_main_fails_on_an_uncovered_requirement_and_lists_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _mini_repo(tmp_path, FULL.replace("'ENF-BBB-002', ", ""))
    out = root / "m0.md"
    code = tr.main(["--milestone", "M0", "--root", str(root), "--output", str(out)])
    assert code == 1
    assert "ENF-BBB-002" in capsys.readouterr().err
    assert "NON COUVERTE" in out.read_text(encoding="utf-8")


def test_main_fails_on_an_unknown_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _mini_repo(tmp_path, FULL.replace("EF-AAA-003", "EF-NOPE-999"))
    code = tr.main(["--milestone", "M0", "--root", str(root), "--output", str(root / "o.md")])
    assert code == 1
    assert "EF-NOPE-999" in capsys.readouterr().err


def test_main_fails_on_an_orphan_test_unless_excepted(tmp_path: Path) -> None:
    body = FULL + "def test_orphan(): pass\n"
    root = _mini_repo(tmp_path, body)
    args = ["--milestone", "M0", "--root", str(root), "--output", str(root / "o.md")]
    assert tr.main(args) == 1
    exc = (
        '[[exception]]\npath = "backend/tests/unit/test_x.py"\n'
        'test = "test_orphan"\nreason = "outil de test"\n'
    )
    (root / "tools" / "traceability-exceptions.toml").write_text(exc, encoding="utf-8")
    assert tr.main(args) == 0


def test_an_exception_without_reason_is_refused(tmp_path: Path) -> None:
    root = _mini_repo(tmp_path, FULL, '[[exception]]\npath = "backend/tests/unit/test_x.py"\n')
    with pytest.raises(SystemExit):
        tr.main(["--milestone", "M0", "--root", str(root), "--output", str(root / "o.md")])


def test_conditional_tests_are_flagged_in_python_and_typescript(tmp_path: Path) -> None:
    py = tmp_path / "backend" / "tests" / "contract" / "test_c.py"
    py.parent.mkdir(parents=True)
    py.write_text(
        "import pytest\n"
        "pytestmark = pytest.mark.contract\n"
        "@pytest.mark.req('ENF-BBB-001')\n"
        "def test_a(): pass\n",
        encoding="utf-8",
    )
    py2 = tmp_path / "backend" / "tests" / "unit" / "test_d.py"
    py2.parent.mkdir(parents=True)
    py2.write_text(
        "import pytest\n"
        "@pytest.mark.req('ENF-BBB-001')\n"
        "@pytest.mark.skipif(True, reason='x')\n"
        "def test_b(): pass\n"
        "@pytest.mark.req('ENF-BBB-001')\n"
        "@pytest.mark.xfail\n"
        "def test_c(): pass\n"
        "@pytest.mark.req('ENF-BBB-001')\n"
        "def test_plain(): pass\n",
        encoding="utf-8",
    )
    ts = tmp_path / "frontend" / "a.test.ts"
    ts.parent.mkdir(parents=True)
    ts.write_text("// req: ENF-BBB-001\nit.skip('s', () => {});\n", encoding="utf-8")
    flags = {t.name: bool(t.conditional) for t in tr.collect_python(tmp_path)}
    assert flags == {"test_a": True, "test_b": True, "test_c": True, "test_plain": False}
    assert [bool(t.conditional) for t in tr.collect_typescript(tmp_path)] == [True]


COND = (
    "import pytest\n"
    "@pytest.mark.req('ENF-BBB-001', 'ENF-BBB-002')\n"
    "def test_all(): pass\n"
    "@pytest.mark.req('EF-AAA-003')\n"
    "@pytest.mark.skipif(True, reason='needs docker')\n"
    "def test_cond(): pass\n"
)
COND_EXC = (
    '[[out_of_required_ci]]\npath = "backend/tests/unit/test_x.py"\n'
    'workflow = "contract.yml"\nreason = "banc de contrat hebdomadaire"\n'
)


def test_requirement_only_covered_conditionally_needs_a_justified_workflow(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _mini_repo(tmp_path, COND)
    args = ["--milestone", "M0", "--root", str(root), "--output", str(root / "o.md")]
    assert tr.main(args) == 1
    assert "EF-AAA-003" in capsys.readouterr().err
    (root / "tools" / "traceability-exceptions.toml").write_text(COND_EXC, encoding="utf-8")
    assert tr.main(args) == 0
    text = (root / "o.md").read_text(encoding="utf-8")
    assert "COUVERTE (hors CI requise)" in text
    assert "contract.yml" in text


def test_an_out_of_required_ci_entry_needs_workflow_and_reason(tmp_path: Path) -> None:
    root = _mini_repo(tmp_path, COND, '[[out_of_required_ci]]\npath = "a.py"\n')
    with pytest.raises(SystemExit):
        tr.main(["--milestone", "M0", "--root", str(root), "--output", str(root / "o.md")])


def test_deferred_aspects_are_listed_in_the_matrix(tmp_path: Path) -> None:
    exc = '[[deferred]]\nrequirement = "ENF-BBB-001"\naspect = "budget"\nreason = "plus tard"\n'
    root = _mini_repo(tmp_path, FULL, exc)
    assert tr.main(["--milestone", "M0", "--root", str(root), "--output", str(root / "o.md")]) == 0
    text = (root / "o.md").read_text(encoding="utf-8")
    assert "Volets reportés" in text
    assert "budget" in text
