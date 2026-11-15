"""Relecture adverse de la traçabilité M3 : preuves de contournement.

Tests rouges attendus, en `xfail` strict jusqu'au correctif.

1. EF-BIB-021 : `test_empty_folders_wait_for_a_scan_that_failed` donne au service un double de
   scan qui lève `RuntimeError`. En production, `LibraryOps.delete_apply` passe
   `lambda path: self._scan_everywhere([path])`, qui avale `TargetError` (Kavita injoignable) :
   le service croit le scan fait et retire le dossier de série alors qu'aucun scan n'a eu lieu.
2. EF-BIB-004 : le critère d'acceptation (01) dit « deux titres identiques sans identifiant
   commun : une suggestion est créée ». Le code exige un auteur en commun et le test
   `test_same_title_without_any_author_is_never_suggested` affirme le contraire du critère.
"""

from collections.abc import Iterator
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from shelfhold.catalog.application.dedup import suggest_duplicates
from shelfhold.export.application.deletion import DeletionService
from shelfhold.export.targets.kavita.errors import TargetUnreachable
from shelfhold.library.public import DeletionLevel, StoreIndex
from shelfhold.ops.library import LibraryOps
from tests.catalog_env import Harness, harness
from tests.deletion_env import Deletion, spread_unit
from tests.export_env import export_env
from tests.null_tombstones import NONE_PURGED

pytestmark = pytest.mark.req("EF-BIB-021", "EF-BIB-004")


def test_closure_trace_poc_folder_kept_when_kavita_cannot_be_asked_through_the_real_wrapper() -> (
    None
):
    """Même scénario que `test_empty_folders_wait_for_a_scan_that_failed`, mais avec l'enveloppe
    de scan de production (`LibraryOps._scan_everywhere`) au lieu du double qui lève."""
    with export_env() as env:
        asked: list[str] = []

        def unreachable(_target_id: str) -> object:
            asked.append(_target_id)
            raise TargetUnreachable("Kavita is down")

        runtime = SimpleNamespace(engine=env.engine, kavita_client=unreachable)
        ops = LibraryOps(runtime)  # type: ignore[arg-type]
        index = StoreIndex(
            env.lib.engine, env.lib.fsops, str(env.lib.store), NONE_PURGED, hold=nullcontext
        )
        service = DeletionService(
            env.lib.engine, env.lib.fsops, clock=env.clock, locate=env.lib.locate,
            request_scan=lambda path: ops._scan_everywhere([path]),  # as in delete_apply
            refresh_index=index.rewrite, fresh_auth_s=900,
        )  # fmt: skip
        d = Deletion(env, service, index)
        unit = spread_unit(env, env.new_series("Dr Stone"))
        report = d.apply(DeletionLevel.REMOVE_DESTINATIONS, [unit])
        folder = env.lib.manga / "Kavita" / "Dr Stone"
        assert asked, "the Kavita target was asked for a scan"
        # Kavita was never scanned: the folder must wait for a later pass (EF-BIB-021)
        assert folder.is_dir(), "the managed folder was removed although no scan happened"
        assert report.folders_removed == ()  # type: ignore[attr-defined]
        # nothing is lost: the folder is written down, and removed once a scan goes through
        assert service.pending_folders()
        scanned: list[str] = []
        retry = DeletionService(
            env.lib.engine, env.lib.fsops, clock=env.clock, locate=env.lib.locate,
            request_scan=scanned.append, refresh_index=index.rewrite, fresh_auth_s=900,
        )  # fmt: skip
        removed, kept = retry.retry_pending()
        assert scanned and kept == () and removed
        assert not folder.exists()
        assert retry.pending_folders() == ()


@pytest.fixture
def h() -> Iterator[Harness]:
    with harness() as inside:
        yield inside


def test_closure_trace_poc_two_identical_titles_without_common_identifier_are_suggested(
    h: Harness,
) -> None:
    """Le « étant donné » de 01 : deux titres identiques, sans identifiant commun (ni auteur)."""
    h.work("manga", "La Voie")
    h.work("manga", "La Voie")
    created = suggest_duplicates(h.connection, screen=_NoScreen())
    assert len(created) == 1, "01 asks for a suggestion, never a merge, on title alone"


class _NoScreen:
    def places(self, places: object) -> set[str]:
        return set()

    def work_ids(self, ids: object) -> set[tuple[str, str]]:
        return set()
