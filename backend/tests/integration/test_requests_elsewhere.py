"""M4 L30 (EF-USR-019): a request that is bought or borrowed elsewhere, the file dropped in the
import folder matched to it, the requester told without a title, and the proposals of the source
search that only an admin validates (D22).

The attacks are here too: another profile's request, a child, a member, a key that only reads."""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.requests import (
    Proposal,
    RequestRefusedError,
    RequestService,
    SourceRef,
)
from shelfhold.acquisition.domain.request_target import RequestTarget
from shelfhold.config import Settings
from shelfhold.identity.public import Credential
from tests.notify_env import world
from tests.request_env import (
    ADMIN_ID,
    CHILD_ID,
    MEMBER_ID,
    OTHER_ID,
    FakePorts,
    Rig,
    admin,
    principal,
    requester,
    rig,
)

pytestmark = pytest.mark.req("EF-USR-019")

SHOP = "https://shop.example/elise"
ISBN = "9780306406157"  # the well-known sample number, no real book


def ask(r: Rig, who: str, role: str, kind: str, value: str) -> str:
    return r.service.create(requester(who, role), RequestTarget(kind, value)).id


def to_buy(r: Rig, request_id: str, link: str | None = SHOP) -> None:
    r.service.mark_elsewhere(request_id, admin(), "to_buy", link=link)


def set_title(r: Rig, text: str) -> str:
    return r.work(title=text, sort_title=text.lower())


# -- the states ------------------------------------------------------------------------------------


def test_to_buy_and_borrow_elsewhere_keep_a_clean_official_link(settings: Settings) -> None:
    with rig(settings) as r:
        buy = ask(r, MEMBER_ID, "member", "free_text", "Élise")
        borrow = ask(r, MEMBER_ID, "member", "isbn", ISBN)
        shown = r.service.mark_elsewhere(
            buy, admin(), "to_buy", link=SHOP + "?utm_source=a&utm_medium=b"
        )
        assert (shown.state, shown.elsewhere_url) == ("to_buy", SHOP)
        again = r.service.mark_elsewhere(borrow, admin(), "borrow_elsewhere", link=None)
        assert (again.state, again.elsewhere_url) == ("borrow_elsewhere", None)
        assert r.service.get(MEMBER_ID, buy).elsewhere_url == SHOP
        (line,) = r.audit("request.to_buy")
        assert line[0].startswith("session:")  # the admin's session, as for any decision


@pytest.mark.parametrize(
    "link", ["javascript:alert(1)", "http://shop.example/x", "https://u:p@shop.example/x",
             "https://10.0.0.1/x", "data:text/html,x", " https://shop.example/x", "x" * 3000],
)  # fmt: skip
def test_a_link_that_is_not_a_plain_https_address_is_refused(settings: Settings, link: str) -> None:
    with rig(settings) as r:
        rid = ask(r, MEMBER_ID, "member", "free_text", "Élise")
        with pytest.raises(RequestRefusedError) as refused:
            r.service.mark_elsewhere(rid, admin(), "to_buy", link=link)
        assert refused.value.code == "invalid_link"
        assert r.state_of(rid) == "pending"


def test_no_protected_file_lending_is_wired(settings: Settings) -> None:
    """There is no state, no port and no column that lends a protected file: `borrow_elsewhere`
    keeps an address and nothing else."""
    with rig(settings) as r:
        rid = ask(r, MEMBER_ID, "member", "free_text", "Élise")
        r.service.mark_elsewhere(rid, admin(), "borrow_elsewhere", link=SHOP)
        assert r.ports.acquired == [] and r.ports.follows == [] and r.ports.searches == []


# -- arrival ---------------------------------------------------------------------------------------


def test_import_matches_open_to_buy_request_and_notifies(settings: Settings) -> None:
    with world(settings) as env:
        ports = FakePorts(engine=env.engine)
        service = RequestService(env.engine, env.clock, ports, bus=env.bus)
        asked = service.create(requester(MEMBER_ID, "member"), RequestTarget("free_text", "Élise"))
        service.mark_elsewhere(asked.id, admin(), "to_buy", link=SHOP)
        work = env.work("Elise", age=None)  # the title a file called `Elise.epub` is given
        assert service.arrived_from_import(work) == 1
        arrived = service.get(MEMBER_ID, asked.id)
        assert arrived.state == "arrived"
        with env.engine.connect() as connection:
            row = connection.execute(
                sa.text("SELECT work_id, arrived_at FROM request WHERE id = :i"), {"i": asked.id}
            ).one()
        assert row.work_id == work and row.arrived_at is not None
        env.at(10)
        env.run()
        assert env.sink.to(MEMBER_ID), "the requester is told"
        assert not env.sink.to(OTHER_ID) and not env.sink.to(CHILD_ID)
        # a channel several people read: no title, in any case
        assert "elise" not in env.sink.channel().lower()
        assert service.arrived_from_import(work) == 0  # a replay finds nothing to do


def test_import_matches_by_isbn_found_in_the_name(settings: Settings) -> None:
    with rig(settings) as r:
        rid = ask(r, MEMBER_ID, "member", "isbn", ISBN)
        to_buy(r, rid)
        assert r.service.arrived_from_import(set_title(r, "Un livre 978-0-306-40615-7")) == 1
        assert r.state_of(rid) == "arrived"


def test_import_never_matches_what_is_not_an_open_elsewhere_request(settings: Settings) -> None:
    with rig(settings) as r:
        pending = ask(r, MEMBER_ID, "member", "free_text", "Élise Trois")  # nobody said "to buy"
        cancelled = ask(r, MEMBER_ID, "member", "free_text", "Élise")
        to_buy(r, cancelled)
        r.service.cancel(MEMBER_ID, cancelled)
        other_title = ask(r, OTHER_ID, "member", "free_text", "Élise Deux")
        to_buy(r, other_title)
        assert r.service.arrived_from_import(set_title(r, "Elise")) == 0
        assert [r.state_of(i) for i in (pending, cancelled, other_title)] == [
            "pending", "cancelled", "to_buy",
        ]  # fmt: skip


def test_each_requester_is_matched_alone(settings: Settings) -> None:
    with rig(settings) as r:
        mine = ask(r, MEMBER_ID, "member", "free_text", "Élise")
        theirs = ask(r, OTHER_ID, "member", "isbn", ISBN)
        to_buy(r, mine)
        to_buy(r, theirs)
        assert r.service.arrived_from_import(set_title(r, "ELISE")) == 1
        assert (r.state_of(mine), r.state_of(theirs)) == ("arrived", "to_buy")


def test_title_match_folds_unicode_not_ascii_only(settings: Settings) -> None:
    with rig(settings) as r:
        accented = ask(r, MEMBER_ID, "member", "free_text", "Élise")
        cjk = ask(r, OTHER_ID, "member", "free_text", "日本語")
        to_buy(r, accented)
        to_buy(r, cjk)
        # an ASCII reduction would make "日本語" and "中文" the same empty title
        assert r.service.arrived_from_import(set_title(r, "中文")) == 0
        assert r.service.arrived_from_import(set_title(r, "ÉLISE")) == 1
        assert r.state_of(cjk) == "to_buy"
        # the same fold resolves a typed text to the Work the catalogue holds (the debt of L35)
        held = ask(r, MEMBER_ID, "member", "free_text", "zoé  ")
        r.work(title="ZOE", sort_title="zoe")
        assert r.service.preview(held, admin()).resolved is True


# -- proposals (D22) -------------------------------------------------------------------------------


def hits(count: int) -> list[SourceRef]:
    return [SourceRef("alpha", f"r{i}", f"Result {i}", {"entry": f"e{i}"}) for i in range(count)]


def test_unresolved_free_text_gets_source_search_proposals(settings: Settings) -> None:
    ports = FakePorts(search_hits=hits(8))
    with rig(settings, ports=ports) as r:
        rid = ask(r, MEMBER_ID, "member", "free_text", "Un titre que personne n'a")
        found = r.service.propose(rid, admin())
        assert len(found) == 5 and all(isinstance(p, Proposal) for p in found)
        assert ports.searches == ["Un titre que personne n'a"]
        # proposals only: nothing followed, nothing acquired, nothing decided
        assert r.state_of(rid) == "pending"
        assert ports.follows == [] and ports.acquired == []
        assert r.rows("SELECT target_kind FROM request WHERE id = :i", i=rid)[0][0] == "free_text"
        assert r.service.propose(rid, admin()) == found  # asked twice: one search, same list
        assert ports.searches == ["Un titre que personne n'a"]
        assert [p.id for p in r.service.proposals(rid, admin())] == [p.id for p in found]


def test_an_isbn_unknown_to_the_catalogue_is_searched_as_well(settings: Settings) -> None:
    ports = FakePorts(search_hits=hits(2))
    with rig(settings, ports=ports) as r:
        rid = ask(r, MEMBER_ID, "member", "isbn", ISBN)
        assert len(r.service.propose(rid, admin())) == 2
        assert ports.searches == [ISBN]


def test_a_request_the_catalogue_resolves_is_not_searched(settings: Settings) -> None:
    ports = FakePorts(search_hits=hits(3))
    with rig(settings, ports=ports) as r:
        r.work(title="Élise", sort_title="elise")
        rid = ask(r, MEMBER_ID, "member", "free_text", "elise")
        with pytest.raises(RequestRefusedError) as refused:
            r.service.propose(rid, admin())
        assert refused.value.code == "nothing_to_propose"
        assert ports.searches == []


def test_a_proposal_is_applied_only_when_the_admin_accepts_it(settings: Settings) -> None:
    ports = FakePorts(search_hits=hits(3), kinds={"alpha": "manga"})
    with rig(settings, ports=ports) as r:
        rid = ask(r, MEMBER_ID, "member", "free_text", "Un titre que personne n'a")
        first = r.service.propose(rid, admin())[0]
        accepted = r.service.accept_proposal(rid, first.id, admin())
        assert accepted.state == "pending" and accepted.target_kind == "source_ref"
        assert ports.follows == []  # accepting a proposal is not approving the request
        r.service.approve(rid, admin())
        assert ports.follows and ports.follows[0][:2] == ("alpha", first.remote_id)
        assert len(r.audit("request.proposal_accepted")) == 1


def test_a_failing_search_proposes_nothing_and_changes_nothing(settings: Settings) -> None:
    ports = FakePorts(search_error=RuntimeError("source down"))
    with rig(settings, ports=ports) as r:
        rid = ask(r, MEMBER_ID, "member", "free_text", "Un titre")
        assert r.service.propose(rid, admin()) == []
        assert r.state_of(rid) == "pending"


def test_child_request_never_triggers_federated_search(settings: Settings) -> None:
    ports = FakePorts(search_hits=hits(3))
    with rig(settings, ports=ports) as r:
        asked = r.service.create_from_ref(requester(CHILD_ID, "child"), SourceRef(
            "alpha", "r1", "A Quiet Tale", {"entry": "e1"}))  # fmt: skip
        r.service.preview(asked.id, admin())
        with pytest.raises(RequestRefusedError):  # a source_ref has nothing to propose
            r.service.propose(asked.id, admin())
        # a text smuggled into a child's row (a write by another door) is never searched either
        with r.engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO request (id, requester_user_id, target_kind, target_value, state)"
                    " VALUES ('01J0000000000000000000FREE', :c, 'free_text', 'secret', 'pending')"
                ), {"c": CHILD_ID},
            )  # fmt: skip
        with pytest.raises(RequestRefusedError) as refused:
            r.service.propose("01J0000000000000000000FREE", admin())
        assert refused.value.code == "not_allowed"
        assert ports.searches == []
        assert r.service.mine(CHILD_ID)  # and she still sees her own requests, a state apiece


# -- attacks ---------------------------------------------------------------------------------------


def test_the_request_of_someone_else_is_an_absent_one(settings: Settings) -> None:
    with rig(settings) as r:
        rid = ask(r, MEMBER_ID, "member", "free_text", "Élise")
        to_buy(r, rid)
        for read in (
            lambda: r.service.get(OTHER_ID, rid),
            lambda: r.service.cancel(OTHER_ID, rid),
            lambda: r.service.get(CHILD_ID, rid),
        ):
            with pytest.raises(RequestRefusedError) as refused:
                read()
            assert refused.value.code == "not_found"
        assert r.state_of(rid) == "to_buy"
        assert all(v.id != rid for v in r.service.mine(OTHER_ID))


def test_only_an_admin_session_decides_what_happens_elsewhere(settings: Settings) -> None:
    ports = FakePorts(search_hits=hits(2))
    with rig(settings, ports=ports) as r:
        rid = ask(r, MEMBER_ID, "member", "free_text", "Élise")
        found = admin_proposals(r, ask(r, MEMBER_ID, "member", "free_text", "Autre titre"))
        liar = (
            principal(MEMBER_ID, "member"),
            principal(CHILD_ID, "child"),
            principal(ADMIN_ID, "admin", can_rate=True, credential=Credential.API_KEY),  # a key
            principal(ADMIN_ID, "admin", credential=Credential.API_KEY),  # a key that only reads
        )
        for who in liar:
            for act in (
                lambda who=who: r.service.mark_elsewhere(rid, who, "to_buy", link=SHOP),
                lambda who=who: r.service.propose(rid, who),
                lambda who=who: r.service.proposals(rid, who),
                lambda who=who: r.service.accept_proposal(rid, found[0].id, who),
            ):
                with pytest.raises(RequestRefusedError) as refused:
                    act()
                assert refused.value.code == "not_allowed"
        assert r.state_of(rid) == "pending" and ports.searches == ["Autre titre"]


def admin_proposals(r: Rig, request_id: str) -> list[Proposal]:
    return r.service.propose(request_id, admin())


def test_a_proposal_of_another_request_is_not_accepted_here(settings: Settings) -> None:
    ports = FakePorts(search_hits=hits(2))
    with rig(settings, ports=ports) as r:
        one = ask(r, MEMBER_ID, "member", "free_text", "Un")
        two = ask(r, OTHER_ID, "member", "free_text", "Deux")
        proposal = admin_proposals(r, one)[0]
        with pytest.raises(RequestRefusedError) as refused:
            r.service.accept_proposal(two, proposal.id, admin())
        assert refused.value.code == "not_found"
        assert r.rows("SELECT target_kind FROM request WHERE id = :i", i=two)[0][0] == "free_text"


def test_a_child_is_shown_no_link_and_no_state_of_hers_leaks(settings: Settings) -> None:
    with rig(settings) as r:
        asked = r.service.create_from_ref(
            requester(CHILD_ID, "child"), SourceRef("alpha", "r1", "A Quiet Tale", {"entry": "e1"})
        )
        shown: Any = r.service.mark_elsewhere(asked.id, admin(), "to_buy", link=SHOP)
        assert shown.elsewhere_url == SHOP  # the admin sees what he set
        hers = r.service.get(CHILD_ID, asked.id)
        assert hers.elsewhere_url is None
        assert r.service.mine(CHILD_ID)[0].elsewhere_url is None


def test_the_search_port_keeps_the_first_edition_of_each_work(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from shelfhold.events.bus import EventBus
    from shelfhold.ops import requests as ops_requests

    asked: list[tuple[str, str]] = []

    def edition(key: str, rid: str, title: str) -> Any:
        return SimpleNamespace(source_key=key, remote_id=rid, title=title, memo={"entry": rid})

    class FakeAcquire:
        def __init__(self, comp: Any) -> None:
            pass

        def federated_search(self, query: str, *, owner: str) -> Any:
            asked.append((query, owner))
            groups = [
                SimpleNamespace(editions=(edition("a", "1", "One"), edition("b", "2", "One"))),
                SimpleNamespace(editions=()),  # a group with no Edition names nothing
                SimpleNamespace(editions=(edition("b", "3", "Two"),)),
            ]
            return SimpleNamespace(items=groups)

    monkeypatch.setattr(ops_requests, "AcquireOps", FakeAcquire)
    with rig(settings) as r:
        comp = SimpleNamespace(
            engine=r.engine, clock=r.clock, bus=EventBus(r.clock),
            settings=SimpleNamespace(fresh_auth_s=900),
        )  # fmt: skip
        found = ops_requests.build_ports(comp).search("Élise", owner=ADMIN_ID)
    assert asked == [("Élise", ADMIN_ID)]
    assert [(s.source_key, s.remote_id, s.title) for s in found] == [
        ("a", "1", "One"),
        ("b", "3", "Two"),
    ]
