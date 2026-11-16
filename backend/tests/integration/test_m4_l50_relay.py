"""M4 L50: the relay from the e-reader to Kavita and back, through a block of a series
(ADR-0053 §2 and §5 and its amendment of 2026-11-16; EF-LEC-009, EF-LEC-014, EF-EXP-026).

The whole chain is one test: a `PUT` of KOReader on a part of the block, the synchronisation job's
pass with the member's own key against a simulated Kavita (`tests.relay_env`), and the `GET` the
e-reader makes afterwards. Contracts that are already fixed and are held here: an echo pushed to
Kavita is never read back as « read »; the push is capped two pages before the end, with no
crossing; the crossing is made in the frame of the block; one note per linked Kavita account; the
state of a member's page map is `candidate` or `unverified` until Kavita announces the same number
of pages, then `verified` (only then is anything relayed).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from structlog.testing import capture_logs

from shelfhold.config import Settings
from tests.bundle_env import Chapter, Exported, page
from tests.integration.test_l47_amendments import _back_by, floor4
from tests.integration.test_l47_kosync import block, positions, pull, push
from tests.integration.test_l48_opds import World, make_world
from tests.relay_env import Relay, build_relay

pytestmark = pytest.mark.req("EF-LEC-009", "EF-LEC-014", "EF-EXP-026", "EF-USR-006")

HEALTH = "/api/v1/health/page"
PAGES = 10


@pytest.fixture
def lab(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with make_world(monkeypatch, settings) as made:
        yield made


def three(
    lab: World, *, pages: int = PAGES, link: tuple[str, ...] = ("mum",)
) -> tuple[Relay, list[Exported], str]:
    """Three chapters of `pages` pages, held by Kavita, the block of `mum` and its digest."""
    relay = build_relay(lab)
    items = [relay.add_chapter(Chapter.simple(str(n), count=pages)) for n in (1, 2, 3)]
    for name in link:
        relay.link(name)
    digest, _ = block(lab, lab.mum)
    return relay, items, digest


def put(lab: World, digest: str, at: int, total: int = 3 * PAGES, reader: Any = None) -> Any:
    answer = push(reader or lab.mum, digest, str(at), floor4(at, total))
    assert answer.status_code == 200, answer.text
    return answer


def progress_of(lab: World, digest: str, reader: Any = None) -> dict[str, Any]:
    answer = pull(reader or lab.mum, digest)
    assert answer.status_code == 200, answer.text
    return answer.json()  # type: ignore[no-any-return]


# -- 1. the chain, both ways -----------------------------------------------------------------


def test_a_put_on_a_part_of_the_block_reaches_kavita_on_the_right_chapter_and_page(
    lab: World,
) -> None:
    relay, items, digest = three(lab)
    put(lab, digest, 15)  # the 5th page of the 2nd chapter
    assert relay.sync_all()["mum"].code == "ok"
    assert [relay.kavita_page("mum", i) for i in items] == [0, 5, 0]


def test_a_read_through_the_block_marks_the_chapters_and_kavita_is_told_read_for_each(
    lab: World,
) -> None:
    relay, items, digest = three(lab)
    put(lab, digest, 1)
    _back_by(lab, timedelta(hours=1))
    put(lab, digest, 25)  # chapters 1 and 2 crossed whole, the 3rd in progress at its 5th page
    relay.sync_all()
    assert [relay.kavita_page("mum", i) for i in items] == [PAGES, PAGES, 5]
    assert [relay.unit_state("mum", i) for i in items] == ["read", "read", "in_progress"]


def test_a_jump_to_the_end_of_the_block_tells_kavita_no_chapter_is_read(lab: World) -> None:
    relay, items, digest = three(lab)
    put(lab, digest, 1)
    _back_by(lab, timedelta(seconds=5))
    put(lab, digest, 30)  # a jump in five seconds: no chapter crossed
    relay.sync_all()
    assert relay.kavita_page("mum", items[0]) <= PAGES - 2
    assert relay.kavita_page("mum", items[1]) == 0
    assert (
        relay.kavita_page("mum", items[2]) == PAGES - 2
    )  # the look at the end: capped, no crossing
    assert [relay.unit_state("mum", i) for i in items] == ["in_progress", "unread", "in_progress"]


def test_a_state_of_kavita_comes_back_by_a_get_in_the_frame_of_the_block(lab: World) -> None:
    relay, items, digest = three(lab)
    relay.read_in_kavita("mum", items[1], 6)
    relay.sync_all()
    got = progress_of(lab, digest)
    assert got["progress"] == str(PAGES + 6) and got["device"] == "Kavita"
    assert got["percentage"] == pytest.approx((PAGES + 6) / (3 * PAGES))


def test_a_look_at_the_last_page_pushed_to_kavita_does_not_come_back_as_read(lab: World) -> None:
    relay, items, digest = three(lab)
    put(lab, digest, 2 * PAGES)  # the last page of the 2nd chapter, with no crossing
    for _ in range(3):
        relay.clock.advance(timedelta(minutes=10))
        relay.sync_all()
    assert relay.kavita_page("mum", items[1]) == PAGES - 2
    assert relay.unit_state("mum", items[1]) != "read"
    assert progress_of(lab, digest)["progress"] == str(2 * PAGES)  # the e-reader's own page


def test_many_passes_neither_loop_nor_oscillate(lab: World) -> None:
    relay, items, digest = three(lab)
    put(lab, digest, 15)
    for _ in range(4):
        relay.clock.advance(timedelta(minutes=10))
        relay.sync_all()
    assert len(relay.writes("mum")) == 1  # pushed once, its echo never pushed again
    assert len(relay.positions("mum", "server")) == 0  # and never taken back as news
    assert progress_of(lab, digest)["progress"] == "15"
    relay.read_in_kavita("mum", items[1], 8)  # Kavita goes on alone
    for _ in range(3):
        relay.clock.advance(timedelta(minutes=10))
        relay.sync_all()
    assert progress_of(lab, digest)["progress"] == str(PAGES + 8)
    assert len(relay.writes("mum")) == 1  # nothing written back to Kavita for what it said
    assert len(relay.positions("mum", "server")) == 1
    assert len(relay.positions("mum", "device")) == 1


def test_the_furthest_wins_when_both_sides_moved_and_nobody_goes_back(lab: World) -> None:
    relay, items, digest = three(lab)
    relay.read_in_kavita("mum", items[1], 8)
    put(lab, digest, 13)  # behind Kavita on the same chapter
    relay.sync_all()
    assert relay.kavita_page("mum", items[1]) == 8  # never moved back
    assert progress_of(lab, digest)["progress"] == str(PAGES + 8)


# -- 2. the page map: only a verified member is relayed -------------------------------------


def test_a_member_is_candidate_until_kavita_announces_the_same_number_of_pages(lab: World) -> None:
    relay, _items, _digest = three(lab)
    assert set(relay.states().values()) == {"candidate"}
    relay.sync_all()
    assert set(relay.states().values()) == {"verified"}
    assert {r.kavita_pages for r in relay.member_rows()} == {PAGES}


def test_without_the_check_of_the_page_map_nothing_of_a_block_is_pushed(lab: World) -> None:
    relay, _items, digest = three(lab)
    put(lab, digest, 15)
    relay.sync_all(verify=False)
    assert relay.writes("mum") == []  # fails closed


def test_a_chapter_kavita_counts_otherwise_gets_nothing_and_the_others_are_relayed(
    lab: World,
) -> None:
    relay, items, digest = three(lab)
    relay.set_kavita_pages(items[1], PAGES + 1)  # Kavita counts one more page
    put(lab, digest, 5)
    put(lab, digest, 15)
    with capture_logs() as logs:
        outcome = relay.sync_all()["mum"]
    assert outcome.code == "ok" and outcome.unverified == 1
    assert relay.kavita_page("mum", items[0]) == 5
    assert relay.kavita_page("mum", items[1]) == 0  # nothing for this chapter
    states = relay.states()
    assert states[items[1].item_id] == "unverified" and states[items[0].item_id] == "verified"
    assert {r.kavita_pages for r in relay.member_rows() if r.rank == 2} == {PAGES + 1}
    assert any(e.get("code") == "page_count_mismatch" for e in logs)


def test_a_mismatch_is_in_the_health_page_with_a_closed_code(lab: World) -> None:
    relay, items, digest = three(lab)
    relay.set_kavita_pages(items[1], PAGES + 1)
    relay.sync_all()
    assert lab.env.login().status_code == 200
    page_ = lab.env.client.get(HEALTH).json()
    found = [w for w in page_["page_map_warnings"] if w["code"] == "relay.page_count_mismatch"]
    assert len(found) == 1 and found[0]["chapters"] == 1
    assert found[0]["subject"] == lab.seed.editions["ok"]
    assert "Series" not in str(found) and digest not in str(page_)  # no title, no digest


def test_a_member_comes_back_to_verified_when_kavita_counts_the_same_again(lab: World) -> None:
    relay, items, digest = three(lab)
    relay.set_kavita_pages(items[1], PAGES + 1)
    relay.sync_all()
    relay.set_kavita_pages(items[1], PAGES)
    put(lab, digest, 15)
    relay.clock.advance(timedelta(minutes=10))
    relay.sync_all()
    assert relay.states()[items[1].item_id] == "verified"
    assert relay.kavita_page("mum", items[1]) == 5


def test_a_chapter_whose_page_names_kavita_would_sort_otherwise_is_never_relayed(
    lab: World,
) -> None:
    relay = build_relay(lab)
    odd = Chapter(
        "1", [(f"{n}.png", page("png", f"p{n}")) for n in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)]
    )
    item = relay.add_chapter(odd)  # `1.png`, `10.png`: a sort by name puts 10 second
    relay.link("mum")
    digest, _ = block(lab, lab.mum)
    put(lab, digest, 5, total=10)
    relay.sync_all()
    assert relay.states()[item.item_id] == "unverified"  # the counts agree, the order may not
    assert relay.kavita_page("mum", item) == 0 and relay.writes("mum") == []


def test_a_state_of_kavita_on_an_unverified_chapter_is_not_given_back_in_the_block(
    lab: World,
) -> None:
    relay, items, digest = three(lab)
    relay.set_kavita_pages(items[1], PAGES + 1)
    relay.read_in_kavita("mum", items[1], 6)
    relay.sync_all()
    assert pull(lab.mum, digest).json() == {}  # no page invented in a frame that is not verified


def test_the_page_map_of_one_chapter_never_cuts_the_relay_of_a_plain_file(lab: World) -> None:
    """A position on the chapter's own file (not through the block) is Kavita's own page counting:
    the state of the block's member does not hold it."""
    relay, items, _digest = three(lab)
    relay.set_kavita_pages(items[1], PAGES + 1)
    relay.sync_all()
    user = relay.user_id("mum")
    from shelfhold.reading.public import record_position

    with relay.engine.begin() as c:
        anchor = c.execute(
            sa.text("SELECT content_hash FROM document_alias WHERE export_item_id = :i"),
            {"i": items[1].item_id},
        ).scalar_one()
        record_position(
            c, reader_id=user, subject_id=user, carrier_type="export_item",
            carrier_id=items[1].item_id, content_hash=anchor, locator={"page": 4}, percent=0.4,
            client_ts=relay.clock.now(),
        )  # fmt: skip
    relay.clock.advance(timedelta(minutes=10))
    relay.sync_all()
    assert relay.kavita_page("mum", items[1]) == 4


# -- 3. what L47 left: a chapter replaced since the version ---------------------------------


def test_a_position_on_a_replaced_chapter_is_transposed_stored_and_never_relayed(
    lab: World,
) -> None:
    relay, items, digest = three(lab)
    relay.replace_chapter(items[1], Chapter.simple("2", count=PAGES, ext="jpg"))
    with capture_logs() as logs:
        put(lab, digest, 15)
    kept = relay.positions("mum", "device")
    assert len(kept) == 1 and kept[0]["carrier_id"] == items[1].item_id
    assert "page" not in kept[0]["locator"] and kept[0]["locator"]["transposed"] is True
    assert kept[0]["percent"] == pytest.approx(0.5)  # the share of the chapter, not a page of it
    assert any(e.get("event") == "kosync.transposed" for e in logs)
    assert relay.unit_state("mum", items[1]) == "in_progress"
    relay.sync_all()
    assert relay.writes("mum") == []  # a guess is never written to Kavita
    assert pull(lab.mum, digest).json() == {}  # and never given back as a page of the block


def test_a_purged_chapter_still_stores_nothing(lab: World) -> None:
    relay, items, digest = three(lab)
    with relay.engine.begin() as c:
        c.execute(sa.text("DELETE FROM export_item WHERE id = :i"), {"i": items[1].item_id})
    put(lab, digest, 15)
    assert positions(lab) == []


# -- 4. a child ------------------------------------------------------------------------------


def _lower_ceiling(lab: World, name: str, rating: int) -> None:
    with lab.seed.engine.begin() as c:
        c.execute(sa.text("UPDATE user SET max_age_rating = :r WHERE username = :n"),
                  {"r": rating, "n": name})  # fmt: skip


def test_nothing_above_the_ceiling_of_a_child_goes_either_way(lab: World) -> None:
    relay = build_relay(lab)
    items = [relay.add_chapter(Chapter.simple(str(n), count=PAGES)) for n in (1, 2, 3)]
    relay.link("kid")
    digest, _ = block(lab, lab.kid)
    put(lab, digest, 15, reader=lab.kid)
    relay.read_in_kavita("kid", items[2], 6)
    _lower_ceiling(lab, "kid", 5)  # the Edition (6+) is now above the ceiling
    put(lab, digest, 16, reader=lab.kid)  # taken for nothing
    outcome = relay.sync_all()["kid"]
    assert relay.calls_of("kid") == []  # not even asked about
    assert relay.writes("kid") == [] and outcome.hidden == 3
    assert relay.positions("kid", "server") == []  # what Kavita holds was never stored
    assert pull(lab.kid, digest).json() == {}
    assert len(relay.positions("kid", "device")) == 1  # only what was taken under the ceiling


def test_a_child_reads_back_in_the_block_only_what_is_under_the_ceiling(lab: World) -> None:
    relay = build_relay(lab)
    items = [relay.add_chapter(Chapter.simple(str(n), count=PAGES)) for n in (1, 2, 3)]
    relay.link("kid")
    digest, _ = block(lab, lab.kid)
    relay.read_in_kavita("kid", items[1], 6)
    relay.sync_all()
    assert progress_of(lab, digest, lab.kid)["progress"] == str(PAGES + 6)
    _lower_ceiling(lab, "kid", 5)
    assert pull(lab.kid, digest).json() == {}


# -- 5. attacks ------------------------------------------------------------------------------


def test_idor_the_block_of_another_profile_relays_nothing_and_looks_unknown(lab: World) -> None:
    relay, _items, digest = three(lab, link=("mum", "kid"))
    other = push(lab.kid, digest, "15", floor4(15, 30))  # the child sends mum's digest
    unknown = push(lab.kid, "0123456789abcdef0123456789abcdef", "15", floor4(15, 30))
    assert other.status_code == unknown.status_code == 200
    assert set(other.json()) == set(unknown.json())
    assert relay.positions("kid") == [] and pull(lab.kid, digest).json() == {}
    relay.sync_all()
    assert relay.writes() == []


def test_a_profile_pushes_only_its_own_positions_to_its_own_account(lab: World) -> None:
    relay = build_relay(lab)
    items = [relay.add_chapter(Chapter.simple(str(n), count=PAGES)) for n in (1, 2, 3)]
    relay.link("mum")
    relay.link("kid")
    mum_digest, _ = block(lab, lab.mum)
    put(lab, mum_digest, 15)
    relay.sync_all()
    assert relay.kavita_page("mum", items[1]) == 5
    assert relay.kavita_page("kid", items[1]) == 0
    assert relay.writes("kid") == []


def test_the_token_of_a_child_never_reads_the_state_kavita_holds_for_another_profile(
    lab: World,
) -> None:
    relay, items, digest = three(lab, link=("mum", "kid"))
    relay.read_in_kavita("mum", items[1], 6)
    relay.sync_all()
    assert progress_of(lab, digest)["progress"] == str(PAGES + 6)
    assert pull(lab.kid, digest).json() == {}


def test_a_kavita_account_two_profiles_share_is_synchronised_for_neither(lab: World) -> None:
    """The same Kavita account linked by two profiles (on two bindings of one Kavita): what is
    read there belongs to neither, so nothing is written to it and nothing is taken from it."""
    relay = build_relay(lab)
    items = [relay.add_chapter(Chapter.simple(str(n), count=PAGES)) for n in (1, 2, 3)]
    other = relay.second_destination()
    relay.link("mum", "family")
    relay.link("kid", "family", destination=other)
    digest, _ = block(lab, lab.mum)
    put(lab, digest, 15)
    relay.read_in_kavita("mum", items[0], 7)
    assert relay.sync("mum").code == "account_shared"
    assert relay.sync("kid", destination=other).code == "account_shared"
    assert relay.writes() == [] and relay.positions("mum", "server") == []
    assert progress_of(lab, digest)["progress"] == "15"  # the e-reader's own page, unmoved


def test_a_key_revoked_during_a_pass_is_not_used_for_the_next_chapter(lab: World) -> None:
    relay, items, digest = three(lab)
    put(lab, digest, 5)
    put(lab, digest, 15)
    done: list[str] = []

    def revoke_after_the_first_write(request: Any) -> None:
        if request.method == "POST" and "/reader/progress" in request.path.lower() and not done:
            done.append(request.path)
            relay.revoke("mum")

    relay.fake.hook = revoke_after_the_first_write
    outcome = relay.sync_all()["mum"]
    assert outcome.code == "member_key_revoked"
    assert len(relay.writes()) == 1  # the write in flight, none after the revocation
    assert [relay.kavita_page("mum", i) for i in items].count(
        0
    ) == 2  # one chapter written, no more


def test_a_revoked_key_leaves_the_positions_local_and_served(lab: World) -> None:
    relay, _items, digest = three(lab)
    relay.revoke("mum")
    put(lab, digest, 15)
    assert relay.sync_all() == {}  # a member without a live key is not even visited
    assert relay.sync("mum").code == "member_key_revoked"
    assert relay.writes() == [] and relay.calls_of("mum") == []
    assert progress_of(lab, digest)["progress"] == "15"


def test_a_device_token_never_reaches_the_synchronisation(lab: World) -> None:
    """No route starts a pass: a token, a key or a session gets a 404 or 405, never a pass."""
    for client_get in (lab.mum.get, lab.kid.get):
        for path in ("/api/v1/progress/sync", "/kosync/sync", "/api/v1/reading/sync"):
            assert client_get(path).status_code in (401, 403, 404, 405)


# -- 6. what L47 left: the warning of the unknown documents ---------------------------------


def test_unknown_documents_of_a_token_are_in_the_health_page_without_the_digest(lab: World) -> None:
    unknown = "0123456789abcdef0123456789abcdef"
    for _ in range(3):
        assert push(lab.mum, unknown, "2", 0.2).status_code == 200
    assert lab.env.login().status_code == 200
    page_ = lab.env.client.get(HEALTH).json()
    found = [w for w in page_["kosync_unknown"] if w["code"] == "kosync.unknown"]
    assert found == [{"code": "kosync.unknown", "token": lab.mum.public_id, "count": 3}]
    assert unknown not in str(page_) and lab.mum.secret not in str(page_)


def test_the_unknown_documents_are_for_the_administrator_alone(lab: World) -> None:
    push(lab.mum, "0123456789abcdef0123456789abcdef", "2", 0.2)
    for reader in (lab.mum, lab.kid):
        denied = reader.get(HEALTH)
        assert denied.status_code in (401, 403) and "kosync" not in denied.text
