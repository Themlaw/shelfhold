"""#339 (M4 L35), relecture adverse : les trous trouvés, chacun en rouge (xfail strict).

Chaque test dit ce qui devrait être vrai ; il échoue tant que le trou est ouvert. Quand le code est
corrigé, le test passe, xfail strict le signale, et l'on retire la marque.
"""

import contextlib
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.team_switch import StaleSwitchError, TeamSwitchService
from shelfhold.catalog.application.dto import BindingRef
from shelfhold.catalog.public import Catalog
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.runtime_jobs import team_switch_handler
from tests.export_env import export_env
from tests.integration.test_m4_l35_team_switch_job import _comp, _context
from tests.team_switch_env import (
    NOW,
    RealUpgrader,
    add_team_chapter,
    preferred_variant,
    team_switch,
)
from tests.upgrade_env import bin_files, mark_read

pytestmark = pytest.mark.req("EF-BIB-005")

ADMIN = Principal("01J00000000000000000000ADM", "admin", "admin", Credential.LOCAL_CLI)


def _queued(service: TeamSwitchService, edition_id: str) -> str:
    switch_id = str(service.preview(ADMIN, edition_id)["id"])
    service.queue(ADMIN, edition_id, switch_id)
    return switch_id


@pytest.mark.xfail(
    strict=True,
    reason="#339 trou 1 : la reprise (`applying`) applique le plan d'aujourd'hui, pas l'aperçu lu",
)
def test_a_resumed_switch_never_applies_what_the_preview_did_not_say() -> None:
    """La personne a lu « passer à Team B ». Le processus meurt en `applying`. Avant la reprise,
    l'équipe préférée devient Team C (un autre clic sur la fiche). La reprise ne doit remplacer
    que ce que l'aperçu a dit : jamais un fichier de Team C que personne n'a vu dans un aperçu."""
    with export_env() as env:
        s = team_switch(env)
        old = s.target.read_bytes()
        service = TeamSwitchService(env.engine, env.clock, submit=lambda *_a: "01JJOB")
        switch_id = _queued(service, s.edition_id)
        with write_transaction(env.engine) as connection:  # tué juste après `_mark("applying")`
            connection.execute(sa.text("UPDATE team_switch SET state = 'applying'"))
            team_c = (
                Catalog(connection)
                .resolve_team(BindingRef(s.exported.series.binding_id, s.edition_id), "Team C", NOW)
                .id
            )
        variant_c, _content_c = add_team_chapter(
            env, s.edition_id, s.exported.series.binding_id, s.unit_id, "1", team_c, tag=b"team-c"
        )
        with write_transaction(env.engine) as connection:
            Catalog(connection).prefer_team(s.edition_id, team_c)

        team_switch_handler(_comp(env, []))(_context(env, switch_id))

        report = service.report(ADMIN, s.edition_id, switch_id)
        assert preferred_variant(env, s.unit_id) != variant_c, (
            "la reprise a basculé l'Unité vers Team C, qu'aucun aperçu n'a montré "
            f"(rapport : switched={report['switched']}, blocked={report['blocked']})"
        )
        assert b"team-c" not in s.target.read_bytes() or s.target.read_bytes() == old


class _DiesOnSecond:
    """Le premier chapitre passe ; le second lève (verrou pris 30 s, copie de bibliothèque absente,
    disque plein…) : la tâche finit `failed` avec un chapitre déjà remplacé."""

    def __init__(self, env: Any) -> None:
        self.real = RealUpgrader(env)
        self.calls = 0

    def upgrade_unit(self, unit_id: str, content_id: str) -> list[str]:
        self.calls += 1
        if self.calls == 2:
            raise TimeoutError("lock of the series folder held")
        return self.real.upgrade_unit(unit_id, content_id)


@pytest.mark.xfail(
    strict=True,
    reason="#339 trou 2 : une tâche `failed` à mi-chemin, reprise par `jobs retry`, devient "
    "`stale` et le rapport dit « rien n'est écrit » alors qu'un fichier a été remplacé",
)
def test_a_failed_switch_retried_ends_the_work_or_says_what_was_done() -> None:
    with export_env() as env:
        s = team_switch(env)
        series = s.exported.series
        second = env.add_chapter(series, "2")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.text("UPDATE unit_variant SET preferred = 1 WHERE unit_id = :u"), {"u": second}
            )
        add_team_chapter(env, s.edition_id, series.binding_id, second, "2", s.team_b, tag=b"two")
        service = TeamSwitchService(env.engine, env.clock, submit=lambda *_a: "01JJOB")
        switch_id = _queued(service, s.edition_id)

        with pytest.raises(TimeoutError):
            service.run(switch_id, upgrader=_DiesOnSecond(env))
        assert len(bin_files(env)) == 1  # le premier chapitre est remplacé, sur le disque
        assert service.report(ADMIN, s.edition_id, switch_id)["state"] == "failed"

        with contextlib.suppress(StaleSwitchError):  # `jobs retry` : la même tâche, la même `run`
            service.run(switch_id, upgrader=RealUpgrader(env))
        report = service.report(ADMIN, s.edition_id, switch_id)
        assert report["state"] == "done", (
            f"état {report['state']!r} : un fichier remplacé, le second jamais fini, et le rapport "
            f"ne nomme rien de fait (switched={report['switched']})"
        )
        assert sorted(report["switched"]) == sorted([s.unit_id, second])


@pytest.mark.xfail(
    strict=True,
    reason="#339 trou 3 (antérieur à L35, atteint par lui) : « chapitre lu » est regardé avant le "
    "rendu, hors des verrous et de l'intention ; sous les verrous on ne revérifie que l'empreinte",
)
def test_a_chapter_read_while_the_new_file_renders_is_not_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La synchronisation de progression (Kavita, KOReader) ne prend pas le bail de `/library` :
    un « lu » peut arriver entre la garde et l'échange. Le fichier lu ne doit pas être remplacé."""
    import shelfhold.export.application.upgrade as upgrade_module

    with export_env() as env:
        s = team_switch(env)
        old = s.target.read_bytes()
        from shelfhold.export.application.rendering import comicinfo_xml as real_xml

        fired: list[bool] = []

        def xml_then_read(*args: Any, **kwargs: Any) -> Any:
            if not fired:  # entre `_refusal` (passé) et `_replace` : le lecteur finit le chapitre
                fired.append(True)
                mark_read(env, s.exported)
            return real_xml(*args, **kwargs)

        monkeypatch.setattr(upgrade_module, "comicinfo_xml", xml_then_read)
        service = TeamSwitchService(env.engine, env.clock, submit=lambda *_a: "01JJOB")
        switch_id = _queued(service, s.edition_id)
        service.run(switch_id, upgrader=RealUpgrader(env))
        assert fired
        assert s.target.read_bytes() == old, "un chapitre lu a été remplacé (garde TOCTOU)"
