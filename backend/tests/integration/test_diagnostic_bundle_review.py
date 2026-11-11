"""Review of #132 (M2 L30): the leaks shown by the review, kept as regressions.

Each test fails if its leak comes back: a short title, a sub-source name, a host or a source key
that went through the positive list because it *looked* like a code word (`{"reason":
"bleach"}`, `{"error": "NARUTO"}`, `{"dune": 1}`, `{"logger": "plugin.monster"}`, a title as a
slug, in hexadecimal, in base32, under an id field); the last barrier that did not see short
values; `--log-file` that read the database, `/proc` or a link, and crashed on a deep line.
"""

from __future__ import annotations

import ast
import base64
import io
import json
import unicodedata
import zipfile
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog
from shelfhold.ops import diagnostic
from shelfhold.ops.diagnostic import (
    DiagnosticLeakError,
    Pseudonyms,
    _Forbidden,
    canonical,
    reduce_log_line,
    vocabulary,
)
from tests.auth_env import run_cli
from tests.diagnostic_env import SERIES_TITLE, UNIT_TITLE, WORK_TITLE, Traps, trap_instance
from tests.integration.test_schema_catalog import add_work
from tests.tools_loader import load_tool

pytestmark = pytest.mark.req("ENF-LEG-009", "EF-BIB-041")

SHORT_TITLES = ("Bleach", "Naruto", "Dune")
SHORT_SERIES = "Monster"
SHORT_SUB_SOURCE = "lunaria"
SHORT_HOST = "lun.io"


def _slug(text: str, sep: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()
    return sep.join(plain.split())


# name -> (line, the values that must not come out)
CASES: dict[str, dict[str, object]] = {
    "title_lower_reason": {"event": "a", "reason": "bleach"},
    "title_upper_error": {"event": "b", "error": "NARUTO"},
    "title_exact_error": {"event": "b", "error": "Naruto"},
    "title_numeric_key": {"event": "c", "dune": 1},
    "series_in_logger": {"event": "d", "logger": "plugin.monster"},
    "series_in_shelfhold_logger": {"event": "d", "logger": "shelfhold.monster"},
    "subsource_lower": {"event": "e", "state": "lunaria"},
    "short_host_kind": {"event": "f", "kind": "lun.io"},
    "short_host_logger": {"event": "g", "logger": "fetch.lun.io"},
    "sealed_username": {"event": "h", "status": "svc"},
    "source_key_logger": {"event": "i", "logger": "suwayomi.9431"},
    "slug_series_kind": {"event": "j", "kind": _slug(SERIES_TITLE, "_")},
    "slug_work_code": {"event": "k", "code": _slug(WORK_TITLE, "-")[:63]},
    "hex_unit_reason": {"event": "l", "reason": "h" + UNIT_TITLE.encode().hex()},
    "b32_unit_reason": {
        "event": "m",
        "reason": "b" + base64.b32encode(UNIT_TITLE.encode()).decode().rstrip("=").lower(),
    },
    "hexupper_as_ulid": {"event": "n", "job_id": b"Chroniques du".hex().upper()},
    "json_escape_title": json.loads('{"event":"o","reason":"bl\\u0065ach"}'),
    "title_part1": {"event": "p", "state": "chroniques_du"},
    "title_part2": {"event": "q", "state": "phare_oublie"},
    "email_local_part": {"event": "r", "state": "reader"},
    "title_as_event": {"event": "bleach"},
    "title_under_known_numeric_key": {"event": "s", "duration_ms": "Dune"},
    "nested_value": {"event": "t", "outcome": {"x": "Bleach"}},
}


_FIELDS_THE_CODE_WRITES = frozenset({
    "event", "reason", "error", "logger", "state", "kind", "status", "code", "job_id",
    "duration_ms", "outcome",
})  # fmt: skip


def _injected(line: dict[str, object]) -> list[str]:
    """What a case smuggles: its string values (the one-letter event names aside), the values of
    a nested mapping, and a field name the code never writes."""
    found = [key for key in line if key not in _FIELDS_THE_CODE_WRITES]
    for value in line.values():
        if isinstance(value, str) and len(value) > 1:
            found.append(value)
        elif isinstance(value, dict):
            found.extend(str(v) for v in value.values())
    return found


@pytest.mark.parametrize("case", sorted(CASES))
def test_review_log_value_outside_the_code_vocabulary_never_comes_out(case: str) -> None:
    """The positive list is a closed vocabulary per field, read from the code: a value the code
    never writes under that field becomes `<other>`, a field it never writes is dropped."""
    line = CASES[case]
    reduced = reduce_log_line(line, Pseudonyms())
    text = json.dumps(reduced)
    for value in _injected(line):
        assert value not in text, (case, reduced)
        assert canonical(value) not in canonical(text) or len(canonical(value)) < 3, (
            case,
            reduced,
        )
    assert set(reduced) <= set(line)
    assert "dune" not in reduced  # a key the code never writes


def test_review_vocabulary_is_the_one_the_code_writes() -> None:
    """The checked-in vocabulary is exactly what `tools/diagnostic_vocabulary.py` reads from the
    code: a new event or a new field cannot widen the list without this file changing."""
    tool = load_tool("diagnostic_vocabulary")
    expected = tool.render(tool.extract())
    current = tool.OUTPUT.read_text(encoding="utf-8")
    assert current == expected, "run tools/diagnostic_vocabulary.py and review the diff"
    words = vocabulary()
    every_word = set().union(*words.words.values())
    for value in ("bleach", "naruto", "dune", "monster", "lunaria", "lun.io", "svc", "reader"):
        assert value not in every_word and value not in words.keys
    assert "NARUTO" not in words.errors and "Naruto" not in words.errors
    assert "ConnectError" in words.errors and "OSError" in words.errors
    assert all(name.startswith("shelfhold") for name in words.loggers)


@pytest.fixture
def traps(monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path) -> Traps:
    found = trap_instance(monkeypatch, settings, tmp_path)
    engine = create_db_engine(settings.database_path)
    try:
        with engine.begin() as connection:
            for title in SHORT_TITLES:
                add_work(connection, title=title, sort_title=title.lower())
            connection.execute(
                sa.insert(catalog.series).values(
                    id=new_ulid(), title=SHORT_SERIES, sort_title=SHORT_SERIES.lower()
                )
            )
            connection.execute(
                sa.insert(acquisition.sub_source).values(
                    plugin_id="suwayomi", source_key="suwayomi:77", name=SHORT_SUB_SOURCE,
                    lang="fr", enabled=True,
                )
            )  # fmt: skip
            connection.execute(
                sa.insert(acquisition.source_item).values(
                    id=new_ulid(), plugin_id="suwayomi", source_key="suwayomi:77",
                    remote_id="/m/1", url=f"https://{SHORT_HOST}/m/1",
                )
            )  # fmt: skip
    finally:
        engine.dispose()
    return found


def _run(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path, *argv: str
) -> tuple[int, Path]:
    out = tmp_path / "out" / f"b{len(list(tmp_path.glob('out/*')))}.zip"
    out.parent.mkdir(exist_ok=True)
    return run_cli(monkeypatch, settings, "diagnostic", "bundle", "--out", str(out), *argv), out


def test_review_every_leaky_line_is_replaced_before_the_barrier(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path, traps: Traps
) -> None:
    """All the review's lines in one log: the bundle is written (code 0, the positive list
    replaced everything, the barrier had nothing to stop) and none of the values is in it."""
    log = tmp_path / "review.log"
    log.write_text(
        "".join(f"shelfhold-1  | {json.dumps(line)}\n" for line in CASES.values()),
        encoding="utf-8",
    )
    code, out = _run(monkeypatch, settings, tmp_path, "--log-file", str(log))
    assert code == 0
    with zipfile.ZipFile(io.BytesIO(out.read_bytes())) as archive:
        text = archive.read("logs.jsonl").decode("utf-8")
    folded = canonical(text)
    for line in CASES.values():
        for value in _injected(line):
            assert value not in text
            assert len(canonical(value)) < 3 or canonical(value) not in folded, value
    for trap in (*SHORT_TITLES, SHORT_SERIES, SHORT_SUB_SOURCE, SHORT_HOST, "svc"):
        assert canonical(trap) not in folded.split(), trap


@pytest.mark.parametrize(
    "smuggled",
    ["Bleach", "NARUTO", "dune", "monster", "lunaria", "lun.io", "svc", "suwayomi.9431",
     _slug(SERIES_TITLE, "_"), _slug(WORK_TITLE, "-"), WORK_TITLE.upper()],
)  # fmt: skip
def test_review_barrier_sees_short_values_any_case_and_slugs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
    traps: Traps,
    smuggled: str,
) -> None:
    """Should a value ever pass the positive list, the barrier finds it whatever its length,
    case or spelling (the title columns are words; the source key and the plugin are collected;
    the user name next to a sealed password too): nothing is written."""
    real = diagnostic.reduce_status

    def leaky(report: object) -> dict[str, object]:
        reduced = dict(real(report))  # type: ignore[arg-type]
        reduced["oops"] = smuggled
        return reduced

    monkeypatch.setattr(diagnostic, "reduce_status", leaky)
    code, out = _run(monkeypatch, settings, tmp_path)
    assert code == 1 and not out.exists(), smuggled
    captured = capsys.readouterr()
    assert smuggled not in captured.err + captured.out


@pytest.mark.parametrize(
    ("added", "shown"),
    [
        ("Chroniques du Phare Oublié", "CHRONIQUES DU PHARE OUBLIÉ"),
        ("Chroniques du Phare Oublié", "chroniques-du-phare-oublie"),
        ("Chroniques du Phare Oublié", "Chroniques du Phare Oublié".encode().hex()),
        ("Bleach", "bleach"),
        ("dune", "the DUNE line"),
        ("svc", "svc"),
        ("One", "one"),
        ("lun.io", "lun_io"),
        ("alice", "Alice"),
    ],
)
def test_review_barrier_unit(added: str, shown: str) -> None:
    forbidden = _Forbidden()
    forbidden.add("t", added, word=True)
    with pytest.raises(DiagnosticLeakError):
        forbidden.check("m", shown)


def test_review_log_file_refuses_data_dir_links_and_virtual_files(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path, traps: Traps
) -> None:
    """The database, the keyring, a link to them, `/proc/self/environ`: never read."""
    link = tmp_path / "innocent.log"
    link.symlink_to(settings.database_path)
    copy_link = tmp_path / "elsewhere.log"
    real = tmp_path / "real.log"
    real.write_text('{"event": "http.call"}\n', encoding="utf-8")
    copy_link.symlink_to(real)
    for given in (
        link,
        copy_link,
        settings.database_path,
        settings.data_dir / "keyring",
        Path("/proc/self/environ"),
    ):
        code, out = _run(monkeypatch, settings, tmp_path, "--log-file", str(given))
        assert code == 2 and not out.exists(), given
    code, out = _run(monkeypatch, settings, tmp_path, "--log-file", str(real))
    assert code == 0


def test_review_log_file_bounds_and_deep_lines(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path, traps: Traps
) -> None:
    """A line nested past the parser is a clean refusal, a line past the bound is dropped, the
    number of files and the total size are bounded; no temporary file is left behind."""
    deep = tmp_path / "deep.log"
    deep.write_text('{"a":' + "[" * 50_000 + "\n", encoding="utf-8")
    code, out = _run(monkeypatch, settings, tmp_path, "--log-file", str(deep))
    assert code == 2 and not out.exists()
    huge_line = tmp_path / "huge.log"
    huge_line.write_text('{"a":' + "[" * 200_000 + "\n", encoding="utf-8")
    code, out = _run(monkeypatch, settings, tmp_path, "--log-file", str(huge_line))
    assert code == 0
    small = tmp_path / "small.log"
    small.write_text('{"event": "http.call"}\n', encoding="utf-8")
    many = ["--log-file", str(small)] * (diagnostic.MAX_LOG_FILES + 1)
    code, out = _run(monkeypatch, settings, tmp_path, *many)
    assert code == 2 and not out.exists()
    monkeypatch.setattr(diagnostic, "MAX_LOG_TOTAL_BYTES", small.stat().st_size + 1)
    code, out = _run(monkeypatch, settings, tmp_path, "--log-file", str(small),
                     "--log-file", str(small))  # fmt: skip
    assert code == 2 and not out.exists()
    assert not [p for p in (tmp_path / "out").iterdir() if p.name.startswith(".")]


def test_review_out_is_never_a_link(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path, traps: Traps
) -> None:
    target = tmp_path / "victim"
    target.write_text("keep", encoding="utf-8")
    out = tmp_path / "link.zip"
    out.symlink_to(target)
    assert run_cli(monkeypatch, settings, "diagnostic", "bundle", "--out", str(out)) == 2
    assert target.read_text(encoding="utf-8") == "keep"
    dangling = tmp_path / "dangling.zip"
    dangling.symlink_to(tmp_path / "nowhere")
    assert run_cli(monkeypatch, settings, "diagnostic", "bundle", "--out", str(dangling)) == 2
    assert not (tmp_path / "nowhere").exists()


@pytest.mark.req("EF-BIB-053")
def test_review_no_log_call_names_a_folder_or_a_path_of_a_title() -> None:
    """`folder=remote` (scan refused) quoted the folder, title included: those lines name the
    destination; no log call names a relative path or a title. (A physical root's mount path,
    set by the admin, holds no title and stays on the start-up lines.)"""
    source = Path(diagnostic.__file__).resolve().parents[1]
    levels = {"debug", "info", "warning", "error", "critical"}
    offending = []
    for path in sorted(source.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in levels
            ):
                continue
            names = {k.arg for k in node.keywords}
            first = node.args[0] if node.args else None
            event = first.value if isinstance(first, ast.Constant) else ""
            scan = str(event).endswith(".scan_refused")
            if names & {"rel_path", "title"} or (
                scan and ("folder" in names or "destination_id" not in names)
            ):
                offending.append(f"{path.relative_to(source)}:{node.lineno}")
    assert offending == []
