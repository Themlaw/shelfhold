"""Contradicteur L30 (M3, EF-EXP-031): preuves rouges des trous trouvés en relecture adverse.

Chaque test décrit le comportement attendu ; il échoue sur la branche telle qu'elle est.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.detach import (
    DetachRefusedError,
    FullDetacher,
    full_detach_phrase,
)
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.export.application.remove_destination import (
    RemovalMode,
    RemovalRefusedError,
    removal_phrase,
)
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, platform
from tests.detach_env import ADMIN, DetachEnv, detach_env
from tests.rename_env import export_chapters

pytestmark = pytest.mark.req("EF-EXP-031")


class Killed(BaseException):
    """`kill -9`, vu de l'intérieur : rien ne s'exécute après."""


class KavitaLikeRevoker:
    """Ce que fait la vraie porte (`headless/adapters.py::_revoke`) : la clé est cherchée par
    valeur puis par nom ; une clé déjà supprimée n'est plus trouvée et la réponse est False."""

    def __init__(self) -> None:
        self.alive = True
        self.calls = 0

    def revoke(self, target_id: str, *, credential: str | None) -> bool:
        self.calls += 1
        if not self.alive:
            return False
        self.alive = False
        return True


@pytest.fixture
def d() -> Iterator[DetachEnv]:
    with detach_env() as made:
        export_chapters(made.env, "Dr Stone", ["1", "2"])
        yield made


def _clean_bins(d: DetachEnv) -> None:
    import shutil

    for name in ("recycle", "received"):
        shutil.rmtree(d.root / ".shelfhold" / name, ignore_errors=True)
    shutil.rmtree(d.env.lib.manga / ".shelfhold", ignore_errors=True)


def _detach(d: DetachEnv, revoker: Any, detacher: FullDetacher | None = None) -> Any:
    target = d.env.target_id
    return (detacher or d.detacher).apply(
        target, principal=ADMIN, typed=full_detach_phrase(target), client=d.wired.client,
        revoker=revoker, credential="the account password",
    )  # fmt: skip


def _series_folder(d: DetachEnv) -> str:
    first = d.env.files()[0]
    return Path(first).relative_to(d.env.root).parts[0]


# -- B1 : la clé révoquée avant l'oubli du manifeste, puis un arrêt ------------------------------


def test_detach_killed_after_the_key_is_revoked_still_finishes_on_rerun(
    d: DetachEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Arrêt entre la révocation (faite dans Kavita) et la transaction `_forget` : la même commande
    relancée doit finir. Kavita ne connaît plus la clé, la porte répond False, et le détachement
    reste `key_not_revoked` pour toujours : la cible n'est jamais oubliée."""
    _clean_bins(d)
    revoker = KavitaLikeRevoker()

    def die(*_a: Any, **_k: Any) -> Any:
        raise Killed

    monkeypatch.setattr(FullDetacher, "_forget", die)
    with pytest.raises(Killed):
        _detach(d, revoker)
    monkeypatch.undo()
    assert not revoker.alive  # la clé est bien partie dans Kavita
    report = _detach(d, revoker)
    assert report.complete, [line.code for line in report.leftovers]
    assert d.count(export.shelf_destination) == 0


# -- B2 : les verrous du retrait et du détachement n'excluent pas un export ----------------------


def test_remove_destination_refused_while_an_export_holds_a_series_of_it(d: DetachEnv) -> None:
    """Un export tient `work:<id>` + `path:<destination>/<dossier>` (pipeline.py) ; le retrait
    tient `path:<chemin absolu>` : deux noms différents, aucune exclusion."""
    locks = ResourceLocks(d.env.engine, clock=d.env.clock)
    d.remover._locks = locks  # le câblage de production (ops/detach.py) en donne un
    dest = d.env.destination_id
    with locks.hold(new_ulid(), work=new_ulid(), series=f"{dest}/{_series_folder(d)}"):
        with pytest.raises(RemovalRefusedError) as refused:
            d.remover.apply(
                dest, RemovalMode.DELETE_FILES, principal=ADMIN, typed=removal_phrase(dest)
            )
    assert refused.value.reason == "busy"


def test_detach_refused_while_an_export_holds_a_series_of_the_target(d: DetachEnv) -> None:
    _clean_bins(d)
    locks = ResourceLocks(d.env.engine, clock=d.env.clock)
    d.detacher._locks = locks
    dest = d.env.destination_id
    with locks.hold(new_ulid(), work=new_ulid(), series=f"{dest}/{_series_folder(d)}"):
        with pytest.raises(DetachRefusedError) as refused:
            _detach(d, KavitaLikeRevoker())
    assert refused.value.reason == "busy"


# -- M1 : la reprise du retrait supprime sans revérifier le contenu ------------------------------


def test_remove_resume_never_deletes_a_file_changed_after_the_stop(
    d: DetachEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Arrêt après `mark_applying`, avant l'effacement ; le fichier est ensuite remplacé par un
    autre contenu (l'admin, un outil). Au premier passage ce fichier aurait été laissé
    (`changed_since_export`) ; à la reprise, `_settle_own` l'efface sans comparer l'empreinte."""
    dest = d.env.destination_id
    real = d.journal.mark_applying

    def die(*a: Any, **k: Any) -> Any:
        real(*a, **k)
        raise Killed

    monkeypatch.setattr(d.journal, "mark_applying", die)
    with pytest.raises(Killed):
        d.remover.apply(dest, RemovalMode.DELETE_FILES, principal=ADMIN, typed=removal_phrase(dest))
    monkeypatch.undo()
    (open_intent,) = d.intents("applying")
    victim = Path(str(open_intent.src_path))
    victim.write_bytes(b"the admin's own edit, not what Shelfhold wrote")
    d.remover.apply(dest, RemovalMode.DELETE_FILES, principal=ADMIN, typed=removal_phrase(dest))
    assert victim.exists(), "a file changed since the export was deleted on resume"


# -- M2 : une bibliothèque supprimée dans Kavita bloque le détachement pour toujours -------------


def test_detach_finishes_when_the_kavita_library_is_gone(d: DetachEnv) -> None:
    """L'admin a supprimé la bibliothèque dans Kavita : il n'y a plus rien à restaurer. Le
    détachement doit le dire et finir ; il reste `settings_not_restored` sans issue."""
    _clean_bins(d)
    d.wired.fake.libraries.clear()
    report = _detach(d, KavitaLikeRevoker())
    assert report.complete, [line.code for line in report.leftovers]


# -- M3 : `--give-up` après l'effacement laisse une cible vivante sur une racine rétrogradée -----


def test_detach_give_up_after_the_erasure_leaves_a_coherent_state(
    d: DetachEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shelfhold.infrastructure.fsops import Fsops

    _clean_bins(d)
    real = Fsops.erase_host_area

    def die(*a: Any, **k: Any) -> Any:
        real(*a, **k)
        raise Killed

    monkeypatch.setattr(Fsops, "erase_host_area", die)
    with pytest.raises(Killed):
        _detach(d, KavitaLikeRevoker())
    monkeypatch.undo()
    report = d.detacher.settle(d.env.target_id, resume=False, principal=ADMIN)
    with d.env.engine.connect() as conn:
        modes = {r.mode for r in conn.execute(sa.select(platform.physical_root.c.mode))}
    target_alive = d.count(export.shelf_destination) == 1
    # soit l'abandon dit que l'effacement est resté (rien ne le dit : `cancelled` == 0, le CLI
    # affiche « 0 abandonnée »), soit la cible reste utilisable (racine encore `rw`)
    assert not (target_alive and "adopt_ro" in modes and report.cancelled == 0), (
        "give-up: target alive, its root demoted and sentinel-less, and the report says nothing"
    )
