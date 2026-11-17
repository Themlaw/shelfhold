"""M4 L36: the adoption of a foreign library reads untrusted images and archives (EF-EXP-045,
EF-EXP-060, ENF-SEC-003).

The first page of each adopted archive is probed in the bounded decode worker, never in the core;
a page the worker refuses (too large, not an image) or an archive the opener refuses (forged,
bomb, `..`, link entry) marks the file "to check" and nothing else: the foreign library is never
touched (not a byte, a name, an mtime or an inode). The names of a plan are read in bulk by
`BulkParse.for_plan` in the plan preview. The sandbox report is a section of the diagnostic bundle.
"""

# ruff: noqa: F811  (the `world` fixture of L39 is imported, then asked for by name)

from __future__ import annotations

import io
import json
import struct
import zipfile
import zlib
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from shelfhold.export.targets.kavita.adopt import AdoptionReport, PageProbe
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.imaging import client as imaging_client
from tests.adopt_env import Adoption, adoption
from tests.export_env import export_env
from tests.integration.test_auth_keys_reserved import _bearer, _create_key
from tests.integration.test_l39_wizard import (
    World,
    _call,
    _csrf,
    _existing_series,
    world,  # noqa: F401
)
from tests.kavita_env import run
from tests.library_env import cbz_bytes
from tests.visibility_env import add_profile


def _png(size: tuple[int, int] = (64, 48)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (10, 200, 10)).save(out, "PNG")
    return out.getvalue()


def _giant_png() -> bytes:
    """A PNG whose header announces 100 000 x 100 000 pixels and carries no pixel."""

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    header = struct.pack(">IIBBBBB", 100_000, 100_000, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IEND", b"")


def _zip(entries: list[tuple[zipfile.ZipInfo | str, bytes]], *, deflate: bool = False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries:
            info = name if isinstance(name, zipfile.ZipInfo) else zipfile.ZipInfo(name)
            if not isinstance(name, zipfile.ZipInfo):
                info.date_time = (2026, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED
            archive.writestr(info, data)
    return buffer.getvalue()


def _bomb() -> bytes:
    """8 MiB of zeros in a few KiB: a ratio far beyond what a page is."""
    return _zip([("001.png", b"\x00" * (8 * 1024 * 1024))], deflate=True)


def _link_entry() -> bytes:
    link = zipfile.ZipInfo("001.png", date_time=(2026, 1, 1, 0, 0, 0))
    link.external_attr = 0o120777 << 16  # a symbolic link to a file outside the archive
    return _zip([(link, b"/etc/passwd")])


def _ops(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real = imaging_client.run_worker

    def spy(op: str, args: dict[str, Any], data: bytes, **kwargs: Any) -> Any:
        seen.append(op)
        return real(op, args, data, **kwargs)

    monkeypatch.setattr(imaging_client, "run_worker", spy)
    return seen


def _no_decoding_in_the_core(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("an image was decoded in the core")

    monkeypatch.setattr(Image, "open", refuse)


def _library(a: Adoption, files: dict[str, bytes]) -> None:
    a.add_series("Hostile", files)


def _flagged(report: Any) -> dict[str, str]:
    return {f.name: f.check for s in report.series for f in s.files if f.check}


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
def test_adoption_probes_first_page_in_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first page of each CBZ (by entry name) is read in the decode worker: its format and
    size are in the preview; the core never opens an image itself."""
    ops = _ops(monkeypatch)
    _no_decoding_in_the_core(monkeypatch)
    with export_env() as env:
        a = adoption(env)
        a.add_series(
            "Dr Stone",
            {"Dr Stone - Ch.1.cbz": cbz_bytes((("002.png", _png((8, 8))), ("001.png", _png())))},
        )
        report = run(a.adopter.preview(a.wired.client, env.destination_id))
    (only,) = report.series[0].files
    assert only.first_page == PageProbe("png", 64, 48)  # 001 comes before 002
    assert only.check == "" and report.to_check == ()
    assert ops == ["probe"]  # one bounded worker run for the one file


@pytest.mark.req("EF-EXP-045", "EF-EXP-060", "ENF-SEC-003")
def test_refused_page_marks_file_to_check_without_touching_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Giant image, bomb, forged archive, `..` entry, link entry, a page that is no image: each
    marks its file and adoption goes on. Not one byte of the library moves."""
    _no_decoding_in_the_core(monkeypatch)
    hostile = {
        "Fine - Ch.1.cbz": cbz_bytes((("001.png", _png()),)),
        "Giant - Ch.2.cbz": cbz_bytes((("001.png", _giant_png()),)),
        "Bomb - Ch.3.cbz": _bomb(),
        "Dotdot - Ch.4.cbz": _zip([("../../evil.png", _png())]),
        "Link - Ch.5.cbz": _link_entry(),
        "Garbage - Ch.6.cbz": cbz_bytes((("001.png", b"not an image at all"),)),
        "Forged - Ch.7.cbz": b"PK\x03\x04" + b"\xff" * 64,
    }
    with export_env() as env:
        a = adoption(env)
        _library(a, hostile)
        before = a.state()
        preview = run(a.adopter.preview(a.wired.client, env.destination_id))
        done = a.adopt()
        assert isinstance(done, AdoptionReport)
        after = a.state()
    assert after == before  # not a byte, a name, an mtime or an inode
    flagged = _flagged(preview)
    assert set(flagged) == set(hostile) - {"Fine - Ch.1.cbz"}
    assert flagged["Giant - Ch.2.cbz"] == "image.too_large"
    assert flagged["Garbage - Ch.6.cbz"] == "image.format_refused"
    for name in ("Bomb - Ch.3.cbz", "Dotdot - Ch.4.cbz", "Link - Ch.5.cbz", "Forged - Ch.7.cbz"):
        assert flagged[name].startswith("archive."), (name, flagged[name])
    assert set(preview.to_check) == {f"Hostile/{name}" for name in flagged}
    assert done.adopted_files == len(hostile)  # marked, not refused: the admin decides
    assert {f.name for s in done.series for f in s.files if f.check} == set(flagged)


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
def test_a_linked_file_is_never_read_for_its_page(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A symbolic link in the library is described, not followed: its target is not probed, and
    it is not adopted."""
    ops = _ops(monkeypatch)
    outside = tmp_path / "outside.cbz"
    outside.write_bytes(cbz_bytes((("001.png", _png()),)))
    with export_env() as env:
        a = adoption(env)
        a.add_series("Real", {"Real - Ch.1.cbz": cbz_bytes((("001.png", _png()),))})
        (env.root / "Real" / "Link - Ch.2.cbz").symlink_to(outside)
        a.wired.fake.files[a.series["Real"]].append("/manga/Kavita/Real/Link - Ch.2.cbz")
        report = run(a.adopter.preview(a.wired.client, env.destination_id))
    assert [f.name for s in report.series for f in s.files] == ["Real - Ch.1.cbz"]
    assert ops == ["probe"]  # the real file only


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
def test_page_probe_runs_in_the_bounded_worker_and_is_not_repeated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caps are those of `page_input`; a file that did not change is not probed again by the
    adoption that follows its preview (same inode, size, mtime)."""
    ops = _ops(monkeypatch)
    caps: list[dict[str, Any]] = []
    real = imaging_client.run_worker

    def spy(op: str, args: dict[str, Any], data: bytes, **kwargs: Any) -> Any:
        caps.append({"limits": kwargs.get("limits"), "caps": args.get("caps")})
        return real(op, args, data, **kwargs)

    monkeypatch.setattr(imaging_client, "run_worker", spy)
    with export_env() as env:
        a = adoption(env)
        a.add_series("S", {"S - Ch.1.cbz": cbz_bytes((("001.png", _png()),))})
        a.adopt()
    assert len(caps) == 1 and ops == ["probe"]  # one run: the preview probes, the adoption does not
    limits = caps[0]["limits"]
    assert limits is not None and limits.wall_seconds <= 60 and limits.network_namespace
    assert caps[0]["caps"]["max_pixels"] <= 89_478_485


@pytest.mark.req("EF-EXP-045")
def test_adoption_bulk_parse_for_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """The preview of a plan reads its names through `BulkParse.for_plan` (production calls it):
    they go through `parse-bulk`, then the plan can be applied (EF-EXP-045)."""
    from types import SimpleNamespace

    import shelfhold.runtime_jobs as rj
    from shelfhold.export.targets.kavita import adopt as adopt_module

    asked: list[str] = []
    real = adopt_module.BulkParse.for_plan.__func__  # type: ignore[attr-defined]

    async def spy(cls: Any, client: Any, pipeline: Any, destination_id: str) -> Any:
        asked.append(destination_id)
        return await real(cls, client, pipeline, destination_id)

    async def nothing(*_: object, **__: object) -> int:
        return 0

    monkeypatch.setattr(adopt_module.BulkParse, "for_plan", classmethod(spy))
    monkeypatch.setattr(rj, "_names_may_refresh", lambda *_: False)
    monkeypatch.setattr(rj, "refresh_foreign_names", nothing)
    with export_env() as env:
        a = adoption(env)
        a.add_series("S", {"S - Ch.1.cbz": cbz_bytes((("001.png", _png()),))})
        a.adopt()
        monkeypatch.setattr(rj, "_destination_of", lambda *_: SimpleNamespace(target_id="t"))
        comp = SimpleNamespace(engine=env.engine, kavita_client=lambda _target: a.wired.client)
        service = rj.PlanService(comp, lambda: env.pipeline, lambda: None)  # type: ignore[arg-type]
        service.preview(env.destination_id)
    assert asked == [env.destination_id]


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
def test_probe_and_diagnose_worker_have_a_production_caller() -> None:
    """The two symbols left the reachability list: each has a caller outside the tests."""
    import tomllib

    allowlist = Path(__file__).resolve().parents[3] / "tools/production-reachability-allowlist.toml"
    text = json.dumps(tomllib.loads(allowlist.read_text("utf-8")))
    assert "imaging.client:probe_image" not in text
    assert "imaging.client:diagnose_worker" not in text
    assert "kavita.adopt:BulkParse.for_plan" not in text


# -- the screen: the admin's preview carries the files to check, nobody else gets it -------------

PREVIEW = "/api/v1/setup/adoption/preview"


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
def test_adoption_preview_names_the_files_to_check_and_touches_nothing(world: World) -> None:
    """The page of the existing series is no image: the admin's preview says which file and why
    (a code of the closed list), and the library is as it was."""
    _existing_series(world)
    before = world.tree()
    seen = world.post(PREVIEW)
    assert seen.status_code == 200, seen.text
    assert seen.json()["to_check"] == [
        {"path": "Dr Stone/Dr Stone - Ch.1.cbz", "reason": "image.format_refused"}
    ]
    assert world.tree() == before and world.dest() == {"adopted": False, "links": 0}


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003", "EF-USR-024")
def test_files_to_check_are_never_shown_to_a_member_a_child_or_a_key(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """IDOR on the preview of another's library: a member, a child, an API key (even an admin
    one) and a stale session each get a refusal with no trace of a path or a reason."""
    _existing_series(world)
    for role, ip in (("member", "10.0.0.5"), ("child", "10.0.0.6")):
        client = add_profile(
            world.api, role, role, ip, **({"max_age_rating": 10} if role == "child" else {})
        )
        answer = _call(client, "POST", PREVIEW, {"destination_id": world.destination_id},
                       _csrf(world, client))  # fmt: skip
        assert answer.status_code == 403 and "to_check" not in answer.text
        assert "Dr Stone" not in answer.text
    key = _create_key(monkeypatch, capsys, world.api.settings)
    answer = _call(world.api.other_client("10.0.0.9"), "POST", PREVIEW,
                   {"destination_id": world.destination_id}, _bearer(key))  # fmt: skip
    assert answer.status_code in {401, 403} and "to_check" not in answer.text
    assert "Dr Stone" not in answer.text


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003", "ENF-LEG-009")
def test_the_file_to_check_is_logged_without_its_name(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """A journal line says that a file is to check and why, never its name or its series."""
    from shelfhold.infrastructure.logging import configure_logging

    configure_logging(level="DEBUG", log_format="json")
    _existing_series(world)
    assert world.post(PREVIEW).status_code == 200
    out = capsys.readouterr()
    text = out.out + out.err
    assert "adopt.file_to_check" in text and "image.format_refused" in text
    assert "Dr Stone" not in text and "Ch.1" not in text


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
def test_a_page_past_the_cap_or_an_unreadable_file_is_to_check_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first page larger than what the core reads in memory is a giant image; a file that
    cannot be opened is `archive.unreadable`. Neither stops the adoption."""
    from shelfhold.export.targets.kavita import adopt as adopt_module

    monkeypatch.setattr(adopt_module, "PROBE_MAX_BYTES", 1024)
    with export_env() as env:
        a = adoption(env)
        a.add_series(
            "S",
            {
                "Big - Ch.1.cbz": cbz_bytes((("001.png", _png((256, 256))),)),
                "Small - Ch.2.cbz": cbz_bytes((("001.png", b"x" * 100),)),
            },
        )
        real = Fsops.open_read

        def broken(self: Any, local: str) -> Any:
            if "Small" in local:
                raise PermissionError(local)
            return real(self, local)

        monkeypatch.setattr(Fsops, "open_read", broken)
        report = run(a.adopter.preview(a.wired.client, env.destination_id))
    assert _flagged(report) == {
        "Big - Ch.1.cbz": "image.too_large",
        "Small - Ch.2.cbz": "archive.unreadable",
    }
