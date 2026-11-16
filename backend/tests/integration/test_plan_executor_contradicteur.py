"""#299 (M4 L04), relecture adverse : les trous trouvés dans l'exécuteur des plans de destination.

Chaque test décrit ce que l'exécuteur devrait faire et échoue aujourd'hui (`xfail` strict) : il
passera au vert, et `strict` le signalera, quand le trou sera bouché. Section « #299 :
contradicteur » de `docs/decisions/M4.md`.
"""

import os
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.plan_executor import PlanBinding, PlanRefusedError
from shelfhold.export.application.target_state import DestinationPausedError, save_fingerprint
from shelfhold.export.domain.bindings import TreeOverlapError
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, platform
from shelfhold.sdk.api_call import ApiCall
from tests.export_env import ROOT_SUBPATH, export_env
from tests.plan_executor_env import (
    BOOK_A,
    BOOK_B,
    BYTES,
    FINGERPRINT,
    ExecEnv,
    make_exec_env,
    second_binding,
)


def _write(rel: str, item: str) -> dict[str, Any]:
    return {"op": "write", "rel_path": rel, "content_ref": {"kind": "export_item", "ref": item}}


@pytest.fixture
def x() -> Iterator[ExecEnv]:
    with export_env() as env:
        ex = make_exec_env(env)
        with write_transaction(env.engine) as connection:
            save_fingerprint(connection, env.target_id, FINGERPRINT, "0.9.0", env.clock.now())
        yield ex


@pytest.mark.xfail(strict=True, reason="#299 M1 : reprise par indice, pas par opération")
def test_a_second_plan_for_the_same_call_is_not_skipped_by_index(x: ExecEnv) -> None:
    """Un appel relancé avec un plan régénéré (l'ordre ou le contenu a changé) : l'opération 0 du
    nouveau plan est sautée parce que l'opération 0 de l'ancien a une intention `done`. Le
    volume 2 n'est jamais écrit et le rapport dit « sauté ». Attendu : l'exécuteur refuse un plan
    qui n'est pas celui des intentions trouvées, ou exécute l'opération qui n'a jamais été faite."""
    x.plan_for("c1", BOOK_A, BOOK_B)
    x.executor.execute(x.binding, "c1", {"ops": [_write("Series A/Volume 1.cbz", BOOK_A)]})
    try:
        x.executor.execute(x.binding, "c1", {"ops": [_write("Series A/Volume 2.cbz", BOOK_B)]})
    except PlanRefusedError:
        return
    assert (x.root / "Series A" / "Volume 2.cbz").read_bytes() == BYTES[BOOK_B]


@pytest.mark.xfail(strict=True, reason="#299 M2 : run_calls ne relie pas la cible à la liaison")
def test_run_calls_refuses_a_target_that_is_not_the_one_of_the_binding(x: ExecEnv) -> None:
    """`PlanBinding.target_id` vient de l'appelant. `execute` le recoupe par `tree_indexer`,
    `run_calls` jamais : l'empreinte contrôlée est celle d'un autre serveur, et la pause de la
    vraie cible est contournée. Attendu : refus quand `shelf_destination.target_id` diffère."""
    other = second_binding(x, new_target=True)
    crossed = PlanBinding(
        destination_id=x.binding.destination_id,
        target_id=other.target_id,
        capabilities=x.binding.capabilities,
    )
    x.plan_for("c1")
    sent: list[str] = []
    call = ApiCall(method="POST", path="/api/scan", auth="target_admin")
    with pytest.raises((PlanRefusedError, DestinationPausedError)):
        x.executor.run_calls(crossed, "c1", [call], send=lambda c: sent.append(c.path))
    assert sent == []


@pytest.mark.xfail(strict=True, reason="#299 M3 : LINKED jamais évalué par l'exécuteur")
def test_one_folder_reached_through_two_roots_is_one_tree(x: ExecEnv) -> None:
    """`_load_tree` ne charge ni `(st_dev, st_ino)` ni l'ascendance : `relation` ne compare que
    dans une même racine, donc un dossier atteint par deux racines (montage lié, ou deux entrées
    pour un même dossier, comme ici) est `APART` et a deux écrivains. Attendu :
    `TreeOverlapError`."""
    env = x.env
    other = second_binding(x, new_target=False)
    twin_root = new_ulid()
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.insert(platform.physical_root).values(
                root_id=twin_root, path_entry=f"{env.lib.library}/", generation=1
            )
        )
        tree_id = connection.execute(
            sa.select(export.tree_indexer.c.tree_id).where(
                export.tree_indexer.c.shelf_destination_id == other.destination_id
            )
        ).scalar_one()
        connection.execute(
            sa.update(export.tree)
            .where(export.tree.c.id == tree_id)
            .values(root_id=twin_root, root_subpath=f"manga/{ROOT_SUBPATH}")
        )
    x.plan_for("c1", BOOK_B, binding=other)
    with pytest.raises((TreeOverlapError, PlanRefusedError)):
        x.executor.execute(other, "c1", {"ops": [_write("Series B/Volume 2.cbz", BOOK_B)]})
    assert not (x.root / "Series B").exists()


@pytest.mark.xfail(strict=True, reason="#299 M4 : touch_mtime remplace l'inode")
def test_touch_mtime_keeps_the_inode_and_its_links(x: ExecEnv) -> None:
    """`touch_mtime` réécrit le fichier entier par `write_atomic(replace=True)` : l'écriture est
    atomique (pas de troncature possible), mais l'inode change. Un fichier lié en dur
    (EF-EXP-015, `link_replaceable`) perd son lien et occupe deux fois sa taille ; un fichier
    adopté, qui n'est pas le nôtre, est réécrit par l'hôte. Attendu : même inode, `nlink` gardé."""
    x.plan_for("c1", BOOK_A)
    x.executor.execute(x.binding, "c1", {"ops": [_write("Series A/Volume 1.cbz", BOOK_A)]})
    served = x.root / "Series A" / "Volume 1.cbz"
    os.link(served, x.env.lib.manga / "kept-copy.cbz")
    before = served.stat()
    x.plan_for("c2")
    x.executor.execute(
        x.binding,
        "c2",
        {"ops": [{"op": "touch_mtime", "rel_path": "Series A/Volume 1.cbz",
                  "mtime": "2020-01-02T03:04:05+00:00"}]},
    )  # fmt: skip
    after = served.stat()
    assert (after.st_ino, after.st_nlink) == (before.st_ino, before.st_nlink)


@pytest.mark.xfail(strict=True, reason="#299 M5 : sidecar ni planifié ni filtré")
def test_a_sidecar_is_planned_and_never_one_the_destination_forbids(x: ExecEnv) -> None:
    """Un `write_inline` qui cite un `sidecar` échappe à `check_content_ref` : l'exécuteur lit au
    `ContentStore` une référence que rien n'a planifiée pour cet appel ni pour cette liaison, et
    il écrit un `series.json` que Kavita déclare dans `forbidden_sidecars` (seul le kit de
    conformité le vérifie, jamais l'exécution). Attendu : refus, aucun octet lu."""
    x.contents.data["sidecar-of-another-binding"] = b"{}"
    x.plan_for("c1")
    sidecar = {"kind": "sidecar", "ref": "sidecar-of-another-binding"}
    plan = {
        "ops": [{"op": "write_inline", "rel_path": "Series A/series.json", "content_ref": sidecar}]
    }
    with pytest.raises(PlanRefusedError):
        x.executor.execute(x.binding, "c1", plan)
    assert x.contents.reads == []


@pytest.mark.xfail(strict=True, reason="#299 m1 : empreinte lue une fois par passage")
def test_an_instance_that_changes_during_a_run_stops_the_next_write(x: ExecEnv) -> None:
    """L'empreinte est lue au départ du passage, plus ensuite : sur 10 000 opérations, un serveur
    remplacé en cours de route reçoit le reste du plan. Attendu : la pause arrête l'opération
    suivante (ou au moins la prochaine écriture)."""
    real = x.contents.read

    def read_then_swap(destination_id: str, ref: Any) -> bytes:
        data = real(destination_id, ref)
        x.probe.current = "sha256:" + "b" * 64  # the server is replaced after the first read
        return data

    x.contents.read = read_then_swap  # type: ignore[method-assign,assignment]
    x.plan_for("c1", BOOK_A, BOOK_B)
    plan = {
        "ops": [_write("Series A/Volume 1.cbz", BOOK_A), _write("Series A/Volume 2.cbz", BOOK_B)]
    }
    with pytest.raises(DestinationPausedError):
        x.executor.execute(x.binding, "c1", plan)
    assert not (x.root / "Series A" / "Volume 2.cbz").exists()
