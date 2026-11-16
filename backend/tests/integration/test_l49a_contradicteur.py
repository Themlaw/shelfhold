"""#291 (M4 L49a), relecture adverse : chaque trou trouvé, d'abord en `xfail` strict, corrigé.

Le test attend le comportement sûr ; il échoue tant que le trou est là. Quand le trou est corrigé,
le `xfail` strict casse et il suffit d'ôter la marque. Détail et classement : section
« #291 : contradicteur » de `docs/decisions/M4.md`.
"""

from __future__ import annotations

import gc
import hashlib
import struct
import zipfile
from collections.abc import AsyncGenerator
from typing import Any, cast

import anyio
import pytest
import sqlalchemy as sa

from shelfhold.identity.public import DELETION_SITES
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import export
from shelfhold.interfaces.http_api.opds.routes import _closing
from shelfhold.reading.bundle import BundleLimits
from shelfhold.reading.bundle.plan import Candidate, plan_parts
from tests.bundle_env import Chapter, page
from tests.integration.test_l48_opds import World
from tests.integration.test_l49a_bundle import (
    fresh_token,
    lab,  # noqa: F401  (fixture)
    library,
    provider,
    scope_of,
    user_id,
)

pytestmark = pytest.mark.req("EF-INT-007", "ENF-SEC-010")


def _open(api: Any, world: World, token: str) -> Any:
    return api.open(
        user_id=user_id(world, "mum"),
        scope=scope_of(world, "mum"),
        edition_id=world.seed.editions["ok"],
        token_id=token,
    )


def test_poc_blq1_a_reader_that_goes_away_gives_its_slot_back_at_once(lab: World) -> None:  # noqa: F811
    """Starlette itère un flux synchrone par `iterate_in_threadpool`, qui n'appelle jamais `close()`
    sur l'itérateur : sur `http.disconnect`, le générateur asynchrone est abandonné et le créneau
    ne revient qu'au ramasse-miettes cyclique (`BundleStream` et son générateur forment un
    cycle). La route passe donc par `_closing`, qui ferme le flux dans un `finally` : le créneau et
    les descripteurs reviennent à l'instant où la réponse s'arrête, sans le ramasse-miettes (coupé
    ici : le test ne dépend ni de lui ni de l'ordre des fils d'anyio). Deux liseuses dont le Wi-Fi
    coupe ne bloquent pas le bloc pour toute l'instance."""
    lib = library(lab)
    lib.add(Chapter("1", [("1.png", page("png", "gone", 600_000))]))
    api = provider(lab, limits=BundleLimits(chunk_bytes=16_384))
    token = fresh_token(lab)

    async def a_reader_that_disconnects() -> None:
        body = cast(AsyncGenerator[bytes], _closing(_open(api, lab, token).chunks))
        await body.__anext__()
        await body.aclose()  # ce que laisse l'annulation de `StreamingResponse`, au mieux

    gc.disable()
    try:
        anyio.run(a_reader_that_disconnects)
        _open(api, lab, token).chunks.close()  # attendu : le créneau est revenu
    finally:
        gc.enable()
        gc.collect()


def test_poc_maj1_a_reader_that_stops_reading_loses_its_slot(lab: World) -> None:  # noqa: F811
    """Le débit minimal n'est mesuré qu'au `next()` suivant. Un client qui ne lit plus (fenêtre
    TCP à zéro) laisse le thread du serveur bloqué dans `send` : `next()` n'est jamais rappelé,
    le créneau et jusqu'à 256 descripteurs restent pris sans limite de temps (slowloris)."""
    lib = library(lab)
    lib.add(Chapter("1", [("1.png", page("png", "stall", 600_000))]))
    now = [0.0]
    api = provider(lab, limits=BundleLimits(chunk_bytes=16_384), monotonic=lambda: now[0])
    token = fresh_token(lab)
    stalled = _open(api, lab, token)
    next(iter(stalled.chunks))
    now[0] += 3600.0  # une heure sans un octet lu
    try:
        _open(api, lab, token).chunks.close()  # attendu : le flux figé a perdu son créneau
    finally:
        stalled.chunks.close()


def _chapter(n: int, unit: str, number: str) -> Candidate:
    return Candidate(
        f"item{n:02d}", unit, number, (False, float(number), 0, n, unit), "d", f"c{n}.cbz",
        "cbz", 100, 1, f"sha256:{n}", "/lib",
    )  # fmt: skip


def test_poc_maj2_the_boundaries_of_the_parts_are_not_rebuilt_by_a_restore() -> None:
    """`bundle_partition` est exclue de l'export comme « reconstructible », mais ses bornes
    sont un historique : un chapitre inséré au milieu (2.5) reste dans la partie 1 tant que la
    borne écrite tient ; replanifiées à vide après une restauration, les parties changent de
    contenu, donc d'octets et de partialMD5 (positions KOReader perdues)."""
    limits = BundleLimits(part_bytes=300)
    c1, c2, c3, c4 = (_chapter(i, f"u{i}", str(i)) for i in (1, 2, 3, 4))
    first = plan_parts([], [c1, c2, c3, c4], limits)
    stored = [(p.index, p.first_unit_id) for p in first]
    assert stored == [(1, None), (2, "u4")]
    c25 = _chapter(5, "u25", "2.5")
    series = [c1, c2, c25, c3, c4]
    kept = plan_parts(stored, series, limits)
    # The export now carries the boundaries: a restore plans from what it gives back (the real
    # round trip is `test_l49a_history.py`). Planned from nothing, the parts would differ.
    assert [p.state_key for p in plan_parts([], series, limits)] != [p.state_key for p in kept]
    after_restore = plan_parts(stored, series, limits)
    assert [p.state_key for p in after_restore] == [p.state_key for p in kept]


def test_poc_maj3_the_carrier_of_a_block_is_not_a_volume_of_a_range_for_the_exporter(
    lab: World,  # noqa: F811
) -> None:
    """`ensure_partitions` crée un `export_item` de type `bundle` pour porter l'alias d'une
    partie. Ce type existe déjà : c'est le tome sans nom d'une plage (EF-BIB-044,
    `acquisition/ranges.py`, `planner.VOLUME_KINDS`). L'instantané d'export le prend pour un tome
    à écrire ; sans fichier gardé, `source_hash` vaut None et le planificateur bloque
    (`export.ranged_source_missing`) pour chaque destination de l'Édition, à chaque plan."""
    from types import SimpleNamespace

    from shelfhold.export.application.snapshot import _volume_rows

    lib = library(lab)
    lib.add(Chapter.simple("1"))
    _open(provider(lab), lab, fresh_token(lab)).chunks.close()
    with lab.seed.engine.connect() as connection:
        rows = _volume_rows(connection, (SimpleNamespace(edition_id=lab.seed.editions["ok"]),))  # type: ignore[arg-type]
    assert [(r.id, r.unit_ids) for r in rows] == []


def test_poc_min2_the_deleters_of_bundle_rows_are_classed_like_purge_orphans() -> None:
    """La garde EF-USR-024 classe tout émetteur nommé `purge*` ; la classe `database_rows`
    existe pour cela (`_purge_orphans`). `store.purge` renommé `retire` efface des membres et des
    alias sans y figurer, comme `ensure_partitions` (export_item, versions en cascade)."""
    assert "shelfhold/reading/bundle/store.py::purge" in DELETION_SITES
    assert "shelfhold/reading/bundle/plan.py::purge_stale_partitions" in DELETION_SITES


def test_poc_min1_a_chapter_whose_archive_lies_on_a_crc_is_left_out_not_cut_every_time(
    lab: World,  # noqa: F811
) -> None:
    """Un CBZ source dont le CRC déclaré est faux (en-tête local et répertoire central d'accord)
    passe `scan` : le bloc annonce ce CRC, puis chaque téléchargement est coupé à cette page, et
    une version avec son alias est quand même enregistrée. Attendu : le chapitre est écarté
    (`unreadable`) ou vérifié avant le premier octet, et le reste du bloc est servi."""
    lib = library(lab)
    bad = lib.add(Chapter("1", [("1.png", page("png", "bad", 200_000))]))
    lib.add(Chapter.simple("2"))
    data = bytearray(bad.path.read_bytes())
    with zipfile.ZipFile(bad.path) as archive:
        info = archive.infolist()[0]
    struct.pack_into("<I", data, info.header_offset + 14, info.CRC ^ 1)
    struct.pack_into("<I", data, data.find(b"PK\x01\x02") + 16, info.CRC ^ 1)
    bad.path.write_bytes(bytes(data))
    with write_transaction(lab.seed.engine) as c:
        c.execute(
            sa.update(export.export_record)
            .where(export.export_record.c.export_item_id == bad.item_id)
            .values(
                size=len(data), mtime_ns=bad.path.stat().st_mtime_ns,
                file_hash="sha256:" + hashlib.sha256(bytes(data)).hexdigest(),
            )
        )  # fmt: skip
    download = _open(provider(lab), lab, fresh_token(lab))
    assert len(b"".join(download.chunks)) == download.size
