"""The M1b points of the contract that were "to confirm at the nightly" (EF-SRC-009, EF-SRC-026,
EF-SRC-029): the documents Shelfhold sends, played as written on the pinned Suwayomi, in both
authentication modes. Real server, local source only, synthetic CBZ, no extension, no egress.

Run by the weekly `contract` job and by the `nightly` workflow (`-m nightly`): the same tests
carry both marks, so a drift of the pinned server shows up the next night, not at a release.
"""

from __future__ import annotations

import pytest

from shelfhold.plugin_host.adapters.meta.suwayomi import queries as q

from .harness import Bench

pytestmark = [pytest.mark.contract, pytest.mark.nightly]
CATEGORY = "Shelfhold"


def _categories(bench: Bench) -> list[dict[str, object]]:
    return list(bench.gql(q.CATEGORY_DOWNLOAD)["categories"]["nodes"])


@pytest.mark.req("EF-SRC-009", "EF-SRC-029")
def test_category_is_created_then_excluded_from_the_download_only(bench: Bench) -> None:
    """`createCategory` (name only), then `updateCategory` with `includeInDownload: EXCLUDE`, read
    back: the category is out of the automatic download and still in the scheduled updates, the
    refresh of the followed series (never `includeInUpdate`)."""
    created = bench.gql(q.CREATE_SHELFHOLD_CATEGORY)["createCategory"]["category"]
    assert created["name"] == CATEGORY
    try:
        before = next(c for c in _categories(bench) if c["id"] == created["id"])
        assert before["includeInDownload"] != "EXCLUDE"  # created plain: the second call is needed
        reply = bench.graphql(
            q.EXCLUDE_CATEGORY_FROM_DOWNLOAD, variables={"id": created["id"]}
        ).json()
        assert "errors" not in reply, reply  # the shape Shelfhold sends is accepted as written
        after = next(c for c in _categories(bench) if c["id"] == created["id"])
        assert after["includeInDownload"] == "EXCLUDE"
        updates = bench.gql(q.CATEGORIES)["categories"]["nodes"]
        assert next(c for c in updates if c["id"] == created["id"])["includeInUpdate"] != "EXCLUDE"
    finally:
        bench.gql(
            f"mutation {{ deleteCategory(input: {{categoryId: {created['id']}}}) {{ category {{ id }} }} }}"
        )


@pytest.mark.req("EF-SRC-026")
def test_download_status_has_the_state_and_the_queue_the_requeue_reads(bench: Bench) -> None:
    """`downloadStatus { state queue { chapter { id } } }`: an idle server answers a state and an
    empty queue (a list or null, both are read as empty), never an error."""
    reply = bench.graphql(q.DOWNLOAD_QUEUE).json()
    assert "errors" not in reply, reply
    status = reply["data"]["downloadStatus"]
    assert isinstance(status["state"], str) and status["state"]
    assert not status["queue"]  # nothing is downloading on a fresh bench


@pytest.mark.req("EF-SRC-026")
def test_update_filters_are_named_as_the_adapter_reads_them(bench: Bench) -> None:
    """`excludeUnreadChapters` and `excludeNotStarted`: the two global filters exist on the pinned
    server, as booleans, and the ones that are on are what `source add` refuses.
    (On the pinned server, `excludeUnreadChapters` is ON by a fresh installation: the default
    that `source add` refuses and tells how to correct.)"""
    reply = bench.graphql(q.UPDATE_FILTERS).json()
    assert "errors" not in reply, reply
    settings = reply["data"]["settings"]
    assert isinstance(settings["excludeUnreadChapters"], bool)
    assert isinstance(settings["excludeNotStarted"], bool)
    print("update filters of the pinned server:", settings)
