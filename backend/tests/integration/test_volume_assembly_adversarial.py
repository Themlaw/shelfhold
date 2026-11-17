"""#326 (M4 L10), relecture adverse intégrité : les trous trouvés, corrigés.

Chaque test décrit un état réel que l'assemblage d'un tome peut laisser, et ce que la décision L10
promet pour lui.
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
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import coherence, export
from shelfhold.ops.settle_hooks import settle_hooks
from tests.assembly_env import bin_files, chapter, exported_chapters, request, service
from tests.export_env import export_env
from tests.integration.test_schema_catalog import add_unit

pytestmark = pytest.mark.req("EF-EXP-019", "EF-BIB-044")


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

        # ce que lance `shelfhold intents resolve <id>` en rejeu (ops/intents.py `_replay`) :
        # la table de crochets du démarrage
        IntentJournal(env.engine, env.lib.fsops, clock=env.clock).resolve_one(
            intent_id,
            on_done=settle_hooks(
                env.engine, env.lib.fsops, env.lib.locate, data_dir=env.lib.data, clock=env.clock
            ),
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
        assert result.outcome is AssemblyOutcome.GAP and result.missing == ("3",)


def test_intents_replay_runs_the_same_hooks_as_the_boot() -> None:
    """Une seule table de crochets : chaque type d'intention qui doit une comptabilité y a son
    crochet, et le rejeu d'admin et le démarrage prennent la même."""
    import inspect

    from shelfhold import runtime
    from shelfhold.ops import intents, settle_hooks

    assert "settle_hooks(" in inspect.getsource(runtime)
    assert "settle_hooks(" in inspect.getsource(intents)
    body = inspect.getsource(settle_hooks)
    for hook in ("adopt", "renamed", "upgraded", "assembled", "repaired"):
        assert f"{hook}(connection, intent)" in body


def test_a_volume_is_assembled_from_a_chapter_with_an_avif_page() -> None:
    """L53 : une page AVIF d'un chapitre ne sort jamais telle quelle dans le tome."""
    from shelfhold.infrastructure.content_trust.cbz_entries import sniff_page_kind
    from tests import page_formats_env as pf
    from tests.assembly_env import page_bytes

    if not pf.has_avif():
        pytest.skip("this Pillow has no AVIF")
    with export_env() as env:
        chapters = exported_chapters(env, [2, 2], first_page={0: ("1-001.avif", pf.avif())})
        result = service(env).assemble(request(env, chapters))
        assert result.outcome is AssemblyOutcome.ASSEMBLED
        (volume,) = env.files()
        kinds = {sniff_page_kind(b[:16]) for b in page_bytes(volume)}
        assert kinds <= {"jpeg", "png", "gif", "webp"}
        assert len(page_bytes(volume)) == 4
