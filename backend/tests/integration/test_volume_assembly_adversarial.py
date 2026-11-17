"""#326 (M4 L10), relecture adverse intégrité : les trous trouvés, en rouge (xfail strict).

Chaque test décrit un état réel que l'assemblage d'un tome peut laisser, et ce que la décision L10
promet pour lui. Quand le correctif arrive, le test passe et `strict` oblige à retirer le xfail.
"""

from decimal import Decimal

import pytest
import sqlalchemy as sa

from shelfhold.export.application.assembly import (
    AssemblyOutcome,
    AssemblyRequest,
    served_chapters,
)
from shelfhold.export.application.intents import OPEN_STATES, IntentJournal
from shelfhold.export.application.manifest_write import adopt_written_hook
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import coherence, export
from tests.assembly_env import bin_files, chapter, exported_chapters, request, service
from tests.export_env import export_env
from tests.integration.test_schema_catalog import add_unit

pytestmark = pytest.mark.req("EF-EXP-019", "EF-BIB-044")


@pytest.mark.xfail(
    strict=True,
    reason="#326 contradicteur (1) : `intents resolve --replay` ferme une intention `assemble` "
    "sans sa comptabilité (seul `adopt_written_hook` est passé, ops/intents.py `_replay`)",
)
def test_intents_replay_of_an_assembly_cut_short_writes_its_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Une erreur après la pose du tome laisse l'intention ouverte (« the boot (or `intents
    resolve`) settles it », assembly.py). Rejouée par la commande d'admin, elle doit finir comme au
    démarrage : le tome au manifeste, les chapitres `recycled`, une ligne `export_assembly`."""
    with export_env() as env:
        chapters = exported_chapters(env, [4, 3, 5])
        real = Fsops.recycle
        calls = [0]

        def flaky(self: Fsops, *args: object, **kwargs: object) -> object:
            calls[0] += 1
            if calls[0] == 2:  # le 2e chapitre : un disque qui hoquette, pas un kill
                raise OSError("EIO")
            return real(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Fsops, "recycle", flaky)
        with pytest.raises(OSError):
            service(env).assemble(request(env, chapters))
        monkeypatch.setattr(Fsops, "recycle", real)
        fs_intent = coherence.fs_intent
        with env.engine.connect() as connection:
            (intent_id,) = connection.execute(
                sa.select(fs_intent.c.id).where(
                    fs_intent.c.op == "assemble", fs_intent.c.state.in_(OPEN_STATES)
                )
            ).scalars()

        # ce que lance `shelfhold intents resolve <id>` en rejeu (ops/intents.py `_replay`)
        IntentJournal(env.engine, env.lib.fsops, clock=env.clock).resolve_one(
            intent_id, on_done=adopt_written_hook(env.lib.fsops, env.lib.locate, env.clock)
        )

        assert len(env.files()) == 1 and len(bin_files(env)) == 3  # le disque : un tome
        with env.engine.connect() as connection:
            assemblies = connection.execute(
                sa.select(sa.func.count()).select_from(export.export_assembly)
            ).scalar_one()
        states = sorted(str(r.state) for r in env.rows(export.export_record))
        # le manifeste doit dire la même chose : sinon trois lignes `written` sur des fichiers
        # absents, un tome que rien ne connaît, et des chapitres à la corbeille sans `trash_item`
        assert states == ["recycled"] * 3 + ["written"]
        assert assemblies == 1


@pytest.mark.xfail(
    strict=True,
    reason="#326 contradicteur (2) : `--first 1 --last 4` avec le chapitre 3 manquant fait un "
    "tome 1, 2, 4 sans rien dire (`served_chapters` saute les trous)",
)
def test_a_range_with_a_hole_is_never_assembled_silently() -> None:
    """Le porteur lit des séries entières dans KOReader : un tome qui saute un chapitre connu du
    catalogue (pas encore reçu) le lui cache, puis le chapitre arrive seul, après le tome fermé."""
    with export_env() as env:
        series = env.new_series()
        for number in ("1", "2", "4"):
            chapter(env, series, number, 2)
        with write_transaction(env.engine) as connection:
            add_unit(connection, series.edition_id, "3")  # connu, pas encore reçu
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        with env.engine.connect() as connection:
            items = served_chapters(
                connection, env.destination_id, series.edition_id, Decimal(1), Decimal(4)
            )
        assert len(items) == 3

        result = service(env).assemble(AssemblyRequest(env.destination_id, tuple(items), "1"))

        assert result.outcome is not AssemblyOutcome.ASSEMBLED
