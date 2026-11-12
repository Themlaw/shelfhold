"""M3 L35 (EF-USR-010): the target of a request is polymorphic, and the rules of an approval are
pure. What a child may ask for, what an approval needs, and which states lead where."""

from __future__ import annotations

import pytest
from shelfhold.acquisition.domain.request_target import (
    SIZE_CONFIRM_BYTES,
    SIZE_CONFIRM_UNITS,
    InvalidTargetError,
    RequestTarget,
    approval_refusal,
    can_transition,
    child_may_ask,
    parse_target,
    pick_edition,
)

ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


@pytest.mark.req("EF-USR-010")
def test_request_target_polymorphic_resolved_at_approval() -> None:
    """Four kinds of target, each normalised; none of them is a Work yet."""
    assert parse_target("work_id", ULID) == RequestTarget("work_id", ULID)
    assert parse_target("source_ref", ULID) == RequestTarget("source_ref", ULID)
    # an ISBN-10 and the ISBN-13 of the same book are one target, hyphens and spaces ignored
    assert parse_target("isbn", "0-306-40615-2") == parse_target("isbn", "9780306406157")
    assert parse_target("isbn", " 978 0 306 40615 7 ").value == "9780306406157"
    assert parse_target("free_text", "  A   quiet \n tale ").value == "A quiet tale"


@pytest.mark.req("EF-USR-010")
@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("work_id", "not-a-ulid"),
        ("work_id", ULID.lower()),
        ("source_ref", "x" * 26),
        ("isbn", "9780306406158"),  # wrong check digit
        ("isbn", "12345"),
        ("free_text", "   "),
        ("free_text", "x" * 201),
        ("free_text", "a\x00b"),  # a control character
        ("free_text", "a​b"),  # an invisible format character
        ("free_text", "a‮b"),  # a bidi override
        ("url", "https://example.invalid/"),  # a kind nobody defined
        ("", "x"),
    ],
)
def test_request_target_refuses_what_is_not_a_target(kind: str, value: str) -> None:
    with pytest.raises(InvalidTargetError):
        parse_target(kind, value)


@pytest.mark.req("EF-USR-010", "EF-USR-014")
def test_child_may_only_ask_by_a_reference_the_search_gave() -> None:
    """A child's door is the reference of the family search: a Work identifier would tell her
    whether a hidden title exists, a free text or an ISBN would make her a search box."""
    assert child_may_ask("source_ref")
    for kind in ("work_id", "isbn", "free_text", "", "anything"):
        assert not child_may_ask(kind)


@pytest.mark.req("EF-USR-010")
def test_request_states_only_move_forward() -> None:
    assert can_transition("pending", "approved")
    assert can_transition("pending", "rejected")
    assert can_transition("pending", "not_found")
    assert can_transition("pending", "cancelled")
    assert can_transition("approved", "arrived")
    for end in ("rejected", "not_found", "cancelled", "arrived"):  # nothing leaves a final state
        for state in ("pending", "approved", "rejected", "not_found", "cancelled", "arrived"):
            assert not can_transition(end, state)
    assert not can_transition("approved", "cancelled")  # approved: the admin said yes, no undo
    assert not can_transition("pending", "arrived")
    assert not can_transition("pending", "unknown")


@pytest.mark.req("EF-USR-010")
def test_child_request_requires_age_on_approval() -> None:
    """Rating is approving (B2): a child's request is not approved without an age."""
    ask = {"units": 3, "size_bytes": 10, "confirmed": False}
    assert approval_refusal(requester_role="child", age=None, already_rated=False, **ask) == (
        "choose_age"
    )
    assert approval_refusal(requester_role="child", age=6, already_rated=False, **ask) is None
    # a Work the admin already rated in bulk needs no second age
    assert approval_refusal(requester_role="child", age=None, already_rated=True, **ask) is None
    assert approval_refusal(requester_role="member", age=None, already_rated=False, **ask) is None
    assert approval_refusal(requester_role="admin", age=None, already_rated=False, **ask) is None
    # a role nobody defined is a child: closed by default
    assert approval_refusal(requester_role="???", age=None, already_rated=False, **ask) == (
        "choose_age"
    )
    # the age is a tier or nothing: 0 is a tier ("all ages"), a bool or a negative is not
    for bad in (-1, 100, True):
        assert (
            approval_refusal(requester_role="child", age=bad, already_rated=False, **ask)
            == "choose_age"
        )


@pytest.mark.req("EF-USR-010")
def test_request_size_confirmation_over_threshold() -> None:
    assert SIZE_CONFIRM_UNITS == 100 and SIZE_CONFIRM_BYTES == 5 * 2**30

    def refusal(units: int, size: int, confirmed: bool = False) -> str | None:
        return approval_refusal(
            requester_role="member", age=None, already_rated=False,
            units=units, size_bytes=size, confirmed=confirmed,
        )  # fmt: skip

    assert refusal(100, 5 * 2**30) is None  # at the threshold: no confirmation
    assert refusal(101, 0) == "confirmation_required"
    assert refusal(1, 5 * 2**30 + 1) == "confirmation_required"
    assert refusal(101, 0, confirmed=True) is None
    assert refusal(1, 5 * 2**30 + 1, confirmed=True) is None
    with pytest.raises(ValueError):
        refusal(-1, 0)  # a negative size is a bug upstream, never a pass


@pytest.mark.req("EF-USR-010")
def test_default_edition_follows_the_language_of_the_requester() -> None:
    editions = [("E1", "fr"), ("E2", "en"), ("E3", "en-GB")]
    assert pick_edition(editions, "en") == "E2"
    assert pick_edition(editions, "fr") == "E1"
    assert pick_edition(editions, "de") == "E1"  # none: the first one, in a stable order
    assert pick_edition([], "en") is None
    assert pick_edition([("E9", "und")], "en") == "E9"
