"""Contradicteur de la #241 : le suivi de génération ne doit jamais lever un gel que la base tient.

L'export ouvre son `Fsops` avec une copie du gel (`loaded.freeze.fork()`), lue à son ouverture.
Pendant qu'il attend son scan, une autre tâche avance la génération, puis un autre écrivain trouve
la base en retard et persiste le gel de l'instance (`instance.restore_generation`, EF-INT-026).
Avant la #241, la sentinelle en avance sur la copie tenue refusait le scan (`restored_behind`).
Avec le suivi, `Fsops` relit la génération en base, la trouve égale et laisse passer le scan,
alors que la base dit `restored_behind` : aucune écriture, suppression ni scan (ADR-0044)."""

from pathlib import Path

import pytest

from shelfhold.config import Settings
from shelfhold.export.domain.physical_roots import RootState
from shelfhold.infrastructure.freeze import FreezeState
from shelfhold.infrastructure.fsops import RootBlockedError
from tests.l41_env import instance


@pytest.mark.req("EF-EXP-029", "EF-INT-026")
def test_follow_never_lifts_a_freeze_the_database_holds(settings: Settings, tmp_path: Path) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        with inst.comp.write_context():
            pass  # the root has a sentinel and a generation
        fsops, _loaded, _tombstones = inst.comp.read_context()  # the export opens its Fsops
        fsops.require_scan_allowed(str(inst.library))
        with inst.comp.write_context():
            pass  # another task moves the generation while the export waits
        with inst.engine.begin() as connection:
            FreezeState().persist(connection, 1)  # another writer froze the instance
        with pytest.raises(RootBlockedError) as caught:
            fsops.require_scan_allowed(str(inst.library))  # followed: the scan goes anyway
        assert caught.value.state is RootState.RESTORED_BEHIND
