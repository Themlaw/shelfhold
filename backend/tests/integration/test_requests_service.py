"""M3 L35 (EF-USR-010, EF-SRC-029, ENF-OBS-003): the request service over a real database.

The ports (a source, the connected service, the shelves) are fakes that count what they were
asked. Every "never" of the card is an attack here: a child naming a Work, a request of someone
else, an approval without the right, a replay that would follow twice, a hidden title that leaks
through a reason, a delay or an error."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.requests import (
    Estimate,
    RequestRefusedError,
    SourceRef,
)
from shelfhold.acquisition.domain.request_target import RequestTarget
from shelfhold.config import Settings
from shelfhold.identity.public import Credential
from tests.request_env import (
    ADMIN_ID,
    CHILD_ID,
    MEMBER_ID,
    OTHER_ID,
    SIBLING_ID,
    FakePorts,
    Rig,
    admin,
    bind_work,
    principal,
    requester,
    rig,
)

REF = SourceRef(source_key="alpha", remote_id="r1", title="A Quiet Tale", memo={"entry": "e1"})
ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def code_of(excinfo: pytest.ExceptionInfo[RequestRefusedError]) -> str:
    return excinfo.value.code


def ask(r: Rig, who: str = CHILD_ID, role: str = "child", ref: SourceRef = REF) -> str:
    return r.service.create_from_ref(requester(who, role), ref).id


# -- polymorphic target, resolved at approval ------------------------------------------------------


@pytest.mark.req("EF-USR-010")
def test_request_target_polymorphic_resolved_at_approval(settings: Settings) -> None:
    with rig(settings) as r:
        work = r.work()
        by_work = r.service.create(requester(MEMBER_ID, "member"), RequestTarget("work_id", work))
        by_ref = ask(r, MEMBER_ID, "member")
        by_isbn = r.service.create(
            requester(MEMBER_ID, "member"), RequestTarget("isbn", "9780306406157")
        )
        by_text = r.service.create(
            requester(MEMBER_ID, "member"), RequestTarget("free_text", "some title")
        )
        rows = r.rows("SELECT id, target_kind, target_value, state FROM request ORDER BY id")
        assert {row.target_kind for row in rows} == {"work_id", "source_ref", "isbn", "free_text"}
        assert {row.state for row in rows} == {"pending"}
        # the remote result went through `pending_source_item`: the request keeps its identifier
        (item,) = r.rows("SELECT id, source_key, series_ref, title FROM pending_source_item")
        source_row = next(x for x in rows if x.target_kind == "source_ref")
        assert source_row.target_value == item.id and item.series_ref == "r1"
        # nothing became a Work by being asked (the Work of `by_work` is the one that was there)
        assert r.rows("SELECT count(*) FROM work")[0][0] == 1
        assert {by_work.id, by_isbn.id, by_text.id, by_ref} == {x.id for x in rows}
        # asking twice for the same thing is one request, not two
        again = r.service.create(requester(MEMBER_ID, "member"), RequestTarget("work_id", work))
        assert again.id == by_work.id
        assert r.rows("SELECT count(*) FROM request")[0][0] == 4


@pytest.mark.req("EF-USR-010")
def test_request_for_an_unknown_work_is_refused_without_saying_more(settings: Settings) -> None:
    with rig(settings) as r:
        with pytest.raises(RequestRefusedError) as unknown:
            r.service.create(requester(MEMBER_ID, "member"), RequestTarget("work_id", ULID))
        assert code_of(unknown) == "not_found"
        assert r.rows("SELECT count(*) FROM request")[0][0] == 0


@pytest.mark.req("EF-USR-010", "EF-USR-014")
def test_child_cannot_ask_by_work_isbn_or_text(settings: Settings) -> None:
    with rig(settings) as r:
        hidden = r.work()  # a Work that exists and that she may not see
        for target in (
            RequestTarget("work_id", hidden),
            RequestTarget("work_id", ULID),  # and one that does not exist: the same answer
            RequestTarget("isbn", "9780306406157"),
            RequestTarget("free_text", "anything"),
        ):
            with pytest.raises(RequestRefusedError) as refused:
                r.service.create(requester(CHILD_ID, "child"), target)
            assert code_of(refused) == "kind_not_allowed"
        assert r.rows("SELECT count(*) FROM request")[0][0] == 0


@pytest.mark.req("EF-USR-010")
def test_pending_requests_are_capped_per_profile(settings: Settings) -> None:
    with rig(settings) as r:
        for n in range(20):
            ask(r, CHILD_ID, "child", SourceRef("alpha", f"r{n}", f"T{n}", {}))
        with pytest.raises(RequestRefusedError) as capped:
            ask(r, CHILD_ID, "child", SourceRef("alpha", "r99", "T99", {}))
        assert code_of(capped) == "too_many_pending"
        ask(r, SIBLING_ID, "child")  # the cap is per profile


# -- approval -------------------------------------------------------------------------------------


@pytest.mark.req("EF-USR-010", "ENF-OBS-003")
def test_request_approve_one_gesture_notifies_on_arrival(settings: Settings) -> None:
    ports = FakePorts(kinds={"alpha": "novel"}, shelf="SHELF")
    with rig(settings, ports=ports) as r:
        rid = ask(r, MEMBER_ID, "member")
        view = r.service.approve(rid, admin())
        assert view.state == "approved"
        assert ports.acquired == [("alpha", "r1", MEMBER_ID, "private")]
        # edition, shelf: the defaults of the Work that arrived, one gesture
        (work_id, edition_id) = next(iter(r.rows("SELECT work_id, id FROM edition")))
        assert [e[0] for e in ports.filed] == [edition_id] and ports.filed[0][1] == "SHELF"
        assert r.rows("SELECT work_id, edition_id FROM request WHERE id = :i", i=rid)[0] == (
            work_id, edition_id,
        )  # fmt: skip
        # the first unit arrives: the request is `arrived` and the requester hears of it, once
        assert r.service.arrived(work_id) == 1
        assert r.state_of(rid) == "arrived"
        assert r.service.arrived(work_id) == 0  # a replay of the event changes nothing
        events = r.rows("SELECT type, actor_id, visibility, payload FROM event")
        assert [(e.type, e.actor_id, e.visibility) for e in events] == [
            ("ops.request_arrived", MEMBER_ID, "private")
        ]
        assert "Quiet" not in str(events[0].payload)  # an event never carries a title


@pytest.mark.req("EF-USR-010")
def test_arrival_of_another_work_does_not_arrive_the_request(settings: Settings) -> None:
    with rig(settings, ports=FakePorts(kinds={"alpha": "manga"})) as r:
        rid = ask(r, MEMBER_ID, "member")
        r.service.approve(rid, admin())
        with r.engine.begin() as connection:
            stranger = bind_work(connection, "alpha", "other")
        assert r.service.arrived(stranger) == 0 and r.state_of(rid) == "approved"
        with r.engine.begin() as connection:
            ours = bind_work(connection, "alpha", "r1")
        assert r.service.arrived(ours) == 1


@pytest.mark.req("EF-USR-010")
def test_pending_request_never_arrives(settings: Settings) -> None:
    """A book that is not approved cannot be 'arrived' by an event about the same source."""
    with rig(settings) as r:
        rid = ask(r, MEMBER_ID, "member")
        with r.engine.begin() as connection:
            work = bind_work(connection, "alpha", "r1")
        assert r.service.arrived(work) == 0 and r.state_of(rid) == "pending"


@pytest.mark.req("EF-USR-010", "ENF-OBS-003")
def test_child_request_requires_age_on_approval(settings: Settings) -> None:
    ports = FakePorts(kinds={"alpha": "novel"})
    with rig(settings, ports=ports) as r:
        rid = ask(r)
        with pytest.raises(RequestRefusedError) as refused:
            r.service.approve(rid, admin())
        assert code_of(refused) == "choose_age"
        # nothing moved: no acquisition, no follow, the request waits, no approval audited
        assert ports.acquired == [] and ports.follows == [] and r.state_of(rid) == "pending"
        assert r.audit("request.approved") == []
        # an age, and the Work that arrives is rated with it: the approval is the rating
        r.service.approve(rid, admin(), age=10)
        work_id = r.rows("SELECT work_id FROM request WHERE id = :i", i=rid)[0][0]
        assert r.rows("SELECT age_rating, age_x, rated_at FROM work WHERE id = :w", w=work_id)[0][
            :2
        ] == (10, 0)
        assert r.rows("SELECT rated_at FROM work WHERE id = :w", w=work_id)[0][0] is not None
        assert [row.target.split(";")[0] for row in r.audit("rate")] == [f"work:{work_id}"]


@pytest.mark.req("EF-USR-010")
def test_child_request_for_a_manga_rates_the_work_when_it_appears(settings: Settings) -> None:
    """A followed series becomes a Work later: the age chosen at approval is applied then, before
    the child could see it, and only for the request that was approved with it."""
    ports = FakePorts(kinds={"alpha": "manga"})
    with rig(settings, ports=ports) as r:
        rid = ask(r)
        r.service.approve(rid, admin(), age=10, x=False)
        assert ports.follows == [("alpha", "r1", CHILD_ID, "household")]
        assert r.rows("SELECT approved_age FROM request WHERE id = :i", i=rid)[0][0] == 10
        with r.engine.begin() as connection:
            work = bind_work(connection, "alpha", "r1")  # the cycle made the Work, unrated
        assert r.rows("SELECT rated_at FROM work WHERE id = :w", w=work)[0][0] is None
        assert r.service.arrived(work) == 1
        row = r.rows("SELECT age_rating, age_x, rated_at FROM work WHERE id = :w", w=work)[0]
        assert row.age_rating == 10 and row.age_x == 0 and row.rated_at is not None
        (rate,) = r.audit("rate")
        assert rate.target.startswith(f"work:{work}")  # the same line as a rating by hand
        assert rate.actor_id.endswith(ADMIN_ID) or rate.actor_id.startswith("session:")


@pytest.mark.req("EF-USR-010", "ENF-OBS-003")
def test_age_of_a_child_request_is_rated_by_nobody_else(settings: Settings) -> None:
    """The deferred rating is that of the approval and of nothing else: a request that was not
    approved with an age never rates a Work, and a rated Work is not touched again."""
    with rig(settings, ports=FakePorts(kinds={"alpha": "manga"})) as r:
        rid = ask(r, MEMBER_ID, "member")  # an adult's request: no age, nothing to apply
        r.service.approve(rid, admin())
        with r.engine.begin() as connection:
            work = bind_work(connection, "alpha", "r1")
        r.service.arrived(work)
        assert r.rows("SELECT rated_at FROM work WHERE id = :w", w=work)[0][0] is None
        assert r.audit("rate") == []


@pytest.mark.req("EF-USR-010", "EF-BIB-036")
def test_approval_needs_the_right_to_rate_and_a_session(settings: Settings) -> None:
    ports = FakePorts(kinds={"alpha": "novel"})
    with rig(settings, ports=ports) as r:
        rid = ask(r)
        cases = [
            principal(MEMBER_ID, "member", can_rate=False),  # no right
            principal(CHILD_ID, "child", can_rate=True),  # a child never rates
            principal(OTHER_ID, "member", can_rate=True, credential=Credential.API_KEY),
            principal(ADMIN_ID, "admin", can_rate=True, credential=Credential.API_KEY),
            principal(ADMIN_ID, "admin", can_rate=True, fresh=False),  # a stale session
            principal(ADMIN_ID, "root", can_rate=True),  # a role nobody defined
        ]
        for who in cases:
            with pytest.raises(RequestRefusedError) as refused:
                r.service.approve(rid, who, age=10)
            assert code_of(refused) == "not_allowed", who
        assert ports.acquired == [] and ports.follows == [] and r.state_of(rid) == "pending"
        assert r.audit("request.approved") == [] and r.audit("rate") == []


@pytest.mark.req("EF-USR-010")
def test_approval_that_fails_halfway_leaves_the_request_pending(settings: Settings) -> None:
    ports = FakePorts(kinds={"alpha": "manga"}, fail_follow=RuntimeError("service down"))
    with rig(settings, ports=ports) as r:
        rid = ask(r, MEMBER_ID, "member")
        with pytest.raises(RuntimeError):
            r.service.approve(rid, admin())
        assert r.state_of(rid) == "pending" and r.audit("request.approved") == []
        ports.fail_follow = None
        r.service.approve(rid, admin())
        assert r.state_of(rid) == "approved" and len(ports.follows) == 1


@pytest.mark.req("EF-USR-010", "EF-SRC-029")
def test_approved_manga_request_calls_follow_once(settings: Settings) -> None:
    ports = FakePorts(kinds={"alpha": "manga"})
    with rig(settings, ports=ports) as r:
        rid = ask(r, MEMBER_ID, "member")
        r.service.approve(rid, admin())
        assert ports.follows == [("alpha", "r1", MEMBER_ID, "private")] and ports.acquired == []
        for _ in range(3):  # a double click, a replay, a second worker
            with pytest.raises(RequestRefusedError) as again:
                r.service.approve(rid, admin())
            assert code_of(again) == "not_pending"
        assert len(ports.follows) == 1
        # two requests for the same series: each is approved, the service is asked by each, and
        # the follow itself is idempotent (EF-SRC-029): the ports are what de-duplicate
        second = ask(r, OTHER_ID, "member")
        r.service.approve(second, admin())
        assert [f[:2] for f in ports.follows] == [("alpha", "r1")] * 2


@pytest.mark.req("EF-USR-010")
def test_request_size_confirmation_over_threshold(settings: Settings) -> None:
    ports = FakePorts(kinds={"alpha": "manga"}, estimate_of=Estimate(units=101, size_bytes=0))
    with rig(settings, ports=ports) as r:
        rid = ask(r, MEMBER_ID, "member")
        preview = r.service.preview(rid, admin())
        assert (preview.units, preview.size_bytes) == (101, 0)
        assert preview.confirmation_required and not preview.needs_age
        with pytest.raises(RequestRefusedError) as refused:
            r.service.approve(rid, admin())
        assert code_of(refused) == "confirmation_required"
        assert ports.follows == [] and r.state_of(rid) == "pending"
        r.service.approve(rid, admin(), confirm_size=True)
        assert r.state_of(rid) == "approved" and len(ports.follows) == 1
        ports.estimate_of = Estimate(units=1, size_bytes=5 * 2**30 + 1)  # the size, not the count
        second = ask(r, OTHER_ID, "member")
        with pytest.raises(RequestRefusedError):
            r.service.approve(second, admin())
        ports.estimate_of = Estimate(units=100, size_bytes=5 * 2**30)
        r.service.approve(second, admin())  # at the threshold: no confirmation


@pytest.mark.req("EF-USR-010")
def test_approval_preview_prefills_edition_source_and_shelf(settings: Settings) -> None:
    ports = FakePorts(kinds={"alpha": "novel"}, shelf="SHELF")
    with rig(settings, ports=ports) as r:
        work = r.work(editions=False)
        r.rows("SELECT 1")
        with r.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE \"user\" SET locale = 'en' WHERE id = :u"), {"u": MEMBER_ID}
            )
        with r.engine.begin() as connection:
            from tests.integration.test_schema_catalog import add_edition

            fr = add_edition(connection, work, lang="fr")
            en = add_edition(connection, work, lang="en")
        rid = r.service.create(
            requester(MEMBER_ID, "member", "en"), RequestTarget("work_id", work)
        ).id
        preview = r.service.preview(rid, admin())
        assert preview.edition_id == en and preview.edition_id != fr  # the language of the asker
        assert preview.shelf_id == "SHELF" and preview.needs_age is False
        child = r.service.create_from_ref(requester(CHILD_ID, "child"), REF)
        assert r.service.preview(child.id, admin()).needs_age is True


# -- refusal, not found, cancellation, and who may see what ------------------------------


@pytest.mark.req("EF-USR-010")
def test_request_reject_reason_shown(settings: Settings) -> None:
    with rig(settings) as r:
        rid = ask(r, MEMBER_ID, "member")
        r.service.reject(rid, admin(), "  Already in the library under another name  ")
        mine = r.service.get(MEMBER_ID, rid)
        assert mine.state == "rejected"
        assert mine.reject_reason == "Already in the library under another name"
        assert [v.reject_reason for v in r.service.mine(MEMBER_ID)] == [mine.reject_reason]
        assert r.audit("request.rejected")
        # a reason is only on a refusal, and a refusal is final
        with pytest.raises(RequestRefusedError):
            r.service.reject(rid, admin(), "again")
        other = ask(r, OTHER_ID, "member")
        for bad in ("", "   ", "x" * 501, "a\x00b", "a‮b"):
            with pytest.raises(RequestRefusedError) as invalid:
                r.service.reject(other, admin(), bad)
            assert code_of(invalid) == "invalid_reason"
        assert r.state_of(other) == "pending"


@pytest.mark.req("EF-USR-010")
def test_not_found_and_reject_need_an_admin_session(settings: Settings) -> None:
    with rig(settings) as r:
        rid = ask(r, MEMBER_ID, "member")
        for who in (
            principal(MEMBER_ID, "member", can_rate=True),
            principal(CHILD_ID, "child"),
            principal(ADMIN_ID, "admin", credential=Credential.API_KEY),
        ):
            for call in (
                lambda w=who: r.service.reject(rid, w, "no"),
                lambda w=who: r.service.mark_not_found(rid, w),
            ):
                with pytest.raises(RequestRefusedError) as refused:
                    call()
                assert code_of(refused) == "not_allowed"
        assert r.state_of(rid) == "pending"
        r.service.mark_not_found(rid, admin())
        assert r.state_of(rid) == "not_found" and r.audit("request.not_found")


@pytest.mark.req("EF-USR-010")
def test_cancel_is_the_requesters_alone_and_only_while_pending(settings: Settings) -> None:
    with rig(settings, ports=FakePorts(kinds={"alpha": "manga"})) as r:
        rid = ask(r, MEMBER_ID, "member")
        for stranger in (OTHER_ID, CHILD_ID, ADMIN_ID, ULID):
            with pytest.raises(RequestRefusedError) as idor:
                r.service.cancel(stranger, rid)
            assert code_of(idor) == "not_found"  # the answer for a request that does not exist
        with pytest.raises(RequestRefusedError) as absent:
            r.service.cancel(MEMBER_ID, ULID)
        assert code_of(absent) == "not_found"
        assert r.state_of(rid) == "pending"
        r.service.cancel(MEMBER_ID, rid)
        assert r.state_of(rid) == "cancelled" and r.audit("request.cancelled")
        with pytest.raises(RequestRefusedError):  # a cancelled request cannot be approved
            r.service.approve(rid, admin())
        approved = ask(r, MEMBER_ID, "member", SourceRef("alpha", "r2", "Two", {}))
        r.service.approve(approved, admin())
        with pytest.raises(RequestRefusedError) as late:
            r.service.cancel(MEMBER_ID, approved)
        assert code_of(late) == "not_pending"


@pytest.mark.req("EF-USR-010", "EF-USR-016")
def test_request_of_someone_else_is_not_found_for_get_and_list(settings: Settings) -> None:
    with rig(settings) as r:
        rid = ask(r, MEMBER_ID, "member")
        for stranger in (OTHER_ID, CHILD_ID, ULID):
            with pytest.raises(RequestRefusedError) as refused:
                r.service.get(stranger, rid)
            assert code_of(refused) == "not_found"
            assert r.service.mine(stranger) == []
        with pytest.raises(RequestRefusedError) as unknown:
            r.service.get(MEMBER_ID, ULID)
        assert code_of(unknown) == code_of(refused)


@pytest.mark.req("EF-USR-016")
def test_request_visibility_follows_the_profile_setting(settings: Settings) -> None:
    """`effective_visibility` finally has a caller: private for an adult, household for a child,
    and the choice of the profile (`user.visibility`) for an adult who made it."""
    with rig(settings) as r:
        adult = ask(r, MEMBER_ID, "member")
        child = ask(r, CHILD_ID, "child")
        with r.engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE \"user\" SET visibility = 'household' WHERE id = :u"),
                {"u": OTHER_ID},
            )
            connection.execute(
                sa.text("UPDATE \"user\" SET visibility = 'private' WHERE id = :u"), {"u": CHILD_ID}
            )
        shared = ask(r, OTHER_ID, "member", SourceRef("alpha", "r3", "Three", {}))
        sibling = ask(r, SIBLING_ID, "child", SourceRef("alpha", "r4", "Four", {}))
        late_child = ask(r, CHILD_ID, "child", SourceRef("alpha", "r5", "Five", {}))
        seen = {x.id: x.visibility for x in r.rows("SELECT id, visibility FROM request")}
        assert seen[adult] == "private" and seen[child] == "household"
        assert seen[shared] == "household"
        assert seen[sibling] == "household"
        assert seen[late_child] == "household"  # a child is never hidden from the parents


@pytest.mark.req("EF-USR-016", "EF-USR-010")
def test_admin_queue_masks_the_title_of_a_private_adult_request(settings: Settings) -> None:
    with rig(settings) as r:
        adult = ask(r, MEMBER_ID, "member", SourceRef("alpha", "r1", "Private Tale", {}))
        child = ask(r, CHILD_ID, "child", SourceRef("alpha", "r2", "Kid Tale", {}))
        free = r.service.create(
            requester(OTHER_ID, "member"), RequestTarget("free_text", "Typed Secret Title")
        ).id
        queue = {v.id: v for v in r.service.queue(admin())}
        assert queue[adult].title is None  # the operation, not the title
        assert queue[free].title is None  # a typed text is a title too
        assert queue[child].title == "Kid Tale"  # a child is never hidden from the parents
        assert all(v.requester_id for v in queue.values())
        assert "Private Tale" not in repr(queue[adult]) and "Typed Secret" not in repr(queue[free])
        # unmasking is explicit and audited: the existing door (`request.unmask`), not this queue
        for who in (
            principal(MEMBER_ID, "member"),
            principal(CHILD_ID, "child"),
            principal(ADMIN_ID, "admin", credential=Credential.API_KEY),
        ):
            with pytest.raises(RequestRefusedError) as refused:
                r.service.queue(who)
            assert code_of(refused) == "not_allowed"


@pytest.mark.req("EF-USR-016")
def test_own_title_is_shown_to_its_requester_only(settings: Settings) -> None:
    with rig(settings) as r:
        rid = ask(r, MEMBER_ID, "member", SourceRef("alpha", "r1", "Mine Alone", {}))
        assert r.service.get(MEMBER_ID, rid).title == "Mine Alone"
        assert [v.title for v in r.service.mine(MEMBER_ID)] == ["Mine Alone"]


# -- audit ---------------------------------------------------------------------------------------


@pytest.mark.req("ENF-OBS-003")
def test_approval_audited(settings: Settings) -> None:
    ports = FakePorts(kinds={"alpha": "novel"}, shelf="SHELF")
    with rig(settings, ports=ports) as r:
        rid = ask(r)
        who = admin()
        r.service.approve(rid, who, age=10)
        (row,) = r.audit("request.approved")
        assert row.actor_id == who.actor_id
        assert row.target.startswith(f"request:{rid}")
        assert "Quiet" not in row.target and "alpha" not in row.target  # never a title or a source
        full = r.rows("SELECT session_id, auth_time FROM audit WHERE action = 'request.approved'")
        assert full[0].session_id == who.session_id and full[0].auth_time is not None
        # the creation and the other decisions are journalled the same way
        assert r.audit("request.created")
        reject = ask(r, MEMBER_ID, "member", SourceRef("alpha", "r2", "Two", {}))
        r.service.reject(reject, who, "no")
        assert r.audit("request.rejected")[0].target.startswith(f"request:{reject}")
        assert "no" not in r.audit("request.rejected")[0].target.split(";")[0]


@pytest.mark.req("ENF-OBS-003")
def test_decision_and_its_audit_are_one_transaction(settings: Settings) -> None:
    with rig(settings, ports=FakePorts(kinds={"alpha": "novel"})) as r:
        rid = ask(r, MEMBER_ID, "member")
        with r.engine.begin() as connection:  # the journal refuses writes: the decision must too
            connection.execute(
                sa.text(
                    "CREATE TRIGGER no_audit BEFORE INSERT ON audit "
                    "WHEN NEW.action = 'request.rejected' "
                    "BEGIN SELECT RAISE(ABORT, 'audit down'); END"
                )
            )
        with pytest.raises(sa.exc.DBAPIError):
            r.service.reject(rid, admin(), "no")
        assert r.state_of(rid) == "pending"
