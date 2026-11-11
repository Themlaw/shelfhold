"""lncrawl connected service (M2 L25): EF-SRC-010, 033, 034, 032, EF-BIB-049, 051.

Everything runs in process against `tests/fakes/lncrawl`: no socket, no third-party content, no
crawler. The simulated instance referees the wire behaviour (allow-listed routes, no route that
names or switches a crawler) and records it, so these tests assert on what was really sent. The
attacks of the lot are here: a second step that leaves the announced host or range, a queue that
never ends or is poisoned, a secret that leaks, a name that is repeated.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable
from pathlib import Path
from typing import Any

import pytest
import structlog

from shelfhold.config import Settings
from shelfhold.plugin_host.adapters.meta.lncrawl import LncrawlService, api
from shelfhold.plugin_host.registry import PluginCandidate, PluginOrigin, PluginRegistry
from shelfhold.sdk.connected import ConnectedCapability
from shelfhold.sdk.refs import FileAsset, RangedPayload, RangeRef, SeriesRef, UnitInfo, UnitRef
from shelfhold.sdk.source import (
    Capability,
    SourceChanged,
    SourceManifest,
    TemporarilyUnavailable,
)
from tests.fakes.lncrawl import API_KEY, BASE_URL, CANARY, FakeLncrawl, FakeLncrawlFetcher

REPO = Path(__file__).resolve().parents[3]
MANIFEST = REPO / "sources" / "meta_lncrawl" / "manifest.json"


def run[T](coro: Awaitable[T]) -> T:
    async def go() -> T:
        return await coro

    return asyncio.run(go())


def make(
    server: FakeLncrawl | None = None,
) -> tuple[LncrawlService, FakeLncrawlFetcher, FakeLncrawl]:
    server = server or FakeLncrawl()
    return LncrawlService(BASE_URL, poll_delay=0.0), FakeLncrawlFetcher(server), server


def alpha(service: LncrawlService, f: FakeLncrawlFetcher) -> tuple[SeriesRef, list[UnitInfo]]:
    (ref,) = run(service.followed_series(f))
    return ref, run(service.list_units(f, ref))


def volume_target(ref: SeriesRef, units: list[UnitInfo], label: str) -> RangeRef:
    span = [u for u in units if u.volume_label == label]
    return RangeRef(series=ref, first=span[0], last=span[-1], volume_label=label)


def ready(service: LncrawlService, f: FakeLncrawlFetcher, ticket: Any) -> str:
    state = "queued"
    for _ in range(10):
        state = run(service.poll(f, ticket))
        if state in ("ready", "failed", "gone"):
            break
    return state


# --- EF-SRC-010: the user's instance, the EPUB as it came ---------------------------------------
@pytest.mark.req("EF-SRC-010", "EF-SRC-033")
def test_lncrawl_adapter_targets_user_instance_epub_passthrough() -> None:
    service, f, server = make()
    ref, units = alpha(service, f)
    target = volume_target(ref, units, "Vol.1")
    ticket = run(service.request(f, target))
    assert ticket.target_kind == "range"
    assert ready(service, f, ticket) == "ready"
    payload = run(service.fetch(f, ticket))
    assert isinstance(payload, RangedPayload)
    asset = payload.asset
    assert asset.mime == "application/epub+zip" and asset.filename.endswith(".epub")
    assert asset.request.url.startswith(BASE_URL + "/api/v1/artifacts/")  # the user's instance
    served = run(f.download(asset.request)).content
    assert served == server.build_epub(6)  # the bytes of the instance, not rewritten
    assert payload.covers == [u.remote_unit_id for u in units[:6]]
    assert payload.volume_closed is True
    assert server.violations == [] and f.unscripted == []
    assert service.manifest.content_trust == "untrusted"  # EF-BIB-051: the body is never trusted
    assert asset.request.headers == {}  # no credential rides in the stored reference


@pytest.mark.req("EF-SRC-010", "ENF-LEG-001")
def test_no_crawler_enabled_or_listed_by_default() -> None:
    assert Settings().active_sources == ()  # nothing is switched on by default
    registry = PluginRegistry(active_keys=())
    registry.load_all([PluginCandidate(key="lncrawl", origin=PluginOrigin.FIRST_PARTY,
                                       load=lambda: LncrawlService(BASE_URL))])  # fmt: skip
    assert registry.active() == []
    service, f, server = make()
    ref, units = alpha(service, f)
    ticket = run(service.request(f, volume_target(ref, units, "Vol.1")))
    ready(service, f, ticket)
    run(service.fetch(f, ticket))
    assert server.violations == []  # nothing outside the contract was ever asked
    assert not [r for r in server.requests if "crawler" in r.path or "source" in r.path]
    names = {n.lower() for n in dir(service)}
    assert not {n for n in names if "crawler" in n}  # no method lists, picks or switches one
    declared = json.loads(MANIFEST.read_text("utf-8"))
    assert "sources" not in declared and "crawlers" not in declared  # nothing is listed
    for link in ("homepage", "terms_url"):  # the tool's own repository is the one name allowed
        declared.pop(link)
    assert "crawler" not in json.dumps(declared).lower()
    compose = [p for p in REPO.glob("**/*compose*.y*ml") if "node_modules" not in p.parts]
    assert not [p for p in compose if "lncrawl" in p.read_text("utf-8").lower()]  # no profile


@pytest.mark.req("EF-SRC-010", "ENF-LEG-001")
def test_the_host_ships_lncrawl_loaded_but_switched_off_with_settings_of_its_own() -> None:
    from shelfhold.plugin_host.public import first_party_candidates
    from shelfhold.plugin_host.registry import PluginStatus
    from shelfhold.plugin_host.settings import parse_settings_schema

    registry = PluginRegistry(active_keys=())
    records = {r.key: r for r in registry.load_all(first_party_candidates())}
    assert records["lncrawl"].status is PluginStatus.LOADED and registry.active() == []
    schema = parse_settings_schema(records["lncrawl"].manifest.settings_schema)
    assert [f.name for f in schema.fields if f.secret] == ["api_key"]
    assert records["lncrawl"].instance.base_url == ""  # no address until the admin gives one


@pytest.mark.req("EF-SRC-010", "EF-SRC-033")
def test_lncrawl_manifest_and_declared_capabilities() -> None:
    service = LncrawlService(BASE_URL)
    on_disk = SourceManifest.model_validate_json(MANIFEST.read_text("utf-8"))
    assert service.manifest == on_disk  # the manifest of sources/meta_lncrawl is the one served
    assert on_disk.key == "lncrawl" and on_disk.content_trust == "untrusted"
    assert on_disk.translation_nature == "community"  # EF-BIB-049
    assert Capability.RANGED_CONTENT in on_disk.capabilities
    assert ConnectedCapability.SERVER_QUEUE in service.connected
    assert ConnectedCapability.RELEASE_CLAIM not in service.connected  # nothing erased upstream
    raw = json.loads(MANIFEST.read_text("utf-8"))
    assert raw["permissions"]["lan_targets"] == []  # declared by the admin, never baked in
    assert raw["settings_schema"]["properties"]["api_key"]["writeOnly"] is True


# --- EF-SRC-034: two steps, one ticket ---------------------------------------------------------
@pytest.mark.req("EF-SRC-034", "EF-SRC-032")
def test_lncrawl_range_request_two_step_one_ticket() -> None:
    service, f, server = make()
    ref, units = alpha(service, f)
    ticket = run(service.request(f, volume_target(ref, units, "Vol.2")))
    assert len(server.jobs) == 1  # one job for the whole range, never one per chapter
    asked = f.posts[0]["json"]
    assert (asked["first"], asked["last"]) == ("ch-7", "ch-10")
    states = [run(service.poll(f, ticket)) for _ in range(3)]
    assert states == ["queued", "downloading", "ready"]
    payload = run(service.fetch(f, ticket))
    assert isinstance(payload, RangedPayload) and payload.volume_closed is False  # rolling tail
    assert len(payload.covers) == 4 and len(server.jobs) == 1


@pytest.mark.req("EF-SRC-034")
def test_lncrawl_unit_target_is_a_one_chapter_range_with_a_unit_payload() -> None:
    service, f, _ = make()
    ref, units = alpha(service, f)
    ticket = run(service.request(f, UnitRef(series=ref, unit=units[0])))
    assert ticket.target_kind == "unit"
    assert ready(service, f, ticket) == "ready"
    payload = run(service.fetch(f, ticket))
    assert isinstance(payload, FileAsset) and payload.mime == "application/epub+zip"


@pytest.mark.req("EF-SRC-034")
def test_lncrawl_cancel_asks_the_instance_once_and_a_gone_job_is_gone() -> None:
    service, f, server = make()
    ref, units = alpha(service, f)
    ticket = run(service.request(f, volume_target(ref, units, "Vol.1")))
    run(service.cancel(f, ticket))
    assert server.cancelled == ["job-1"]
    assert run(service.poll(f, ticket)) == "gone"
    server.jobs.clear()  # the instance forgot it (a reinstall, a purge of its queue)
    assert run(service.poll(f, ticket)) == "gone"


@pytest.mark.req("EF-SRC-034", "ENF-SEC-009")
def test_lncrawl_token_of_a_ticket_is_not_trusted_to_name_another_host_or_route() -> None:
    service, f, server = make()
    ref, units = alpha(service, f)
    ticket = run(service.request(f, volume_target(ref, units, "Vol.1")))
    forged = ticket.model_copy(update={"token": json.dumps({"j": "../admin", "n": "x"})})
    with pytest.raises(SourceChanged):
        run(service.poll(f, forged))
    with pytest.raises(SourceChanged):
        run(service.poll(f, ticket.model_copy(update={"token": "not json"})))
    assert server.violations == [] and f.unscripted == []


# --- attack: the second step leaves the host or the range that was announced -------------------
def _tamper(server: FakeLncrawl, **changes: Any) -> None:
    def apply(answer: dict[str, Any]) -> dict[str, Any]:
        return answer | changes if answer["state"] == "done" else answer

    server.tamper_job = apply


@pytest.mark.req("EF-SRC-034", "ENF-SEC-009")
@pytest.mark.parametrize(
    "bad_artifact",
    ["http://other.invalid/x", "../../etc/passwd", "a/b", "a@other.invalid", "a b", "", "x" * 200],
)
def test_lncrawl_second_step_with_a_foreign_artifact_reference_is_refused(
    bad_artifact: str,
) -> None:
    service, f, server = make()
    _tamper(server, artifact=bad_artifact)
    ref, units = alpha(service, f)
    ticket = run(service.request(f, volume_target(ref, units, "Vol.1")))
    ready(service, f, ticket)
    with pytest.raises(SourceChanged):
        run(service.fetch(f, ticket))
    assert server.artifact_hits == [] and f.downloads == [] and server.violations == []


@pytest.mark.req("EF-SRC-034", "ENF-SEC-009")
def test_lncrawl_second_step_never_follows_a_url_the_instance_hands_back() -> None:
    service, f, server = make()
    _tamper(server, download_url="http://other.invalid/steal.epub", url="http://other.invalid/")
    ref, units = alpha(service, f)
    ticket = run(service.request(f, volume_target(ref, units, "Vol.1")))
    ready(service, f, ticket)
    payload = run(service.fetch(f, ticket))
    assert isinstance(payload, RangedPayload)
    assert payload.asset.request.url.startswith(BASE_URL + "/api/v1/artifacts/")
    assert "other.invalid" not in payload.model_dump_json()
    assert f.unscripted == []  # nothing was asked of another host


@pytest.mark.req("EF-SRC-034", "EF-SRC-032")
@pytest.mark.parametrize(
    "changes",
    [
        {"last": "ch-10"},  # a later range than the one announced
        {"first": "ch-2"},  # another start
        {"novel": "nv-other"},  # another novel
        {"covers": [f"ch-{n}" for n in range(1, 11)]},  # more chapters than were asked
        {"covers": ["ch-1", "ch-1", "ch-2"]},  # a chapter twice
        {"covers": ["ch-2", "ch-1"]},  # out of order
        {"covers": [f"ch-{n}" for n in range(1, 9000)]},  # a list that is not a range
        {"covers": []},  # nothing
        {"covers": "ch-1"},  # not a list
    ],
)
def test_lncrawl_second_step_for_another_range_than_announced_is_refused(
    changes: dict[str, Any],
) -> None:
    service, f, server = make()
    _tamper(server, **changes)
    ref, units = alpha(service, f)
    ticket = run(service.request(f, volume_target(ref, units, "Vol.1")))
    with pytest.raises(SourceChanged):  # at the poll for another novel, at the file otherwise
        ready(service, f, ticket)
        run(service.fetch(f, ticket))
    assert f.downloads == []


# --- attack: a queue that never ends, or that is poisoned --------------------------------------
@pytest.mark.req("EF-SRC-033", "EF-SRC-034")
def test_lncrawl_endless_queue_is_only_ever_queued_and_costs_one_request_per_poll() -> None:
    service, f, server = make()
    server.stuck = True
    ref, units = alpha(service, f)
    ticket = run(service.request(f, volume_target(ref, units, "Vol.1")))
    before = len(server.requests)
    assert [run(service.poll(f, ticket)) for _ in range(50)] == ["queued"] * 50
    assert len(server.requests) - before == 50  # no loop inside the adapter
    with pytest.raises(TemporarilyUnavailable):  # fetching a job that is not done is not a file
        run(service.fetch(f, ticket))


@pytest.mark.req("EF-SRC-033")
@pytest.mark.parametrize(
    "answer",
    [
        {"state": "exploded"},
        {"state": ["done"]},
        {"state": None},
        {"state": "queued", "novel": "nv-other"},
    ],
)
def test_lncrawl_poisoned_poll_answer_is_a_source_change_not_a_state(
    answer: dict[str, Any],
) -> None:
    service, f, server = make()
    server.tamper_job = lambda a: a | answer
    ref, units = alpha(service, f)
    ticket = run(service.request(f, volume_target(ref, units, "Vol.1")))
    with pytest.raises(SourceChanged):
        run(service.poll(f, ticket))


@pytest.mark.req("EF-SRC-033")
def test_lncrawl_fetch_range_gives_up_on_a_job_that_never_ends() -> None:
    service, f, server = make()
    server.stuck = True
    ref, units = alpha(service, f)
    with pytest.raises(TemporarilyUnavailable):
        run(service.fetch_range(f, volume_target(ref, units, "Vol.1")))
    assert len(server.requests_to("/jobs/job-1")) <= 60  # bounded: it does not spin forever


@pytest.mark.req("EF-SRC-033", "ENF-SEC-002")
def test_lncrawl_reply_too_large_or_not_json_is_refused() -> None:
    service, f, server = make()
    server.huge_reply = True
    with pytest.raises(SourceChanged):
        run(service.semantic_health(f))
    plain, g, other = make()
    for raw in (b"<html>not json</html>", b"[" * 100_000, b'{"items": "x"}', b"[]"):
        other.raw_reply = raw
        with pytest.raises(SourceChanged):  # nested past the stack: no crash either
            run(plain.followed_series(g))


@pytest.mark.req("EF-SRC-033", "ENF-SEC-002")
def test_lncrawl_endless_listing_is_cut_at_a_bound() -> None:
    service, f, server = make()
    server.endless_pages = True
    with pytest.raises(SourceChanged):
        run(service.followed_series(f))
    assert len(server.requests_to("/library")) < 300
    ref = SeriesRef(source_key="lncrawl", remote_id="nv-alpha", title="")
    with pytest.raises(SourceChanged):
        run(service.list_units(f, ref))


# --- attack: a secret that leaks ---------------------------------------------------------------
@pytest.mark.req("EF-SRC-035", "ENF-SEC-009")
def test_lncrawl_api_key_never_reaches_a_ticket_a_reference_a_log_or_an_error() -> None:
    service, f, _ = make()
    with structlog.testing.capture_logs() as logs:
        ref, units = alpha(service, f)
        ticket = run(service.request(f, volume_target(ref, units, "Vol.1")))
        ready(service, f, ticket)
        payload = run(service.fetch(f, ticket))
        bad, g, _ = make(FakeLncrawl(api_key="another-synthetic-key"))
        with pytest.raises(Exception) as refused:
            run(bad.followed_series(g))
    everything = " ".join(
        [
            ticket.model_dump_json(), payload.model_dump_json(), ref.model_dump_json(),
            *(u.model_dump_json() for u in units), repr(service), str(refused.value),
            repr(refused.value), json.dumps(logs, default=str),
        ]
    )  # fmt: skip
    assert API_KEY not in everything and "another-synthetic-key" not in everything
    assert getattr(service, "_key", None) is None and API_KEY not in repr(vars(service))
    assert CANARY not in str(refused.value)  # the body of the refusal is not copied


@pytest.mark.req("EF-SRC-031", "ENF-SEC-009")
def test_lncrawl_refused_credentials_are_auth_not_a_broken_source() -> None:
    from shelfhold.sdk.source import AuthRequired

    service, _, _ = make()
    f = FakeLncrawlFetcher(FakeLncrawl(), key=None)  # the host injected nothing
    with pytest.raises(AuthRequired):
        run(service.followed_series(f))


# --- attack: a name the instance repeats ---------------------------------------------------------
@pytest.mark.req("EF-SRC-010")
def test_lncrawl_crawler_name_in_a_reply_is_never_logged_raised_or_shown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    service, f, server = make()
    server.leak_name = True
    with structlog.testing.capture_logs() as logs:
        ref, units = alpha(service, f)
        info = run(service.get_series(f, ref))
        ticket = run(service.request(f, volume_target(ref, units, "Vol.1")))
        ready(service, f, ticket)
        payload = run(service.fetch(f, ticket))
        _tamper(server, state="exploded")
        with pytest.raises(SourceChanged) as changed:
            run(service.poll(f, ticket))
    shown = " ".join(
        [
            info.model_dump_json(), ticket.model_dump_json(), payload.model_dump_json(),
            *(u.model_dump_json() for u in units), str(changed.value), repr(changed.value),
            json.dumps(logs, default=str), caplog.text,
        ]
    )  # fmt: skip
    assert CANARY not in shown
    assert f.unscripted == [] and server.violations == []


# --- an instance outside the DomainRules ------------------------------------
@pytest.mark.req("EF-SRC-003", "EF-SRC-010")
def test_lncrawl_instance_outside_the_domain_rules_is_refused() -> None:
    from shelfhold.infrastructure.fetcher.guard import DomainAllowlist, DomainNotAllowed

    manifest = LncrawlService(BASE_URL).manifest
    rules = manifest.permissions.domains
    assert [r.base_url_setting for r in rules] == ["base_url"]  # the admin's address, only that
    allowed = DomainAllowlist("lncrawl", rules, {"base_url": BASE_URL})
    assert allowed.allows("lncrawl.lan") and not allowed.allows("other.invalid")
    with pytest.raises(DomainNotAllowed):
        allowed.check("other.invalid", "api")
    for hostile in ("file:///etc/passwd", "http://user:pw@other.invalid/", "ftp://x.lan", 5):
        closed = DomainAllowlist("lncrawl", rules, {"base_url": hostile})
        assert not closed.allows("other.invalid") and not closed.allows("lncrawl.lan")


@pytest.mark.req("EF-SRC-010")
def test_lncrawl_without_an_instance_does_nothing() -> None:
    service = LncrawlService("")
    f = FakeLncrawlFetcher(FakeLncrawl())
    with pytest.raises(TemporarilyUnavailable):
        run(service.followed_series(f))
    assert f.server.requests == []


# --- listing, identities, health ------------------------------------
@pytest.mark.req("EF-SRC-036", "EF-SRC-033")
def test_lncrawl_identities_follow_the_instance_and_the_fingerprint_its_installation() -> None:
    service, f, server = make()
    ref, units = alpha(service, f)
    assert (ref.source_key, ref.remote_id) == ("lncrawl", "nv-alpha")
    assert units[0].remote_unit_id == "lncrawl:nv-alpha:ch-1"
    assert [u.volume_label for u in units] == ["Vol.1"] * 6 + ["Vol.2"] * 4
    assert [u.volume_closed for u in units[:6]] == [True] * 6 and units[9].volume_closed is False
    assert {u.team for u in units} == {"Synthetic Team A", "Synthetic Team B"}
    first = run(service.instance_fingerprint(f))
    server.reinstall()
    assert run(service.instance_fingerprint(f)) != first
    assert len(first) == 64 and "inst-0001" not in first  # a hash, never the raw identifier


@pytest.mark.req("EF-SRC-026", "EF-SRC-033")
def test_lncrawl_health_reports_an_unsupported_version() -> None:
    service, f, server = make()
    assert run(service.semantic_health(f)).ok
    server.version = "0.9.0"
    health = run(service.semantic_health(f))
    assert not health.ok and health.detail == "source.version_unsupported"
    server.version = "banana"
    assert not run(service.semantic_health(f)).ok


@pytest.mark.req("EF-SRC-010")
def test_lncrawl_search_reads_only_the_library_of_the_account() -> None:
    service, f, server = make()
    page = run(service.search(f, "alpha", {}, None))
    assert [r.remote_id for r in page.items] == ["nv-alpha"]
    assert run(service.search(f, "zzz", {}, None)).items == []
    assert run(service.resolve_url(f, "https://example.org/x")) is None
    assert server.violations == []
    assert service.filters() == []


# --- review of the PR: regression tests -------------------------------------------------------
def _second_novel(server: FakeLncrawl) -> None:
    import copy

    beta = copy.deepcopy(server.novels[0])
    beta["id"], beta["title"] = "nv-beta", "Novel Beta"
    server.novels.append(beta)  # the chapter ids are local to a novel: the same ch-1..ch-10


@pytest.mark.req("EF-SRC-034", "ENF-SEC-009")
def test_a_job_answer_without_its_novel_is_refused_not_taken_for_ours() -> None:
    service, f, server = make()
    _second_novel(server)
    ref = next(r for r in run(service.followed_series(f)) if r.remote_id == "nv-alpha")
    units = run(service.list_units(f, ref))
    beta = json.loads(
        server._post("/jobs", {"novel": "nv-beta", "first": "ch-1", "last": "ch-6"}).body
    )
    ticket = run(service.request(f, volume_target(ref, units, "Vol.1")))

    def tamper(answer: dict[str, Any]) -> dict[str, Any]:
        if answer["state"] != "done":
            return answer
        return {k: v for k, v in answer.items() if k != "novel"} | {
            "artifact": f"art-{beta['job']}"
        }

    server.tamper_job = tamper
    with pytest.raises(SourceChanged):
        ready(service, f, ticket)
        run(service.fetch(f, ticket))


@pytest.mark.req("EF-SRC-010", "ENF-SEC-015")
def test_a_team_that_looks_like_a_host_or_an_address_is_dropped() -> None:
    service, f, server = make()
    for number, chapter in enumerate(server.novels[0]["chapters"]):
        chapter["team"] = (
            "some-host.invalid",
            "http://some-host.invalid/x",
            "host.example:80/p",
            "Team A",
        )[number % 4]
        chapter["title"] = "see some-host.invalid"
    server.novels[0]["authors"] = ["some-host.invalid"]
    _ref, units = alpha(service, f)
    assert {u.team for u in units} == {None, "Team A"}


@pytest.mark.req("EF-SRC-034", "ENF-SEC-009")
def test_a_listing_keeps_only_the_fields_that_are_read_and_bounds_before_it_grows() -> None:
    service, f, server = make()
    pad = "x" * 4900
    chapters = server.novels[0]["chapters"]
    chapters[:] = [
        {"id": f"c{n}", "order": n, "volume": "Vol.1", "junk": pad, "title": pad}
        for n in range(1, 301)
    ]
    ref = run(service.followed_series(f))[0]
    entries = run(service._pages(f, api.CHAPTERS.format(novel=ref.remote_id)))
    assert len(entries) == 300
    assert all(set(e) <= {"id", "order", "title", "volume", "closed", "team"} for e in entries)
    assert all(len(e["title"]) <= 400 for e in entries)
    server.page_size = 100
    chapters[:] = [{"id": f"c{n}", "order": n} for n in range(1, api.MAX_ITEMS + 150)]
    with pytest.raises(SourceChanged):  # refused before the excess is kept
        run(service._pages(f, api.CHAPTERS.format(novel=ref.remote_id)))


@pytest.mark.req("EF-SRC-034", "EF-BIB-051")
def test_a_refused_answer_is_untrusted_never_a_pause_of_the_service() -> None:
    from shelfhold.acquisition.application.lncrawl_ranges import RangeAnswerRefused
    from shelfhold.acquisition.domain.failures import Disposition, classify, disposition_of

    failure = classify(RangeAnswerRefused("the file is not an EPUB container"))
    assert failure == "untrusted" and disposition_of(failure) is Disposition.REFUSE
