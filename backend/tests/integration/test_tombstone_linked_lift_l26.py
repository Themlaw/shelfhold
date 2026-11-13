"""M3 L26 (EF-BIB-054 (7)), the linked lift L27 deferred: lifting the tombstone of an erased
reference lifts the `new_form` tombstones recorded because of it; each stays visible in
`tombstone list` (`linked_to`) and has its own audit row. Real cipher, journals and SQLite."""

from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.schema import coherence
from shelfhold.library.tombstones import SeriesBindingRef
from shelfhold.library.tombstones.lift import lift_phrase
from tests.integration.test_tombstone_lift_l26 import _audit
from tests.intents_env import NOW
from tests.tombstone_env import ADMIN, TombEnv, make_tomb_env

pytestmark = pytest.mark.req("EF-BIB-054")

OLD = SeriesBindingRef("plug", "src-a", "/old/ref")
NEW = SeriesBindingRef("plug", "src-a", "/new/ref")
OTHER = SeriesBindingRef("plug", "src-a", "/unrelated")


def _laid(tmp_path: Path) -> tuple[TombEnv, object, int, int]:
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    origin = service.record(OLD, env.roots)
    tokens = service.origin_tokens(OLD)
    assert len(tokens) == 1
    new = service.record(NEW, env.roots, reason="new_form", origins=tokens)
    return env, service, origin.seq, new.seq


def _lift(service: object, env: TombEnv, seq: int) -> object:
    return service.lift(  # type: ignore[attr-defined]
        env.roots, seq=seq, confirmation=lift_phrase(seq), principal=ADMIN, now=NOW
    )


def _blocked(service: object, values: SeriesBindingRef, env: TombEnv) -> bool:
    return bool(service.check(values, root_id=env.library.root_id).blocked)  # type: ignore[attr-defined]


def test_lifting_the_origin_lifts_its_new_forms_and_audits_each(tmp_path: Path) -> None:
    env, service, origin, new = _laid(tmp_path)
    other = service.record(OTHER, env.roots)  # type: ignore[attr-defined]

    done = _lift(service, env, origin)

    assert done.linked == (new,)  # type: ignore[attr-defined]
    assert not _blocked(service, OLD, env)
    assert not _blocked(service, NEW, env)
    assert _blocked(service, OTHER, env)  # an unrelated tombstone stays
    targets = sorted(str(r["target"]) for r in _audit(env, "tombstone.lifted"))
    assert targets == sorted([f"seq:{origin}", f"seq:{new};via:{origin}"])
    assert [r.seq for r in service.listing()] == [other.seq]  # type: ignore[attr-defined]


def test_listing_shows_which_origin_a_new_form_comes_from(tmp_path: Path) -> None:
    _env, service, origin, new = _laid(tmp_path)

    rows = {r.seq: r for r in service.listing()}  # type: ignore[attr-defined]

    assert rows[new].reason == "new_form"
    assert rows[new].linked_to == (origin,)
    assert rows[origin].linked_to == ()


def test_lifting_a_new_form_alone_leaves_the_origin(tmp_path: Path) -> None:
    env, service, _origin, new = _laid(tmp_path)

    done = _lift(service, env, new)

    assert done.linked == ()  # type: ignore[attr-defined]
    assert not _blocked(service, NEW, env)
    assert _blocked(service, OLD, env)


def test_the_linked_lift_survives_a_replay_and_the_loss_of_the_table(tmp_path: Path) -> None:
    env, service, origin, _new = _laid(tmp_path)
    _lift(service, env, origin)

    with env.engine.begin() as connection:  # the table is lost: the journals are the memory
        connection.execute(sa.delete(coherence.content_tombstone))
    again = env.service()
    again.replay(env.roots)

    assert not _blocked(again, OLD, env)
    assert not _blocked(again, NEW, env)


def test_a_new_form_with_no_known_origin_stands_alone(tmp_path: Path) -> None:
    env = make_tomb_env(tmp_path)
    service = env.service()
    service.replay(env.roots)
    assert service.origin_tokens(OLD) == []
    new = service.record(NEW, env.roots, reason="new_form", origins=[])

    assert [r.linked_to for r in service.listing() if r.seq == new.seq] == [()]
