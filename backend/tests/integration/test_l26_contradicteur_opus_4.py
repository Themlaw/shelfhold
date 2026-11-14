"""M3 L26, review of the linked lift (6e14cef2): proofs of concept, red against the branch as
reviewed. See `docs/decisions/M3.md`, « L26 : contradicteur de la #233 », linked lift."""

from pathlib import Path

import pytest

from shelfhold.library.tombstones import SeriesBindingRef, VariantRef
from shelfhold.library.tombstones.lift import lift_phrase
from tests.intents_env import NOW
from tests.tombstone_env import ADMIN, make_tomb_env

OLD = SeriesBindingRef("plug", "src-a", "/old/ref")
NEW = SeriesBindingRef("plug", "src-a", "/new/ref")


@pytest.mark.req("EF-BIB-054")
def test_a_new_form_stays_while_another_of_its_origins_still_blocks(tmp_path: Path) -> None:
    """The old reference is covered by two active tombstones (two purges whose references fold to
    the same one): `origin_tokens` gives both, and the new form names both. Lifting **one** of
    them lifts the new form, while the other still blocks the old reference: the series comes
    back under its new identifier although a tombstone that covers it is still active. A new form
    must be lifted only when none of its origins blocks any more."""
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    first = service.record(OLD, env.roots)
    service.record(OLD, env.roots)  # a second purge of the same reference: still active
    tokens = service.origin_tokens(OLD)
    assert len(tokens) == 2
    service.record(NEW, env.roots, reason="new_form", origins=tokens)

    service.lift(
        env.roots, seq=first.seq, confirmation=lift_phrase(first.seq), principal=ADMIN, now=NOW
    )

    assert service.check(OLD, root_id=env.library.root_id).blocked  # the second origin stays
    assert service.check(NEW, root_id=env.library.root_id).blocked, (
        "the new form was lifted while another of its origins still blocks the same series"
    )


@pytest.mark.req("EF-BIB-054")
def test_the_lift_of_a_tombstone_of_another_scope_never_lifts_a_new_form(tmp_path: Path) -> None:
    """Tombstones from before the identifiers are named by `"~" + sha256(folded fields)`, without
    their scope. A `variant` tombstone whose (source, sub-source, unit id) fold like the
    (source, sub-source, series ref) of a `series_binding` gets the same token: lifting that
    variant (another origin, another scope) lifts the new form of the series."""
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    # two lines as `record` wrote them before the identifiers (no `ev`)
    service._record(OLD, env.roots, reason=None, cited=service.cited_now(OLD))
    variant = VariantRef("plug", "src-a", "/old/ref")
    stranger = service._record(variant, env.roots, reason=None, cited=service.cited_now(variant))
    restarted = env.service()
    restarted.replay(env.roots)
    restarted.record(NEW, env.roots, reason="new_form", origins=restarted.origin_tokens(OLD))

    done = restarted.lift(
        env.roots, seq=stranger.seq, confirmation=lift_phrase(stranger.seq), principal=ADMIN,
        now=NOW,
    )  # fmt: skip

    assert done.linked == ()
    assert restarted.check(NEW, root_id=env.library.root_id).blocked
