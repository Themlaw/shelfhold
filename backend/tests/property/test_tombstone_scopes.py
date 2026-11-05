"""EF-BIB-053: scopes, forged lines, the registry of issued kids (passes 10 to 13, N1, R58)."""

import tempfile
from pathlib import Path

import nacl.utils
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.library.tombstones import (
    MembershipIndex,
    PlaceRef,
    SeriesBindingRef,
    VariantRef,
    WorkRef,
    default_normalizers,
)
from shelfhold.library.tombstones.journal import JournalLine, header_bytes
from tests.tombstone_env import PLACE, SERIES, VARIANT, make_tomb_env

pytestmark = pytest.mark.req("EF-BIB-053")


def _forged_line(kid: str, seq: int = 1) -> bytes:
    return JournalLine(
        seq, "series_binding", kid, nacl.utils.random(24), nacl.utils.random(80)
    ).dumps()


@pytest.mark.parametrize("kid_kind", ["known_kid", "unregistered_kid"])
def test_forged_tombstone_line_never_deletes(tmp_path: Path, kid_kind: str) -> None:
    """A line without an authenticable ciphertext, under a known kid or a kid absent from the
    registry: nothing deleted, nothing blocked, `tombstone.unverified`."""
    env = make_tomb_env(tmp_path)
    keyring = env.store.load()
    chapter = env.fs.manga / "Series" / "Chapter 7.cbz"
    chapter.parent.mkdir(parents=True)
    chapter.write_bytes(b"irreplaceable")
    kid = keyring.active.kid if kid_kind == "known_kid" else "k-" + "ee" * 8
    env.fs.fsops.append_tombstone_line(env.library.path, _forged_line(kid), header=header_bytes())

    service = env.service()
    report = service.replay(env.roots)

    expected = "authentication_failed" if kid_kind == "known_kid" else "kid_unregistered"
    assert [(line.root_id, line.reason) for line in report.inert] == [
        (env.library.root_id, expected)
    ]
    assert report.key_unknown == set()
    assert report.verified == 0
    assert chapter.read_bytes() == b"irreplaceable"
    for candidate in (SERIES, VARIANT, PLACE):
        assert not service.check(candidate, root_id=env.library.root_id).blocked
    assert service.reconcile_allowed(env.library.root_id)
    assert env.events("tombstone.unverified") == [
        {"root_id": env.library.root_id, "reason": expected, "seq": 1}
    ]
    assert env.table_rows() == []  # nothing merged from a line that does not authenticate


def test_unregistered_kid_line_inert_registered_unknown_kid_blocks(tmp_path: Path) -> None:
    """Invented kid (absent from both registries): inert, `kid_unregistered`. Kid absent from the
    keyring but in the sentinel's registry (database restored from before the rotation): the
    reacquisition of its scope on that root blocks, `tombstone.key_unknown`."""
    env = make_tomb_env(tmp_path)
    old = env.service()
    old.replay(env.roots)
    old.record(SERIES, [env.library])
    issued = old.report  # nothing pending: the kid is in the library's sentinel
    assert issued.inert == []
    assert env.store.load().active.kid in env.sentinel(env.library)["tombstone_kids"]
    env.fs.fsops.append_tombstone_line(
        env.books.path, _forged_line("k-" + "ab" * 8, seq=9), header=header_bytes()
    )
    # The database comes back from a copy taken before the purge, the keyring from before the
    # rotation that issued the kid: neither knows it; only the sentinel does.
    restored = make_tomb_env(tmp_path / "restored")
    restored.fs = env.fs
    restored.roots = env.roots
    service = restored.service(Keyring.generate())

    report = service.replay(env.roots)

    assert report.key_unknown == {(env.library.root_id, "series_binding")}
    assert [(i.root_id, i.reason) for i in report.inert] == [
        (env.books.root_id, "kid_unregistered")
    ]
    blocked = service.check(SERIES, root_id=env.library.root_id)
    assert blocked.blocked and blocked.cause == "key_unknown"
    assert not service.check(SERIES, root_id=env.books.root_id).blocked  # the forged one: inert
    other_scope = service.check(PLACE, root_id=env.library.root_id)
    assert not other_scope.blocked
    assert not service.reconcile_allowed(env.library.root_id)
    assert service.reconcile_allowed(env.books.root_id)
    assert restored.events("tombstone.key_unknown") == [
        {"root_id": env.library.root_id, "scope": "series_binding"}
    ]
    assert restored.events("tombstone.unverified")[0]["reason"] == "kid_unregistered"


refs = st.text(alphabet="abcdefghij/-0123456789", min_size=1, max_size=12)


@st.composite
def _values(draw: st.DrawFn) -> VariantRef | PlaceRef | SeriesBindingRef | WorkRef:
    kind = draw(st.sampled_from(["variant", "place", "series", "work"]))
    if kind == "variant":
        return VariantRef("suwayomi", draw(refs), draw(refs))
    if kind == "place":
        return PlaceRef.of(
            {"lang": draw(st.sampled_from(["fr", "en", "ja"])), "media": "images"},
            track="main",
            number_segment="",
            number_raw=str(draw(st.integers(1, 40))),
            part="",
        )
    if kind == "series":
        return SeriesBindingRef("suwayomi", draw(refs), draw(refs))
    return WorkRef("isbn", draw(refs))


@settings(max_examples=60)
@given(tombstoned=st.lists(_values(), max_size=6), others=st.lists(_values(), max_size=6))
def test_membership_blocks_exactly_the_tombstoned_values(
    tombstoned: list[VariantRef | PlaceRef | SeriesBindingRef | WorkRef],
    others: list[VariantRef | PlaceRef | SeriesBindingRef | WorkRef],
) -> None:
    """I40: each scope blocks what it names, and only that (property)."""
    normalizers = default_normalizers()
    index = MembershipIndex(normalizers)
    index.add_all((values, {"number": "number@1", "lang": "lang@1"}) for values in tombstoned)
    for values in tombstoned:
        assert index.contains(values)
    for values in others:
        assert index.contains(values) == (values in tombstoned)


def test_series_binding_scope_blocks_follow_of_the_series(tmp_path: Path) -> None:
    """EF-BIB-053 (2): the follow cycle asks before creating or adopting anything; a series under a
    `series_binding` tombstone is refused, another series of the same source is not."""
    with tempfile.TemporaryDirectory(dir=tmp_path) as raw:
        env = make_tomb_env(Path(raw))
        service = env.service()
        service.replay(env.roots)
        service.record(SERIES, env.roots)
        verdict = service.check(SERIES, root_id=None)
        assert verdict.blocked and verdict.cause == "tombstoned"
        sibling = SeriesBindingRef(SERIES.source_key, SERIES.sub_source, SERIES.series_ref + "-2")
        assert not service.check(sibling, root_id=None).blocked
