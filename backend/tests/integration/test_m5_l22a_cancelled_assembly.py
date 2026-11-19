"""M5 L22a (EF-EXP-019, EF-BIB-044, M4 #326 c debt): a cancelled assembly gives back its chapters.

An assembly writes the volume, then sends each chapter file to the bin of its root, then keeps the
books. When it is cut short and a third party removes the volume before the next start, the journal
used to close the intent `cancelled` with the chapters already in the bin and the books still
saying `written`: files absent, no `trash_item`, nothing that `restore` could find. A cancelled
assembly now puts back the chapters it had taken, one by one, and only what it can prove is its
own: a path that another file has taken is drift, never overwritten.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from shelfhold.export.application.intents import OPEN_STATES, IntentJournal
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import coherence, export
from shelfhold.ops.settle_hooks import settle_hooks
from tests.assembly_env import bin_files, exported_chapters, request, service
from tests.export_env import ExportEnv, export_env

pytestmark = pytest.mark.req("EF-EXP-019", "EF-BIB-044")


def _cut_after_one_chapter(env: ExportEnv, monkeypatch: pytest.MonkeyPatch) -> tuple[list, str]:  # type: ignore[type-arg]
    """An assembly of three chapters that dies at the second `recycle`: the volume is written, the
    first chapter is in the bin, two are still served. Returns the chapters and the intent."""
    chapters = exported_chapters(env, [4, 3, 5])
    real = Fsops.recycle
    calls = [0]

    def flaky(self: Fsops, *args: object, **kwargs: object) -> object:
        calls[0] += 1
        if calls[0] == 2:
            raise OSError("EIO")
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Fsops, "recycle", flaky)
    with pytest.raises(OSError):
        service(env).assemble(request(env, chapters))
    monkeypatch.setattr(Fsops, "recycle", real)
    with env.engine.connect() as connection:
        (intent_id,) = connection.execute(
            sa.select(coherence.fs_intent.c.id).where(
                coherence.fs_intent.c.op == "assemble",
                coherence.fs_intent.c.state.in_(OPEN_STATES),
            )
        ).scalars()
    return list(chapters.targets), str(intent_id)


def _resolve(env: ExportEnv, intent_id: str) -> object:
    return IntentJournal(env.engine, env.lib.fsops, clock=env.clock).resolve_one(
        intent_id,
        on_done=settle_hooks(
            env.engine, env.lib.fsops, env.lib.locate, data_dir=env.lib.data, clock=env.clock
        ),
    )


def _intent_state(env: ExportEnv, intent_id: str) -> str:
    with env.engine.connect() as connection:
        return str(
            connection.execute(
                sa.select(coherence.fs_intent.c.state).where(coherence.fs_intent.c.id == intent_id)
            ).scalar_one()
        )


def test_cancelled_intent_restores_chapters(monkeypatch: pytest.MonkeyPatch) -> None:
    """The volume is removed by a third party: the intent is cancelled, the first chapter comes
    back out of the bin at its own path with its own bytes, the other two were never touched, and
    the books say what the disk says (three chapters `written`, no assembly)."""
    with export_env() as env:
        targets, intent_id = _cut_after_one_chapter(env, monkeypatch)
        before = {t: t.read_bytes() for t in targets[1:]}
        (volume,) = [p for p in env.files() if p not in targets]
        volume.unlink()
        assert not targets[0].exists() and len(bin_files(env)) == 1

        report = _resolve(env, intent_id)

        assert report.abandoned == [intent_id]  # type: ignore[attr-defined]
        assert _intent_state(env, intent_id) == "cancelled"
        assert sorted(env.files()) == sorted(targets)  # the three chapters, nothing else
        assert bin_files(env) == []
        assert all(t.read_bytes() == b for t, b in before.items())
        states = sorted(str(r.state) for r in env.rows(export.export_record))
        assert states == ["written"] * 3
        with env.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(export.export_assembly)
                ).scalar_one()
                == 0
            )


def test_a_cancelled_intent_never_overwrites_what_took_the_chapters_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A third party put another file where the first chapter was: the chapter stays in the bin,
    the other file is not touched, and the intent stays open as drift for an admin."""
    with export_env() as env:
        targets, intent_id = _cut_after_one_chapter(env, monkeypatch)
        (volume,) = [p for p in env.files() if p not in targets]
        volume.unlink()
        targets[0].write_bytes(b"someone else's file")

        report = _resolve(env, intent_id)

        assert intent_id in report.drifted  # type: ignore[attr-defined]
        assert _intent_state(env, intent_id) in OPEN_STATES
        assert targets[0].read_bytes() == b"someone else's file"
        assert len(bin_files(env)) == 1  # the chapter is still where it was put, nothing lost


def test_a_cancelled_intent_with_nothing_in_the_bin_is_closed_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No chapter was taken yet (the volume was never written): nothing to give back."""
    with export_env() as env:
        chapters = exported_chapters(env, [4, 3])
        real = Fsops.recycle
        monkeypatch.setattr(
            Fsops, "recycle", lambda *_a, **_k: (_ for _ in ()).throw(OSError("EIO"))
        )
        with pytest.raises(OSError):
            service(env).assemble(request(env, chapters))
        monkeypatch.setattr(Fsops, "recycle", real)
        with env.engine.connect() as connection:
            (intent_id,) = connection.execute(
                sa.select(coherence.fs_intent.c.id).where(
                    coherence.fs_intent.c.op == "assemble",
                    coherence.fs_intent.c.state.in_(OPEN_STATES),
                )
            ).scalars()
        (volume,) = [p for p in env.files() if p not in chapters.targets]
        volume.unlink()

        report = _resolve(env, str(intent_id))

        assert report.abandoned == [str(intent_id)]  # type: ignore[attr-defined]
        assert sorted(env.files()) == sorted(chapters.targets)
