"""L12, the verification of #134: variants of the review's attacks on the parental barrier.

Chains of attaches undone in any order, an origin rated after the attach, an attach undone then
made again, two suggestions for the same Edition, and the Work it joins rated at every age: no
Edition rated 16+ where it came from ever enters the shelf capped at 13.
"""

import pytest

from shelfhold.catalog.public import (
    Arrival,
    AttachRefusedError,
    Detached,
    ExternalId,
    IncomingEdition,
    add_external_id,
    suggest_age,
)
from shelfhold.sdk.age import AgeRating
from tests.export_env import ExportEnv, Series
from tests.family_env import NOW, admin, family_env, rate
from tests.integration.test_auto_attach import (
    ANILIST,
    Scene,
    _confirmed,
    _files,
    _open_suggestions,
    arriving,
    edition_row,
    scene,
)

pytestmark = pytest.mark.req("EF-BIB-055", "EF-BIB-037", "EF-BIB-003")

MAL = ExternalId("work", "mal", "777")


def _third(env: ExportEnv) -> Series:
    other = env.new_series("Dr Stone bis")
    rate(env, other, 0)
    with env.engine.begin() as connection:
        add_external_id(connection, other.work_id, MAL, origin="provider")
    return other


def _separate(s: Scene, attach_id: str) -> Detached:
    with s.env.engine.begin() as connection:
        return s.service.separate(connection, attach_id, principal=admin(), now=NOW)


def _work_of(env: ExportEnv, edition_id: str) -> Series:
    return Series(str(edition_row(env, edition_id)[0]), "", "")


def _arrive(
    s: Scene, edition_id: str, external: ExternalId, shelf_id: str | None = None
) -> Arrival:
    incoming = IncomingEdition(edition_id, shelf_id=shelf_id, ids=(external,))
    with s.env.engine.begin() as connection:
        return s.service.arrive(connection, incoming, now=NOW)


def test_a_chain_of_attaches_undone_out_of_order() -> None:
    with family_env(13) as env:
        s = scene(env)
        uncut = arriving(env)
        rate(env, uncut, 16)
        first = _arrive(s, uncut.edition_id, ANILIST)
        assert first.state == "suggested"
        _confirmed(s, first.attach_id)
        _third(env)
        rate(env, s.family, 0)  # stamps the Edition there; its 16+ stays open
        second = _arrive(s, uncut.edition_id, MAL, env.shelf_id)
        assert second.state == "attached"
        with pytest.raises(AttachRefusedError):
            _separate(s, first.attach_id)  # it moved since
        _separate(s, second.attach_id)
        assert edition_row(env, uncut.edition_id)[0] == s.family.work_id
        assert not _files(env, uncut.edition_id)


def test_a_chain_of_attaches_undone_last_first() -> None:
    with family_env(13) as env:
        s = scene(env)
        uncut = arriving(env)
        rate(env, uncut, 16)
        first = _arrive(s, uncut.edition_id, ANILIST)
        _confirmed(s, first.attach_id)
        _third(env)
        rate(env, s.family, 0)
        second = _arrive(s, uncut.edition_id, MAL, env.shelf_id)
        assert second.state == "attached"
        assert (16, False, "attach") in _open_suggestions(env, uncut.edition_id)
        rate(env, _work_of(env, uncut.edition_id), 0)
        assert not _files(env, uncut.edition_id)
        _separate(s, second.attach_id)
        assert not _files(env, uncut.edition_id)
        _separate(s, first.attach_id)
        assert not _files(env, uncut.edition_id)


def test_an_origin_rated_after_the_attach() -> None:
    with family_env(13) as env:
        s = scene(env)
        uncut = arriving(env)
        with env.engine.begin() as connection:
            suggest_age(
                connection, "work", uncut.work_id, AgeRating(16), provenance="sub_source", now=NOW
            )
        attached = _arrive(s, uncut.edition_id, ANILIST)
        assert attached.state == "attached"
        rate(env, uncut, 16)
        rate(env, s.family, 0)
        assert not _files(env, uncut.edition_id)
        _separate(s, attached.attach_id)
        assert not _files(env, uncut.edition_id)
        assert _arrive(s, uncut.edition_id, ANILIST).state == "suggested"


def test_an_attach_undone_then_made_again() -> None:
    with family_env(13) as env:
        s = scene(env)
        uncut = arriving(env)
        rate(env, uncut, 16)
        first = _arrive(s, uncut.edition_id, ANILIST)
        _confirmed(s, first.attach_id)
        _separate(s, first.attach_id)
        again = _arrive(s, uncut.edition_id, ANILIST, env.shelf_id)
        assert again.state == "suggested"
        _confirmed(s, again.attach_id)
        rate(env, s.family, 0)
        assert not _files(env, uncut.edition_id)


def test_two_concurrent_suggestions_the_second_is_stale() -> None:
    """A -> B and A -> C suggested together: once A -> B is confirmed, A -> C is refused."""
    with family_env(13) as env:
        s = scene(env)
        uncut = arriving(env)
        rate(env, uncut, 16)
        _third(env)
        to_b = _arrive(s, uncut.edition_id, ANILIST)
        to_c = _arrive(s, uncut.edition_id, MAL)
        assert to_b.state == to_c.state == "suggested"
        _confirmed(s, to_b.attach_id)
        with pytest.raises(AttachRefusedError, match="stale"):
            _confirmed(s, to_c.attach_id)
        assert edition_row(env, uncut.edition_id)[0] == s.family.work_id
        rate(env, s.family, 0)
        assert not _files(env, uncut.edition_id)
        _separate(s, to_b.attach_id)
        assert edition_row(env, uncut.edition_id)[0] == uncut.work_id


def test_rating_the_work_it_joins_never_rules_on_its_age() -> None:
    with family_env(13) as env:
        s = scene(env)
        uncut = arriving(env)
        rate(env, uncut, 16)
        attached = _arrive(s, uncut.edition_id, ANILIST)
        _confirmed(s, attached.attach_id)
        for age in (0, 16, 18):
            rate(env, s.family, age)
            assert _open_suggestions(env, uncut.edition_id) == [(16, False, "attach")]
        rate(env, s.family, 0)
        assert not _files(env, uncut.edition_id)
