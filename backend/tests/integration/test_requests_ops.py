"""M3 L35 (EF-USR-010, EF-SRC-029): the ports of the request service over the application: which
kind a source is, how a reference is taken, how an arrival reaches a request, and that a failing
subscriber never undoes a filing."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from shelfhold.acquisition.application.family_search import FamilyRef
from shelfhold.acquisition.application.requests import (
    RequestRefusedError,
    RequestService,
    SourceRef,
)
from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.ops import requests as ops_requests
from shelfhold.ops.errors import CodedRefusal
from shelfhold.plugin_host.registry import PluginCandidate, PluginOrigin, PluginRegistry
from shelfhold.sdk.age import AgeRating
from tests.federated_env import FakeSource
from tests.request_env import (
    MEMBER_ID,
    FakePorts,
    Rig,
    admin,
    bind_work,
    requester,
    rig,
)


def comp_of(r: Rig, *sources: FakeSource) -> Any:
    registry = PluginRegistry()
    registry.load_all(
        [
            PluginCandidate(key=s.key, origin=PluginOrigin.FIRST_PARTY, load=lambda s=s: s)  # type: ignore[misc]
            for s in sources
        ]
    )
    return SimpleNamespace(
        engine=r.engine, clock=r.clock, bus=EventBus(r.clock), plugins=lambda: registry,
        settings=SimpleNamespace(fresh_auth_s=900),
    )  # fmt: skip


@pytest.mark.req("EF-USR-010", "EF-SRC-029")
def test_a_series_of_the_connected_service_or_of_images_is_followed(settings: Settings) -> None:
    with rig(settings) as r:
        ports = ops_requests.build_ports(
            comp_of(r, FakeSource("pics", kinds=("images",)), FakeSource("txt", kinds=("text",)))
        )
        assert ports.kind_of("suwayomi:1") == "manga"
        assert ports.kind_of("pics") == "manga"
        assert ports.kind_of("txt") == "novel"
        assert ports.kind_of("unknown") == "novel"  # a source nobody loaded is not followed


@pytest.mark.req("EF-USR-010", "EF-SRC-029")
def test_ports_delegate_to_follow_acquire_and_the_unit_count(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    class FakeAcquire:
        def __init__(self, comp: Any) -> None:
            pass

        def unit_count(self, *a: Any) -> int:
            calls.append(("count", a, {}))
            return 7

        def prepare(self, **kw: Any) -> Any:
            calls.append(("prepare", (), kw))
            return SimpleNamespace(work_id="W", edition_id="E", request_id="R")

        def enqueue(self, request_id: str | None) -> dict[str, Any]:
            calls.append(("enqueue", (request_id,), {}))
            return {}

    class FakeFollow:
        def __init__(self, comp: Any) -> None:
            pass

        def follow(self, *a: Any, **kw: Any) -> dict[str, Any]:
            calls.append(("follow", a, kw))
            return {}

    monkeypatch.setattr(ops_requests, "AcquireOps", FakeAcquire)
    monkeypatch.setattr(ops_requests, "FollowOps", FakeFollow)
    with rig(settings) as r:
        ports = ops_requests.build_ports(comp_of(r))
        assert ports.estimate("alpha", "r1").units == 7
        ports.follow("alpha", "r1", requester_user_id=MEMBER_ID, visibility="private")
        prepared = ports.prepare("alpha", "r1", requester_user_id=MEMBER_ID, visibility="private")
        assert (prepared.work_id, prepared.edition_id, prepared.handle) == ("W", "E", "R")
        ports.start(prepared.handle)
    assert [c[0] for c in calls] == ["count", "follow", "prepare", "enqueue"]
    assert calls[1][2] == {"requester_user_id": MEMBER_ID, "visibility": "private", "units": 7}
    assert calls[2][2]["url"] is None and calls[2][2]["visibility"] == "private"
    assert calls[3][1] == ("R",)


@pytest.mark.req("EF-USR-010")
def test_a_shelf_that_does_not_exist_or_refuses_is_a_refusal_of_the_approval(
    settings: Settings,
) -> None:
    with rig(settings) as r:
        ports = ops_requests.build_ports(comp_of(r))
        work = r.work()
        edition = r.rows("SELECT id FROM edition WHERE work_id = :w", w=work)[0][0]
        for shelf in ("01J0000000000000000000NOPE", "not-an-id"):
            with pytest.raises(RequestRefusedError) as refused:
                ports.settle(work, edition, None, shelf, principal=admin())
            assert refused.value.code == "shelf_refused"
        assert ports.proposed_shelf(edition) is None  # no shelf is set up for the kind
        # no shelf is proposed: the rating alone is written, and nothing is filed
        assert ports.settle(work, edition, AgeRating(10), None, principal=admin()) is None
        assert r.rows("SELECT age_rating FROM work WHERE id = :w", w=work)[0][0] == 10
        stale = admin(fresh=False)  # a stale session cannot rate: `not_allowed`, nothing written
        other = r.work()
        with pytest.raises(RequestRefusedError) as refused:
            ports.settle(other, None, AgeRating(10), None, principal=stale)
        assert refused.value.code == "not_allowed"
        assert r.rows("SELECT rated_at FROM work WHERE id = :w", w=other)[0][0] is None


class _Acquire:
    def __init__(self, ref: FamilyRef | None, open_: bool = True) -> None:
        self.ref, self.open_ = ref, open_

    def resolve_family_ref(self, token: str, *, owner: str) -> FamilyRef:
        if self.ref is None:
            raise CodedRefusal("invalid_ref", "no")
        return self.ref

    def family_door_open(self, owner: str, source_key: str) -> bool:
        return self.open_


@pytest.mark.req("EF-USR-010", "EF-USR-014")
def test_a_reference_is_taken_only_while_the_door_is_open() -> None:
    ref = FamilyRef("alpha", "r1", {"entry": "e"}, "A Tale")
    taken = ops_requests.resolve_request_ref(_Acquire(ref), "t", "kid")  # type: ignore[arg-type]
    assert taken == SourceRef("alpha", "r1", "A Tale", {"entry": "e"})
    untitled = FamilyRef("alpha", "r1", {}, "")
    assert ops_requests.resolve_request_ref(_Acquire(untitled), "t", "k").title == "-"  # type: ignore[arg-type,union-attr]
    assert ops_requests.resolve_request_ref(_Acquire(None), "t", "kid") is None  # type: ignore[arg-type]
    assert ops_requests.resolve_request_ref(_Acquire(ref, open_=False), "t", "kid") is None  # type: ignore[arg-type]


def _stored(comp: Any, work_id: str) -> None:
    with comp.engine.begin() as connection:
        event = comp.bus.publish(
            connection, "unit.stored",
            {"unit_id": new_ulid(), "edition_id": new_ulid(),
             "work_id": work_id, "kind": "cbz"},
        )  # fmt: skip
    comp.bus.deliver([event])


@pytest.mark.req("EF-USR-010")
def test_a_filed_unit_arrives_the_approved_request_and_a_failure_never_undoes_it(
    settings: Settings,
) -> None:
    with rig(settings, ports=FakePorts(kinds={"alpha": "manga"})) as r:
        comp = comp_of(r)
        service = RequestService(r.engine, r.clock, r.ports, bus=comp.bus)
        ops_requests.subscribe_arrivals(comp, service)
        made = service.create_from_ref(
            requester(MEMBER_ID, "member"), SourceRef("alpha", "r1", "T", {})
        )
        service.approve(made.id, admin())
        with r.engine.begin() as connection:
            work = bind_work(connection, "alpha", "r1")
        _stored(comp, work)
        assert r.state_of(made.id) == "arrived"
        # a payload without a Work is ignored; a service that breaks does not break the filing
        with r.engine.begin() as connection:
            comp.bus.deliver([comp.bus.publish(connection, "unit.stored", {"kind": "cbz"})])

        def broken(_work_id: str) -> int:
            raise RuntimeError("boom")

        service.arrived = broken  # type: ignore[method-assign]
        _stored(comp, work)  # no exception reaches the publisher
