"""Every event the code publishes with a literal payload passes the check of the real bus.

A test that stands in for `publish` never sees the bus refuse a list or a text, and the nightly job
that published it then fails in production (a failed copy to /store did exactly that). This reads
the source: for each call to a publisher with a literal event type and a dict literal, the literal
values are judged by `check_payload`; a list, a set, a dict or a call to `sorted`/`list` is a
non-scalar, and a value the scan cannot know is replaced by one that is valid for its key."""

import ast
from pathlib import Path

import pytest

from shelfhold.events.bus import EventPayloadError, check_payload

SRC = Path(__file__).resolve().parents[2] / "src"
ULID = "01M7Q3SQR0E52TZ4D8T92HWP2J"
PUBLISHERS = {"publish", "_publish", "_emit", "emit", "_announce"}
ENUM_KEYS = {"reason", "kind", "state", "code", "error_code", "scope", "volume"}


def _value(key: str, node: ast.expr) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.List | ast.Tuple | ast.Set | ast.Dict | ast.ListComp):
        return []
    if isinstance(node, ast.Call) and getattr(node.func, "id", "") in {
        "sorted",
        "list",
        "set",
        "tuple",
        "dict",
    }:
        return []
    if key.endswith("_id"):
        return ULID
    return None if key in ENUM_KEYS else 1


@pytest.mark.req("ENF-OBS-003")
def test_every_literal_payload_of_a_publisher_passes_the_check_of_the_bus() -> None:
    refused: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text()
        for call in (n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)):
            name = getattr(call.func, "attr", getattr(call.func, "id", None))
            if name not in PUBLISHERS:
                continue
            event = next(
                (
                    a.value for a in call.args
                    if isinstance(a, ast.Constant) and isinstance(a.value, str) and "." in a.value
                ),
                None,
            )  # fmt: skip
            payload = next((a for a in call.args if isinstance(a, ast.Dict)), None)
            if event is None or payload is None:
                continue
            literal = {
                k.value: _value(k.value, v)
                for k, v in zip(payload.keys, payload.values, strict=True)
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
                and not (k.value in ENUM_KEYS and not isinstance(v, ast.Constant))
            }  # fmt: skip
            try:
                check_payload(literal, event)
            except EventPayloadError as error:
                refused.append(f"{path.relative_to(SRC)}:{call.lineno} {event}: {error}")
    assert not refused, "\n".join(refused)


@pytest.mark.req("ENF-OBS-003")
def test_the_events_written_straight_to_the_table_pass_the_check_of_the_bus() -> None:
    """`changes._insert` skips `publish` but runs the same check: the payloads it writes (the
    child of a visibility change included) must be accepted, whatever the id of a person is."""
    from shelfhold.catalog.application import changes
    from shelfhold.infrastructure.ids import new_ulid

    work, child, shelf = new_ulid(), new_ulid(), new_ulid()
    for event, payload in (
        (
            changes.PROFILE_EVENT,
            {
                "work_id": work,
                "profile_id": child,
                "visible_before": False,
                "visible_after": True,
                "created_seq": 3,
            },
        ),
        (
            changes.EDITION_EVENT,
            {
                "edition_id": work,
                "profile_id": child,
                "visible_before": True,
                "visible_after": False,
            },
        ),
        (changes.SHELF_REMOVED_EVENT, {"shelf_id": shelf}),
    ):
        check_payload(payload, event)
