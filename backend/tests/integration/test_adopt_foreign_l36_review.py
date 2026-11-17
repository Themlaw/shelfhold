# ruff: noqa: F811  (the `world` fixture of L39 is imported, then asked for by name)
"""#341 (M4 L36), the answers to the adverse review: verdicts and the bound on probe workers are
the process's, a page is read inside its slot and never copied, a preview is a background task,
the sandbox says what it really shuts (inet, Unix sockets), the codes are the right ones."""

from __future__ import annotations

import contextlib
import threading
import time
import tracemalloc
from collections.abc import Iterator
from typing import Any

import pytest

from shelfhold.export.targets.kavita import adopt as adopt_module
from shelfhold.export.targets.kavita import page_probe
from shelfhold.export.targets.kavita.adopt import Adopter
from shelfhold.infrastructure.archives.safe_open import UnsafeArchiveError
from shelfhold.infrastructure.imaging import ImageRejected
from shelfhold.infrastructure.imaging import client as imaging_client
from shelfhold.infrastructure.imaging.protocol import encode_frame
from shelfhold.ops import wizard as wizard_module
from tests.adopt_env import adoption
from tests.export_env import export_env
from tests.integration.test_adopt_foreign_l36 import PREVIEW, _flagged, _ops, _png, _stored_cbz
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

CONFIRM = "/api/v1/setup/adoption/confirm"


@pytest.fixture(autouse=True)
def _fresh_process_state() -> Iterator[None]:
    page_probe.clear()
    yield
    page_probe.configure(page_probe.DEFAULT_WORKERS)
    page_probe.clear()


def _second_adopter(a: Any) -> Adopter:
    """What the screen does for its next request: a new `Adopter` over the same instance."""
    old = a.adopter
    return Adopter(
        old._engine, old._fsops, locate=old._locate, clock=old._clock,
        auditor=old._auditor, tombstones=old._tombstones,
    )  # fmt: skip


def _library(a: Any, count: int) -> None:
    files = {
        f"S - Ch.{n}.cbz": cbz_bytes((("001.png", _png((8 + n, 8))),)) for n in range(1, count + 1)
    }
    a.add_series("S", files)


# -- M1: the confirmation does not probe again, whoever builds the Adopter --


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
def test_preview_then_confirmation_probe_each_file_once_across_two_adopters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ops = _ops(monkeypatch)
    with export_env() as env:
        a = adoption(env)
        _library(a, 3)
        seen = run(a.adopter.preview(a.wired.client, env.destination_id))
        second = _second_adopter(a)
        assert second is not a.adopter
        done = run(second.adopt(a.wired.client, env.destination_id, confirm=seen.fingerprint))
    assert done.adopted_files == 3
    assert ops == ["probe"] * 3  # one probe per file, in all


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
def test_the_verdict_cache_is_bounded_and_least_recently_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(page_probe, "CACHE_MAX", 2)
    for name in ("a", "b"):
        page_probe.remember(name, (1,), (None, name))
    assert page_probe.lookup("a", (1,)) is not None  # a is used: b is the oldest
    page_probe.remember("c", (1,), (None, "c"))
    assert page_probe.lookup("b", (1,)) is None
    assert page_probe.lookup("a", (1,)) is not None and page_probe.lookup("c", (1,)) is not None
    assert page_probe.lookup("a", (2,)) is None  # another ctime, device or inode: another file


# -- the screen: a preview is a background task --


def _poll(world: World, url: str) -> dict[str, Any]:
    for _ in range(100):
        answer = world.api.client.get(url)
        assert answer.status_code == 200, answer.text
        if answer.json()["status"] == "done":
            return dict(answer.json())
        time.sleep(0.1)
    raise AssertionError("the preview never ended")


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
def test_a_long_preview_is_a_task_with_a_status_and_a_report(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _existing_series(world)
    monkeypatch.setattr(wizard_module, "PREVIEW_WAIT_S", 0.0)
    started = world.post(PREVIEW)
    assert started.status_code == 202, started.text
    job = started.json()
    assert (
        started.headers["Location"]
        == job["status_url"]
        == (f"/api/v1/setup/adoption/preview/{job['job_id']}")
    )
    done = _poll(world, job["status_url"])
    assert done["preview"]["files"] == 1 and done["preview"]["to_check"]
    ops = _ops(monkeypatch)  # the confirmation on that report probes nothing again
    adopted = world.post(CONFIRM, {"fingerprint": done["preview"]["fingerprint"]})
    assert adopted.status_code == 200, adopted.text
    assert ops == []


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
def test_the_same_preview_asked_twice_while_it_runs_is_one_task(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _existing_series(world)
    release = threading.Event()
    real = wizard_module.WizardOps.adoption_preview
    calls: list[str] = []

    def slow(self: Any, destination_id: str, **kwargs: Any) -> Any:
        calls.append(destination_id)
        release.wait(20)
        return real(self, destination_id, **kwargs)

    monkeypatch.setattr(wizard_module.WizardOps, "adoption_preview", slow)
    monkeypatch.setattr(wizard_module, "PREVIEW_WAIT_S", 0.0)
    first, second = world.post(PREVIEW), world.post(PREVIEW)
    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    running = world.api.client.get(first.json()["status_url"]).json()
    assert running["status"] == "running" and running["preview"] is None
    release.set()
    _poll(world, first.json()["status_url"])
    assert len(calls) == 1


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
def test_a_preview_that_fails_says_its_code_when_asked(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shelfhold.ops.errors import InvalidChoiceError

    def refuse(self: Any, destination_id: str, **kwargs: Any) -> Any:
        raise InvalidChoiceError("this destination is not linked to a Kavita library")

    monkeypatch.setattr(wizard_module.WizardOps, "adoption_preview", refuse)
    monkeypatch.setattr(wizard_module, "PREVIEW_WAIT_S", 0.0)
    started = world.post(PREVIEW)
    answer = started
    if started.status_code == 202:  # the task may also have ended before the answer was made
        for _ in range(100):
            answer = world.api.client.get(started.json()["status_url"])
            if answer.status_code != 200:
                break
            time.sleep(0.1)
    assert answer.status_code in {400, 404, 409, 422}, answer.text
    assert "code" in answer.json()  # a code of the catalog, nothing of the library


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003", "EF-USR-024")
def test_a_preview_task_is_the_admin_s_only(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """IDOR: its id read by a member, a child, an API key (even an admin one), a stranger without
    a session, or an unknown id: no report, no path."""
    _existing_series(world)
    monkeypatch.setattr(wizard_module, "PREVIEW_WAIT_S", 0.0)
    job = world.post(PREVIEW).json()
    _poll(world, job["status_url"])
    for role, ip in (("member", "10.0.0.5"), ("child", "10.0.0.6")):
        client = add_profile(
            world.api, role, role, ip, **({"max_age_rating": 10} if role == "child" else {})
        )
        answer = _call(client, "GET", job["status_url"], None, _csrf(world, client))
        assert answer.status_code == 403 and "Dr Stone" not in answer.text
    key = _create_key(monkeypatch, capsys, world.api.settings)
    stranger = world.api.other_client("10.0.0.9")
    answer = _call(stranger, "GET", job["status_url"], None, _bearer(key))
    assert answer.status_code in {401, 403} and "Dr Stone" not in answer.text
    anonymous = _call(world.api.other_client("10.0.0.8"), "GET", job["status_url"], None)
    assert anonymous.status_code == 401
    missing = world.api.client.get("/api/v1/setup/adoption/preview/01ARZ3NDEKTSV4RRFFQ69G5FAV")
    assert missing.status_code == 404


# -- m1: what a verdict is kept under --


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
def test_a_file_swapped_between_the_look_and_the_read_is_not_given_the_other_s_verdict() -> None:
    with export_env() as env:
        a = adoption(env)
        a.add_series("S", {"S - Ch.1.cbz": cbz_bytes((("001.png", _png()),))})
        report = run(a.adopter.preview(a.wired.client, env.destination_id))
        (file,) = (f for s in report.series for f in s.files)
        stale = (*file.stamp[:-1], file.stamp[-1] + 1)  # the ctime the look saw is not the file's
        assert adopt_module._first_page(env.lib.fsops, file.local_path, stale) == "archive.changed"
        assert adopt_module._first_page(env.lib.fsops, file.local_path, file.stamp) is not None


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
def test_a_page_rewritten_with_its_mtime_put_back_is_probed_again_and_confirmation_refused() -> (
    None
):
    """The contradicteur's PoC: same path, size, inode and mtime (`utime`), only the ctime moved.
    The cached verdict is not reused, and the fingerprint the admin looked at no longer holds:
    stronger than adopting with a flag, the adoption is refused as stale."""
    import os

    from shelfhold.export.targets.kavita.adopt import PreviewStale

    clean = cbz_bytes((("001.png", _png()),))
    with export_env() as env:
        a = adoption(env)
        a.add_series("S", {"S - Ch.1.cbz": clean})
        seen = run(a.adopter.preview(a.wired.client, env.destination_id))
        assert _flagged(seen) == {}
        (file,) = (f for s in seen.series for f in s.files)
        before = os.stat(file.local_path)
        with open(file.local_path, "r+b") as stream:
            stream.seek(clean.index(b"\x89PNG"))
            stream.write(b"X" * 16)
        os.utime(file.local_path, ns=(before.st_atime_ns, before.st_mtime_ns))
        again = run(a.adopter.preview(a.wired.client, env.destination_id))
        assert _flagged(again) != {} and again.fingerprint != seen.fingerprint
        with pytest.raises(PreviewStale):
            run(a.adopter.adopt(a.wired.client, env.destination_id, confirm=seen.fingerprint))


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
def test_the_confirmation_fingerprint_binds_the_ctime() -> None:
    import os

    with export_env() as env:
        a = adoption(env)
        a.add_series("S", {"S - Ch.1.cbz": cbz_bytes((("001.png", _png()),))})
        seen = run(a.adopter.preview(a.wired.client, env.destination_id))
        (file,) = (f for s in seen.series for f in s.files)
        before = os.stat(file.local_path)
        os.chmod(file.local_path, 0o640)  # ctime moves, the mtime does not
        os.utime(file.local_path, ns=(before.st_atime_ns, before.st_mtime_ns))
        again = run(a.adopter.preview(a.wired.client, env.destination_id))
    assert again.fingerprint != seen.fingerprint


# -- M2: nothing copied, a page per slot --


@pytest.mark.req("EF-EXP-045", "ENF-PERF-001")
def test_a_preview_of_big_pages_stays_far_under_the_peak_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = threading.Barrier(page_probe.workers(), timeout=5)

    def worker(op: str, args: dict[str, Any], data: bytes, **_: Any) -> Any:
        frame = encode_frame({"op": op, "args": args}, data)  # what the client really builds
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait()
        del frame
        raise ImageRejected("image.decode_failed", "stand-in", "decode")

    monkeypatch.setattr(imaging_client, "run_worker", worker)
    # a page under the 32 MiB cap
    archive = _stored_cbz(b"\x89PNG\r\n\x1a\n" + b"\x00" * (30 * 1024 * 1024))
    with export_env() as env:
        a = adoption(env)
        a.add_series("Big", {f"Big - Ch.{n}.cbz": archive for n in range(1, 5)})
        del archive
        tracemalloc.start()
        try:
            base = tracemalloc.get_traced_memory()[0]
            run(a.adopter.preview(a.wired.client, env.destination_id))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
    assert peak - base < 200_000_000, f"{(peak - base) / 1e6:.0f} MB above the base"


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
def test_a_page_past_32_mib_is_a_giant_image_and_never_reaches_the_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ops = _ops(monkeypatch)
    archive = _stored_cbz(b"\x89PNG\r\n\x1a\n" + b"\x00" * (33 * 1024 * 1024))
    with export_env() as env:
        a = adoption(env)
        a.add_series("Big", {"Big - Ch.1.cbz": archive})
        report = run(a.adopter.preview(a.wired.client, env.destination_id))
    assert _flagged(report) == {"Big - Ch.1.cbz": "image.too_large"} and ops == []


# -- M3: one bound for the whole process --


def _counting_worker(running: list[int], lock: threading.Lock) -> Any:
    def worker(op: str, args: dict[str, Any], data: bytes, **_: Any) -> Any:
        with lock:
            running[0] += 1
            running[1] = max(running[0], running[1])
        time.sleep(0.25)
        with lock:
            running[0] -= 1
        return {"format": "PNG", "width": 1, "height": 1}, b""

    return worker


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
@pytest.mark.parametrize("workers", [1, 2, 3])
def test_two_simultaneous_previews_never_open_more_workers_than_configured(
    monkeypatch: pytest.MonkeyPatch, workers: int
) -> None:
    running, lock = [0, 0], threading.Lock()
    with export_env() as env:
        a = adoption(env)
        _library(a, 6)
        files = [
            f
            for s in run(a.adopter.preview(a.wired.client, env.destination_id)).series
            for f in s.files
        ]
        page_probe.clear()
        page_probe.configure(workers)
        monkeypatch.setattr(imaging_client, "run_worker", _counting_worker(running, lock))
        threads = [
            threading.Thread(target=x._with_pages, args=(list(files),))
            for x in (a.adopter, _second_adopter(a))
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert 1 <= running[1] <= workers, running


# -- m2, m4, m5 --


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
def test_a_worker_that_cannot_start_is_the_workers_failure_whatever_the_layer() -> None:
    import subprocess

    original = subprocess.Popen

    def refuse(*_a: object, **_k: object) -> Any:
        raise OSError(11, "Resource temporarily unavailable")

    subprocess.Popen = refuse  # type: ignore[misc,assignment]
    try:
        with pytest.raises(ImageRejected) as caught:
            imaging_client.run_worker("probe", {}, b"")
    finally:
        subprocess.Popen = original  # type: ignore[misc]
    assert caught.value.code == "image.worker_failed"


@pytest.mark.req("EF-EXP-045", "ENF-SEC-003")
def test_a_big_entry_that_is_not_a_page_is_not_called_a_giant_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_a: object, **_k: object) -> Any:
        raise UnsafeArchiveError("entry_too_large", "129 MiB", entry="'video.mp4'")

    monkeypatch.setattr(adopt_module, "safe_open_archive", refuse)
    with export_env() as env:
        a = adoption(env)
        a.add_series("S", {"S - Ch.1.cbz": cbz_bytes((("001.png", _png()),))})
        report = run(a.adopter.preview(a.wired.client, env.destination_id))
    assert _flagged(report) == {"S - Ch.1.cbz": "archive.entry_too_large"}


@pytest.mark.req("EF-EXP-045")
def test_the_screen_says_that_comic_rar_seven_zip_and_pdf_are_not_probed() -> None:
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "frontend/src"
    page = (root / "routes/reglages/adoption/+page.svelte").read_text("utf-8")
    assert "adoption.not_probed" in page
    for language in ("fr", "en"):
        catalog = json.loads((root / f"lib/i18n/{language}.json").read_text("utf-8"))
        text = catalog["adoption.not_probed"]
        assert all(word in text for word in ("CBR", "CB7", "PDF"))
    assert set(adopt_module.NOT_PROBED) >= {".cbr", ".cb7", ".pdf"}
