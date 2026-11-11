"""`shelfhold diagnostic bundle --out <file>`: the minimal diagnostic bundle (M2 L30).

The bundle is closed by default: a positive list says what enters it (configuration without
secret, the codes and counts of `status`, the schema, a hash-and-size inventory of the contents,
the sources under pseudonyms, the log lines it was given, reduced to their allowed fields). A
watermarked file never enters it (EF-BIB-041); domains, `manga.url` and sub-source names become
`<source-N>` (ENF-LEG-009, GAP-6-22); no secret, title, address or user identifier survives.
"""

from __future__ import annotations

import io
import json
import stat
import zipfile
from pathlib import Path

import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.logging import configure_logging
from tests.auth_env import run_cli
from tests.diagnostic_env import (
    SUB_SOURCE,
    UNIT_TITLE,
    WORK_TITLE,
    Traps,
    leaks,
    log_file,
    trap_instance,
)
from tests.fsops_env import make_env

pytestmark = pytest.mark.req("ENF-LEG-009", "EF-BIB-041")


def _bundle(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path, *logs: Path
) -> tuple[int, Path]:
    out = tmp_path / "out" / "bundle.zip"
    out.parent.mkdir(exist_ok=True)
    argv = ["diagnostic", "bundle", "--out", str(out)]
    for log in logs:
        argv += ["--log-file", str(log)]
    return run_cli(monkeypatch, settings, *argv), out


def _members(out: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(out.read_bytes())) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


@pytest.fixture
def traps(monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path) -> Traps:
    return trap_instance(monkeypatch, settings, tmp_path)


def test_watermarked_never_in_diagnostic_bundle(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path, traps: Traps
) -> None:
    """Given a bought EPUB marked `watermarked`, when a bundle is made, then the file is not in
    it and only its hash and its size are cited (EF-BIB-041)."""
    code, out = _bundle(monkeypatch, settings, tmp_path, log_file(tmp_path / "s.log", traps))
    assert code == 0
    found = leaks(out.read_bytes(), traps)
    assert "watermarked_file" not in found and "watermark" not in found, found
    members = _members(out)
    assert not any(name.endswith((".epub", ".cbz", ".pdf")) for name in members)
    inventory = json.loads(members["content.json"])
    assert inventory["watermarked"] == [
        {"hash": traps.watermarked_sha256, "size": traps.watermarked_size}
    ]


def test_watermarked_file_given_as_log_input_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
    traps: Traps,
) -> None:
    """A content file, whatever its name, never enters through `--log-file`: neither the
    watermarked EPUB kept in /store, nor a copy of it elsewhere, nor the provenance mirror."""
    copy = tmp_path / "shelfhold.log"
    copy.write_bytes(traps.watermarked_bytes)
    mirror = settings.store_dir.parent / "lib" / ".shelfhold" / "manifest" / "a.jsonl"
    mirror.parent.mkdir(parents=True)
    mirror.write_text('{"event": "x"}\n', encoding="utf-8")
    kept = settings.store_dir / WORK_TITLE / f"{UNIT_TITLE}.epub"
    for given in (copy, mirror, kept):
        code, out = _bundle(monkeypatch, settings, tmp_path, given)
        assert code == 2, given
        assert not out.exists()
        assert WORK_TITLE not in capsys.readouterr().err


def test_diagnostic_bundle_redacts_source_urls(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path, traps: Traps
) -> None:
    """Domains, `manga.url` and sub-source names are replaced by `<source-N>` (ENF-LEG-009)."""
    code, out = _bundle(monkeypatch, settings, tmp_path, log_file(tmp_path / "s.log", traps))
    assert code == 0
    found = leaks(out.read_bytes(), traps)
    for category in ("source_url", "source_host", "sub_source", "service_ip", "service_host"):
        assert category not in found, found
    sources = json.loads(_members(out)["sources.json"])
    assert sources["sub_sources"] == [
        {"source": "<source-1>", "plugin": "suwayomi", "lang": "fr", "enabled": True}
    ]
    assert sources["items"] == {"<source-1>": 1}
    logs = [json.loads(line) for line in _members(out)["logs.jsonl"].splitlines()]
    looked = next(line for line in logs if line["event"] == "follow.series_looked")
    assert looked["outcome"] == "ok" and looked["series_id"] == traps.ids["work"]
    assert "source" not in looked or looked["source"] == "<source-1>"
    assert looked["user_id"] == "<user-1>"  # a user identifier is never written in clear


def test_diagnostic_bundle_contains_no_secret(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path, traps: Traps
) -> None:
    """Settings secrets, sealed secrets, the keyring: none of them, in no form."""
    code, out = _bundle(monkeypatch, settings, tmp_path, log_file(tmp_path / "s.log", traps))
    assert code == 0
    found = leaks(out.read_bytes(), traps)
    for category in ("api_token", "wake_token", "source_password", "email", "user_id"):
        assert category not in found, found
    keyring = (settings.data_dir / "keyring").read_bytes()
    assert keyring not in out.read_bytes()
    config = json.loads(_members(out)["config.json"])
    present = {name for name, value in config["values"].items() if value == "set"}
    assert {"api_token", "wake_token"} <= present  # said to be set, never shown
    assert config["values"]["public_url"] == "set" and config["values"]["host"] == "loopback"
    assert set(_members(out)) == {
        "manifest.json", "config.json", "status.json", "schema.sql", "content.json",
        "sources.json", "logs.jsonl",
    }  # fmt: skip


def test_diagnostic_bundle_is_owner_only_and_never_overwrites(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path, traps: Traps
) -> None:
    code, out = _bundle(monkeypatch, settings, tmp_path)
    assert code == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    before = out.read_bytes()
    code, _ = _bundle(monkeypatch, settings, tmp_path)
    assert code == 2 and out.read_bytes() == before
    assert "logs.jsonl" not in _members(out)  # no log file given: no log member


def test_diagnostic_bundle_refused_when_a_trap_value_survives(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
    traps: Traps,
) -> None:
    """The last barrier: should a section ever carry a title, a sub-source name or a secret
    (a bug of the positive list), nothing is written, and the error names the category only."""
    from shelfhold.ops import diagnostic

    real = diagnostic.reduce_status

    def leaky(report: object) -> dict[str, object]:
        reduced = dict(real(report))  # type: ignore[arg-type]
        reduced["oops"] = f"seen {SUB_SOURCE}"
        return reduced

    monkeypatch.setattr(diagnostic, "reduce_status", leaky)
    code, out = _bundle(monkeypatch, settings, tmp_path)
    assert code == 1 and not out.exists()
    assert not list(out.parent.iterdir())  # no temporary file left behind
    captured = capsys.readouterr()
    assert SUB_SOURCE not in captured.err + captured.out


@pytest.mark.req("EF-BIB-053")
def test_fsops_debug_lines_carry_the_root_never_the_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Debt of M1a: `fsops.*` lines quoted the path, title included. They name the root (ULID)."""
    env = make_env(tmp_path)
    fsops: Fsops = env.fsops
    configure_logging(level="DEBUG", log_format="json")
    capsys.readouterr()
    folder = env.manga / WORK_TITLE
    fsops.mkdir(str(folder))
    fsops.write_atomic(str(folder / f"{UNIT_TITLE}.cbz"), b"PK", parents=True)
    fsops.unlink(str(folder / f"{UNIT_TITLE}.cbz"))
    fsops.rmdir(str(folder))
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    written = [x for x in lines if str(x.get("event", "")).startswith("fsops.")]
    assert {x["event"] for x in written} >= {
        "fsops.mkdir", "fsops.write_atomic", "fsops.unlink", "fsops.rmdir"
    }  # fmt: skip
    assert all(x.get("root_id") == env.expected[0].root_id for x in written), written
    text = json.dumps(lines, ensure_ascii=False)
    assert WORK_TITLE not in text and UNIT_TITLE not in text
