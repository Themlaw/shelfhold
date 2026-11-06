"""L59: detailed multi-level logs, through the real paths.

The follow handler, the export pipeline and the Huey handlers registered by the runtime are the
production ones; only Kavita and Suwayomi are the simulated servers. Every test reads the whole
output of the process logger (JSON lines), never a mock of the logger.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
import structlog
from huey.exceptions import RetryTask

import shelfhold.runtime_jobs as rj
from shelfhold.config import Settings
from shelfhold.infrastructure.fetcher.client import GuardedFetcher
from shelfhold.infrastructure.fetcher.errors import InvalidFetchUrl
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.logging import (
    configure_logging,
    forget_secret_values,
    route_of,
    untrusted_name,
)
from shelfhold.jobs.huey_app import JobPools
from tests.fakes.kavita import API_KEY
from tests.fakes.suwayomi.server import PASSWORD, USER
from tests.fakes_clock import ManualClock
from tests.fakes_fetcher import FakeResolver, Recorder
from tests.follow_env import new_chapter
from tests.l41_env import KAVITA_PASSWORD
from tests.l50_env import Seeded, seeded

SUWAYOMI_SECRET = f"{USER}:{PASSWORD}"
WINDOW_NIGHT = datetime(2026, 11, 6, 2, 30, tzinfo=UTC)  # 03:30 in Paris: the window is open


@dataclass
class Logs:
    capsys: pytest.CaptureFixture[str]
    lines: list[str]

    def start(self, level: str) -> None:
        configure_logging(level=level, log_format="json")  # type: ignore[arg-type]
        self.capsys.readouterr()

    def drain(self) -> list[dict[str, object]]:
        out = self.capsys.readouterr().out
        self.lines.append(out)
        return [json.loads(line) for line in out.splitlines() if line.startswith("{")]

    def everything(self) -> str:
        self.drain()
        return "".join(self.lines)


@pytest.fixture
def logs(capsys: pytest.CaptureFixture[str]) -> Iterator[Logs]:
    yield Logs(capsys, [])
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_shelfhold_handler", False)]:
        root.removeHandler(handler)
    root.setLevel(logging.WARNING)
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    forget_secret_values()


def named(records: list[dict[str, object]], event: str) -> list[dict[str, object]]:
    return [r for r in records if r["event"] == event]


def run_job(pools: JobPools, kind: str) -> str:
    created = pools.submit(kind, dedupe_key=f"{kind}-{new_ulid()}")
    with contextlib.suppress(RetryTask):
        pools._execute(created.job_id)
    return created.job_id


def lifecycle(env: Seeded, logs: Logs, *, level: str) -> list[dict[str, object]]:
    """Follow cycles (discovery, a new chapter received) then an export, over the real jobs."""
    env.inst.admin.source_add_suwayomi(url="http://suwayomi.lan:4567", secret=SUWAYOMI_SECRET)
    pools = JobPools(
        env.inst.settings.data_dir, engine=env.inst.engine, clock=env.inst.comp.clock, seed="l59"
    )
    rj.register_runtime_jobs(pools, env.inst.comp)
    logs.start(level)
    run_job(pools, "follow.cycle")  # the baseline: what Suwayomi holds becomes the watermark
    alpha = env.inst.suwayomi.state["mangas"][0]
    alpha["chapters"].append(new_chapter(5030, 3))
    for _ in range(4):  # request, poll, fetch: the simulated server needs a few passes
        run_job(pools, "follow.cycle")
    run_job(pools, "export.run")
    return logs.drain()


# -- the events expected at each level, by the real paths ------------


@pytest.mark.req("ENF-OBS-001")
def test_debug_follow_then_export_emit_the_expected_events(
    settings: Settings, tmp_path: Path, logs: Logs
) -> None:
    with seeded(settings, tmp_path) as env:
        records = lifecycle(env, logs, level="DEBUG")
    events = {str(r["event"]) for r in records}
    for expected in (
        "job.started", "job.finished", "job.dispatched",
        "follow.library_listed", "follow.series_looked", "follow.unit_seen",
        "follow.ticket_requested", "follow.chapter_fetched", "follow.cycle_summary",
        "http.call", "kavita.call",
        "export.plan_computed", "export.plan_op", "export.op_done", "export.applied",
        "fsops.write_atomic",
    ):  # fmt: skip
        assert expected in events, expected
    summary = next(r for r in named(records, "follow.cycle_summary") if r["fetched"])
    assert summary["level"] == "info"  # a cycle that brought a chapter is told at INFO
    finished = named(records, "job.finished")
    assert all("duration_ms" in r and "outcome" in r for r in finished)
    assert {r["job_kind"] for r in finished} >= {"follow.cycle", "export.run"}


@pytest.mark.req("ENF-OBS-001")
def test_info_level_has_no_debug_line(settings: Settings, tmp_path: Path, logs: Logs) -> None:
    with seeded(settings, tmp_path) as env:
        records = lifecycle(env, logs, level="INFO")
    assert records
    assert {r["level"] for r in records} <= {"info", "warning", "error", "critical"}
    events = {str(r["event"]) for r in records}
    assert {"job.started", "job.finished", "export.plan_computed", "export.applied"} <= events
    assert "http.call" not in events and "fsops.write_atomic" not in events


@pytest.mark.req("ENF-OBS-001")
def test_context_is_bound_on_every_line_of_a_scope(
    settings: Settings, tmp_path: Path, logs: Logs
) -> None:
    with seeded(settings, tmp_path) as env:
        records = lifecycle(env, logs, level="DEBUG")
    scoped = [r for r in records if str(r["event"]).startswith(("follow.", "export.", "fsops."))]
    assert scoped and all("job_id" in r for r in scoped)
    series = named(records, "follow.series_looked")
    assert series and all(r.get("edition_id") and r.get("series_id") for r in series)
    ops = named(records, "export.op_done")
    assert ops and all(r.get("unit_id") and r.get("edition_id") and r.get("target_id") for r in ops)


# -- no noise in nominal operation ------------


@pytest.mark.req("ENF-OBS-001", "ENF-OPS-014")
def test_a_follow_cycle_without_news_emits_at_most_two_info_lines(
    settings: Settings, tmp_path: Path, logs: Logs
) -> None:
    with seeded(settings, tmp_path) as env:
        env.inst.admin.source_add_suwayomi(url="http://suwayomi.lan:4567", secret=SUWAYOMI_SECRET)
        pools = JobPools(
            env.inst.settings.data_dir,
            engine=env.inst.engine,
            clock=env.inst.comp.clock,
            seed="l59",
        )
        rj.register_runtime_jobs(pools, env.inst.comp)
        run_job(pools, "follow.cycle")  # the first pass files what is new in the installation
        run_job(pools, "follow.cycle")
        logs.start("INFO")
        run_job(pools, "follow.cycle")  # the 15-minute cron: nothing new
        records = logs.drain()
    info = [r for r in records if r["level"] == "info"]
    assert 0 < len(info) <= 2, [r["event"] for r in info]
    assert {r["event"] for r in info} == {"job.started", "job.finished"}


# -- the guard: every registered job says it started and finished ------------


@pytest.mark.req("ENF-OBS-001")
def test_every_registered_job_emits_started_and_finished(
    settings: Settings, tmp_path: Path, logs: Logs
) -> None:
    with seeded(settings, tmp_path) as env:
        clock = ManualClock(WINDOW_NIGHT)
        pools = JobPools(
            env.inst.settings.data_dir, engine=env.inst.engine, clock=clock, seed="l59"
        )
        rj.register_runtime_jobs(pools, env.inst.comp)
        kinds = sorted(pools._handlers)  # the real registry
        assert len(kinds) >= 8
        logs.start("INFO")
        ids = {kind: run_job(pools, kind) for kind in kinds}
        records = logs.drain()
    for kind, job_id in ids.items():
        mine = [r for r in records if r.get("job_id") == job_id]
        names = [r["event"] for r in mine]
        assert "job.started" in names, kind
        assert "job.finished" in names, kind
        assert names.index("job.started") < names.index("job.finished")
        assert next(r for r in mine if r["event"] == "job.started")["job_kind"] == kind


@pytest.mark.req("ENF-OBS-001")
def test_a_failing_job_is_retried_then_dead_lettered_in_the_logs(
    settings: Settings, tmp_path: Path, logs: Logs
) -> None:
    with seeded(settings, tmp_path) as env:
        pools = JobPools(
            env.inst.settings.data_dir,
            engine=env.inst.engine,
            clock=env.inst.comp.clock,
            seed="l59",
        )

        def boom(_context: object) -> None:
            raise RuntimeError("synthetic failure")

        pools.register("l59.boom", pool="io", handler=boom, writes=False)  # type: ignore[arg-type]
        logs.start("INFO")
        for _ in range(8):
            created = pools.submit("l59.boom", dedupe_key=f"boom-{new_ulid()}")
            for _attempt in range(8):
                try:
                    pools._execute(created.job_id)
                    break
                except RetryTask:
                    continue
            else:
                continue
            break
        records = logs.drain()
    assert named(records, "job.retry")
    assert named(records, "job.dead_lettered")
    assert any(r["outcome"] == "error" for r in named(records, "job.finished"))


# -- secrets: nothing, at any level ------------


KNOWN = {
    "kavita key": API_KEY,
    "suwayomi password": PASSWORD,
    "suwayomi pair": SUWAYOMI_SECRET,
    "suwayomi basic": base64.b64encode(SUWAYOMI_SECRET.encode()).decode(),
    "kavita password": KAVITA_PASSWORD,
}


@pytest.mark.req("ENF-SEC-005", "ENF-SEC-013", "ENF-OBS-001")
def test_no_known_secret_in_a_debug_cycle_and_export(
    settings: Settings, tmp_path: Path, logs: Logs
) -> None:
    with seeded(settings, tmp_path) as env:
        records = lifecycle(env, logs, level="DEBUG")
        # an error carrying secrets, through the failure path (its traceback is a DEBUG line)
        pools = JobPools(
            env.inst.settings.data_dir,
            engine=env.inst.engine,
            clock=env.inst.comp.clock,
            seed="l59",
        )

        def leaky(_context: object) -> None:
            raise RuntimeError(
                f"upstream said x-api-key: {API_KEY} and Authorization: Basic "
                f"{KNOWN['suwayomi basic']} password={PASSWORD}"
            )

        pools.register("l59.leaky", pool="io", handler=leaky, writes=False)  # type: ignore[arg-type]
        run_job(pools, "l59.leaky")
        structlog.get_logger("l59").error(
            "l59.with_exc_info", exc_info=RuntimeError(f"cookie: session={API_KEY}")
        )
        records += logs.drain()
    text = logs.everything() + json.dumps(records)
    assert text.count("http.call") > 5  # the cycle really spoke, so the search is not vacuous
    for label, secret in KNOWN.items():
        assert secret not in text, label
    assert named(records, "job.failure_detail")  # the traceback line exists and is clean


@pytest.mark.req("ENF-SEC-005", "ENF-OBS-001")
def test_the_search_for_a_leak_does_work(logs: Logs) -> None:
    """A control: a secret printed in a value no rule knows is found by the same search."""
    logs.start("DEBUG")
    structlog.get_logger("l59").debug("control", blob="synthetic-unregistered-blob-9f3a")
    assert "synthetic-unregistered-blob-9f3a" in logs.everything()


@pytest.mark.req("ENF-SEC-005", "ENF-SEC-013")
def test_http_calls_are_logged_without_the_query_string(logs: Logs) -> None:
    import asyncio

    logs.start("DEBUG")
    recorder = Recorder()
    fetcher = GuardedFetcher(FakeResolver({"kavita.example": ["93.184.216.34"]}),
                             transport=recorder.transport)  # fmt: skip

    async def go() -> None:
        await fetcher.get("https://kavita.example/api/Image/cover?apiKey=SECRET-QUERY-KEY&x=1")
        await fetcher.get("https://kavita.example/api/opds/SECRET-PATH-KEY/series")
        with contextlib.suppress(InvalidFetchUrl):  # refused, and the refusal is logged too
            await fetcher.get("https://user:SECRET-USERINFO@kavita.example/plain")

    asyncio.run(go())
    text = logs.everything()
    for leaked in ("SECRET-QUERY-KEY", "SECRET-PATH-KEY", "SECRET-USERINFO", "apiKey"):
        assert leaked not in text
    assert "kavita.example/api/Image/cover" in text
    assert route_of("https://h.test:8080/a/b?k=v#f") == "h.test:8080/a/b"


@pytest.mark.req("ENF-SEC-005")
def test_the_http_library_does_not_log_urls_at_any_level(logs: Logs) -> None:
    import asyncio

    logs.start("DEBUG")

    async def go() -> None:
        async with httpx2.AsyncClient(transport=Recorder().transport) as client:
            await client.get("http://x.test/p?apiKey=LIB-LEVEL-SECRET")

    asyncio.run(go())
    assert "LIB-LEVEL-SECRET" not in logs.everything()


@pytest.mark.req("ENF-SEC-005")
def test_effective_config_at_start_hides_the_secrets(
    settings: Settings, logs: Logs, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import SecretStr

    from shelfhold.main import log_effective_config

    logs.start("DEBUG")
    with_secret = settings.model_copy(
        update={
            "api_token": SecretStr("tok-synthetic-0123456789"),
            "plugin_repos": ("https://repo.example/index.json?token=QUERY-SECRET-ABCDEF",),
        }
    )
    log_effective_config(with_secret)
    text = logs.everything()
    assert "startup.config" in text and "repo.example/index.json" in text
    assert "tok-synthetic-0123456789" not in text and "QUERY-SECRET-ABCDEF" not in text


# -- names from an untrusted archive ------------


@pytest.mark.req("ENF-SEC-005", "ENF-OBS-001")
def test_an_untrusted_entry_name_is_only_in_debug_and_escaped(logs: Logs) -> None:
    from shelfhold.acquisition.application.tickets import DownloadTickets
    from shelfhold.infrastructure.content_trust import UntrustedEntryError

    hostile = "../evil\n\x1b[31mred.exe"
    error = UntrustedEntryError("not_allowlisted", hostile)

    class Request:
        id = "req"
        unit_id = "unit"

    class Report:
        refused = 0

    tickets = object.__new__(DownloadTickets)
    tickets._engine = _NoEngine()  # type: ignore[attr-defined]
    tickets._clock = ManualClock(WINDOW_NIGHT)  # type: ignore[attr-defined]
    tickets._announce = None  # type: ignore[attr-defined]
    for level, shown in (("INFO", False), ("DEBUG", True)):
        logs.start(level)
        tickets._refuse(Request(), error, Report())  # type: ignore[arg-type]
        text = "".join(json.dumps(r) for r in logs.drain())
        assert ("evil" in text) is shown
        assert "\x1b" not in text and "\n" not in text.replace("\\n", "")
        if shown:
            assert untrusted_name(hostile)[1:-1].replace("\\", "") in text.replace("\\", "")


class _NoEngine:
    def begin(self) -> _NoEngine:
        return self

    def __enter__(self) -> _NoEngine:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, *_: object, **__: object) -> None:
        return None


# -- cost ------------


@pytest.mark.req("ENF-OPS-003")
def test_a_debug_call_at_info_runs_no_processor(logs: Logs) -> None:
    calls: list[str] = []

    def spy(_logger: object, _method: str, event_dict: dict[str, object]) -> dict[str, object]:
        calls.append(str(event_dict["event"]))
        return event_dict

    logs.start("INFO")
    config = structlog.get_config()
    structlog.configure(processors=[config["processors"][0], spy, *config["processors"][1:]])
    structlog.get_logger("l59").debug("l59.too_fine", value="x")
    structlog.get_logger("l59").info("l59.kept")
    assert calls == ["l59.kept"]


@pytest.mark.req("ENF-OBS-001")
def test_every_log_event_name_follows_the_convention() -> None:
    """`context.action`: lowercase words, one dot at least (the table of docs/admin/journaux.md)."""
    import re

    root = Path(__file__).resolve().parents[2] / "src" / "shelfhold"
    pattern = re.compile(r"\blog\.(?:debug|info|warning|error)\(\s*\"([^\"]+)\"")
    bad = {
        name
        for path in root.rglob("*.py")
        for name in pattern.findall(path.read_text("utf-8"))
        if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", name)
    }
    assert bad <= LEGACY_NAMES, sorted(bad - LEGACY_NAMES)


LEGACY_NAMES = {  # they predate the convention; renaming them would break the logs people grep
    "decode_worker_sandbox",
    "meta_repair_skipped",
    "migrate_done",
    "migration_done",
    "migration_restored",
    "migration_started",
    "migration_up_to_date",
    "mirror_event_failed",
    "mirror_heal_failed",
    "mirror_skipped",
    "mirror_write_failed",
    "pre_migration_backup",
    "restamp_failed",
}


@pytest.mark.req("ENF-OBS-001")
def test_the_admin_page_on_logs_names_no_host_ip_or_personal_path() -> None:
    import re

    page = Path(__file__).resolve().parents[3] / "docs" / "admin" / "journaux.md"
    text = page.read_text("utf-8")
    assert "contexte.action" in text and "duration_ms" in text
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)  # no IP address
    assert not re.search(r"/home/|/Users/|\.lan\b|\.local\b|\.ts\.net", text)  # no host, no path
