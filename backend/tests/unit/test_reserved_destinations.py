"""Reserved destinations (M4 L11): the Calibre and Audiobookshelf declarations are expressible
with the SDK 1.4 types alone, and pass the declaration kit. No adapter ships (PLAN-M4 D9)."""

import pytest
from shelfhold_sdk.api_call import ApiCall, ContentBody, RawEncoding, check_api_call
from shelfhold_sdk.export_target import TargetCapabilities
from shelfhold_sdk.fsops import ContentRef, PlannedContent
from shelfhold_sdk.testing.fixtures.reserved_targets import (
    AudiobookshelfReservedTarget,
    CalibreReservedTarget,
)
from shelfhold_sdk.testing.kit import run_destination_declaration_check


def _parts(raw: str) -> tuple[int, ...]:
    return tuple(int(piece) for piece in raw.split("."))


@pytest.mark.req("EF-EXP-023")
def test_calibre_cdb_push_declaration_passes_kit() -> None:
    target = CalibreReservedTarget()
    caps = target.capabilities
    assert isinstance(caps, TargetCapabilities)
    report = run_destination_declaration_check(caps)
    assert report.ok, report.failures
    assert caps.transport == "push"
    assert caps.content_body is True
    # calibre has no field lock: `lock` is ignored, and declared as such
    assert caps.update_metadata is not None
    assert caps.update_metadata.lock is False
    assert caps.indexes_tree is False


@pytest.mark.req("EF-EXP-023")
def test_calibre_add_book_carries_a_raw_body_and_book_id_is_the_remote_id() -> None:
    target = CalibreReservedTarget()
    ref = ContentRef(kind="export_item", ref="item-1")
    call = target.add_book_call(ref, filename="Title.epub", library_id="main")
    assert isinstance(call, ApiCall)
    assert call.method == "POST"
    assert call.path.startswith("/cdb/add-book/")
    assert call.path.split("/")[4] == "n"  # add_duplicates = n is the idempotence
    assert isinstance(call.body, ContentBody)
    assert isinstance(call.body.encoding, RawEncoding)
    assert call.auth == "target_admin"
    planned = PlannedContent(planned_refs=frozenset({ref.ref}), egress="foyer")
    check_api_call(call, planned)
    assert target.interpret(call, {"book_ids": [42]}) == {"remote_id": "42"}


@pytest.mark.req("EF-EXP-035")
def test_audiobookshelf_tree_declaration_passes_kit() -> None:
    target = AudiobookshelfReservedTarget()
    caps = target.capabilities
    report = run_destination_declaration_check(caps)
    assert report.ok, report.failures
    assert caps.transport == "tree"
    assert caps.path_scoped_scan is True
    assert "metadata.json" in caps.sidecars
    paths = [c.path for c in target.scan_calls(["Author/Series/1 - Title"])]
    assert paths == ["/api/watcher/update"]
    assert all("/api/upload" not in c.path for c in target.scan_calls(["a", "b"]))


@pytest.mark.req("EF-EXP-035", "ENF-SEC-016")
def test_audiobookshelf_below_2_32_2_in_known_bad_versions() -> None:
    versions = AudiobookshelfReservedTarget().supported_versions()
    flagged = [
        kb for kb in versions["known_bad_versions"] if _parts("2.32.1") < _parts(kb["below"])
    ]
    assert flagged, "2.32.1 must be flagged"
    assert flagged[0]["category"] == "access_control"
    assert not any(_parts("2.32.2") < _parts(kb["below"]) for kb in versions["known_bad_versions"])
    assert versions["minimum"] == "2.37"  # the floor of ENF-SEC-016
