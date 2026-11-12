"""Review of #209 (M3 L35), contradictor: proofs of concept, each red on the head of the PR.

Every test names the leak it proves; none of them changes production code. The ports are the
fakes of `tests/request_env.py`; the sources are `source-a`, `source-b`."""

from __future__ import annotations

from typing import Any

import pytest

from shelfhold.acquisition.application.requests import RequestRefusedError, SourceRef
from shelfhold.acquisition.domain.request_target import RequestTarget
from shelfhold.config import Settings
from tests.request_env import (
    CHILD_ID,
    MEMBER_ID,
    SIBLING_ID,
    FakePorts,
    Rig,
    admin,
    bind_work,
    requester,
    rig,
)

SRC = "source-a"
REF = SourceRef(source_key=SRC, remote_id="r1", title="A Quiet Tale", memo={"entry": "e1"})


def ask(r: Rig, who: str = CHILD_ID, role: str = "child", ref: SourceRef = REF) -> str:
    return r.service.create_from_ref(requester(who, role), ref).id


class _RefusingShelfPorts(FakePorts):
    """The source hands the book over, then the shelf refuses it (a shelf the admin named that
    does not take this Edition: `shelf_refused`, as `ops/requests.py` raises it)."""

    def file(self, edition_id: str, shelf_id: str, *, approver_id: str) -> None:
        raise RequestRefusedError("shelf_refused")


@pytest.mark.req("EF-USR-010", "ENF-OBS-003")
def test_review_poc_failed_approval_leaves_an_acquired_rated_book_and_no_approval(
    settings: Settings,
) -> None:
    """Choice 8 (decision first, revert on failure) only undoes the request row and its audit
    line: what the source did and the rating stay. The request is pending again, can be refused,
    and the library holds a book rated by an approval that the journal says never happened."""
    ports = _RefusingShelfPorts(kinds={SRC: "novel"}, shelf="SHELF")
    with rig(settings, ports=ports) as r:
        rid = ask(r)
        with pytest.raises(RequestRefusedError):
            r.service.approve(rid, admin(), age=10)
        assert r.state_of(rid) == "pending" and r.audit("request.approved") == []
        r.service.reject(rid, admin(), "no")
        # a refused request: nothing may have been fetched for it, nothing rated by it
        assert ports.acquired == [], "acquired for a request that ends refused"
        assert r.rows("SELECT count(*) FROM work WHERE rated_at IS NOT NULL")[0][0] == 0
        assert r.audit("rate") == [], "a rating line with no approval line behind it"


@pytest.mark.req("EF-USR-010")
def test_review_poc_crash_after_the_claim_strands_the_request(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Choice 8: the process dies between the claim and the call to the source (the revert never
    runs). The request reads `approved` forever, nothing follows it, and no door moves it again:
    approve says `not_pending`, reject and not-found too."""
    ports = FakePorts(kinds={SRC: "manga"}, fail_follow=RuntimeError("killed"))
    with rig(settings, ports=ports) as r:
        rid = ask(r, MEMBER_ID, "member")
        monkeypatch.setattr(type(r.service), "_revert", lambda *_a, **_k: None)  # the crash
        with pytest.raises(RuntimeError):
            r.service.approve(rid, admin())
        monkeypatch.undo()
        ports.fail_follow = None
        assert r.state_of(rid) == "approved" and ports.follows == []
        outcomes: list[str] = []
        for attempt in (
            lambda: r.service.approve(rid, admin()),
            lambda: r.service.reject(rid, admin(), "retry"),
            lambda: r.service.mark_not_found(rid, admin()),
        ):
            try:
                attempt()
                outcomes.append("moved")
            except RequestRefusedError as error:
                outcomes.append(error.code)
        assert "moved" in outcomes or ports.follows, (
            f"an approved request with no effect is stuck: {outcomes}"
        )


@pytest.mark.req("EF-USR-016", "EF-USR-010")
def test_review_poc_masked_title_comes_back_from_the_decision_routes(settings: Settings) -> None:
    """Choice 3: the queue withholds the title of a private request of an adult, but every
    decision returns the full view (`_view` without `for_admin`): refuse it, or mark it not
    found, and the title is in the answer, with no `request.unmask` line."""
    with rig(settings) as r:
        rejected = r.service.create(
            requester(MEMBER_ID, "member"), RequestTarget("free_text", "Typed Secret Title")
        ).id
        missing = r.service.create(
            requester(MEMBER_ID, "member"), RequestTarget("free_text", "Other Secret Title")
        ).id
        assert {v.title for v in r.service.queue(admin())} == {None}  # masked in the queue
        seen = [
            r.service.reject(rejected, admin(), "no").title,
            r.service.mark_not_found(missing, admin()).title,
        ]
        assert seen == [None, None], f"masked titles returned to the admin: {seen}"


@pytest.mark.req("EF-USR-016", "EF-USR-010")
def test_review_poc_preview_names_the_edition_of_a_masked_request(settings: Settings) -> None:
    """Choice 3: `GET /{id}/approval` is a read with no state change, and for a private request
    of an adult it hands the Edition (hence the Work, hence the title) the queue withholds."""
    with rig(settings) as r:
        work = r.work()
        rid = r.service.create(requester(MEMBER_ID, "member"), RequestTarget("work_id", work)).id
        (queued,) = r.service.queue(admin())
        assert queued.title is None
        preview = r.service.preview(rid, admin())
        assert preview.edition_id is None, "the preview names the Edition the queue masks"


@pytest.mark.req("EF-USR-010", "EF-USR-014")
def test_review_poc_title_shown_to_a_child_is_rewritten_by_another_reference(
    settings: Settings,
) -> None:
    """Card § 2: "the title shown to the requester is the one she was shown". It is stored on
    the shared `pending_source_item` (one row per source and remote id) and `_upsert_item`
    overwrites it: the next reference to the same result, a sibling's or a later search, renames
    her request."""
    with rig(settings) as r:
        rid = ask(r)
        assert r.service.get(CHILD_ID, rid).title == "A Quiet Tale"
        ask(r, SIBLING_ID, "child", SourceRef(SRC, "r1", "Another Name Entirely", {"entry": "x"}))
        shown = r.service.get(CHILD_ID, rid).title
        assert shown == "A Quiet Tale", f"her request now reads {shown!r}"


@pytest.mark.req("EF-USR-010", "EF-USR-006")
def test_review_poc_two_ages_for_one_series_the_laxer_one_can_win(settings: Settings) -> None:
    """Choice 1 (age kept on the request, applied when the Work appears): two children ask for
    the same followed series. The admin approves the elder's with 16, then the younger's with 10
    (the preview says nothing of the 16 already decided). At arrival the rows are read in no
    order and the first one rates the Work: here 10, the laxer age, is what the catalogue keeps.
    Two decisions on one Work must not be settled by the order of a table scan; closed by
    default means the stricter one."""
    ports = FakePorts(kinds={SRC: "manga"})
    with rig(settings, ports=ports) as r:
        young = ask(r, CHILD_ID, "child")  # asked first: read first by the arrival
        elder = ask(r, SIBLING_ID, "child")
        r.service.approve(elder, admin(), age=16)
        preview = r.service.preview(young, admin())
        assert preview.needs_age  # nothing tells the admin that 16 is already decided
        r.service.approve(young, admin(), age=10)
        with r.engine.begin() as connection:
            work = bind_work(connection, SRC, "r1")
        r.service.arrived(work)
        rated: Any = r.rows("SELECT age_rating FROM work WHERE id = :w", w=work)[0][0]
        assert rated == 16, f"the Work was rated {rated}, not the stricter 16"


@pytest.mark.req("ENF-OBS-003")
def test_review_poc_reject_reason_is_not_scrubbed_from_the_diagnostic_bundle() -> None:
    """The bundle's last barrier forbids every household text column (`_sensitive_columns`).
    `request.reject_reason` is free text the admin writes to a child ("not before 16: <title>")
    and does not match the pattern: a log line carrying it would leave the instance."""
    from shelfhold.ops.diagnostic import _sensitive_columns

    covered = {(c.table.name, c.name) for c, _word in _sensitive_columns()}
    assert ("pending_source_item", "title") in covered  # the barrier works for titles
    assert ("request", "reject_reason") in covered, "reject_reason is not a forbidden value"
