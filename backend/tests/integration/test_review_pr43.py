"""Regressions of the PR #43 adversarial review PoCs (rotation under SIGKILL, forged rmdir)."""

import os
import re
import signal
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.auth_env import auth_env, run_cli

KEY = re.compile(r"shk_([0-9a-f]{8})_[A-Za-z0-9_-]{43}")


def _touch(p: Path) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"precious")
    return p


# --- Q4 : rotation ------------------------------------------------------------------------
CHILD = textwrap.dedent(
    """
    import os, signal, sys
    from shelfhold.identity.adapters import store as s
    when = sys.argv[1]
    if when == "audit":
        def boom(self, audit):
            os.kill(os.getpid(), signal.SIGKILL)
        s.SqlIdentityStore._audit_insert = boom
    else:  # after both writes, at commit time
        import sqlalchemy as sa
        orig = sa.engine.Connection.commit
        def commit(self):
            os.kill(os.getpid(), signal.SIGKILL)
        sa.engine.base.Transaction.commit = lambda self: os.kill(os.getpid(), signal.SIGKILL)
    import shelfhold.interfaces.cli.admin as admin
    admin._server_socket = lambda settings: None  # this is the offline channel: the database
    from shelfhold.interfaces.cli.registry import main
    sys.exit(main(["admin", "keys", "rotate", sys.argv[2]]))
    """
)


@pytest.mark.req("EF-USR-003")
@pytest.mark.parametrize("when", ["audit", "commit"])
def test_q4_sigkill_between_insert_and_commit_leaves_old_key_alive(
    monkeypatch, capsys, settings, when
):
    ctx = auth_env(monkeypatch, settings)
    ctx.__enter__()
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "admin", "keys", "create", "--name", "s") == 0
    prefix = KEY.search(capsys.readouterr().out).group(1)
    ctx.__exit__(None, None, None)  # server stopped: the CLI takes the flock and acts directly
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", CHILD, when, prefix],
        env={
            **os.environ,
            "SHELFHOLD_DATA_DIR": str(settings.data_dir),
            "SHELFHOLD_STORE_DIR": str(settings.store_dir),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == -signal.SIGKILL, proc.stderr
    assert not KEY.search(proc.stdout)  # rien affiche
    con = sqlite3.connect(settings.database_path)
    rows = con.execute("SELECT prefix, revoked_at FROM api_key").fetchall()
    audit = con.execute("SELECT count(*) FROM audit WHERE action='api_key.rotated'").fetchone()
    con.close()
    assert rows == [(prefix, None)], rows
    assert audit == (0,)


@pytest.mark.req("EF-USR-003")
def test_q4_rotate_revoked_refused_expired_allowed_scope_kept(monkeypatch, capsys, settings):
    ctx = auth_env(monkeypatch, settings)
    ctx.__enter__()
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "admin", "keys", "create", "--name", "s") == 0
    p1 = KEY.search(capsys.readouterr().out).group(1)
    assert run_cli(monkeypatch, settings, "admin", "keys", "revoke", p1) == 0
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "admin", "keys", "rotate", p1) != 0
    out = capsys.readouterr()
    assert not KEY.search(out.out + out.err)
    con = sqlite3.connect(settings.database_path)
    assert con.execute("SELECT count(*) FROM api_key WHERE revoked_at IS NULL").fetchone() == (0,)
    con.close()


# --- Q1 : REPLAY ---------------------------------------------------------------------------
@pytest.mark.req("EF-USR-024")
def test_q1_forged_rmdir_intent_only_removes_an_empty_user_folder(tmp_path):
    from shelfhold.export.application.intents import IntentJournal
    from tests.intents_env import make_intent_env

    ienv = make_intent_env(tmp_path)
    journal = IntentJournal(ienv.engine, ienv.fs.fsops, clock=ienv.clock)
    empty = ienv.fs.manga / "Series" / "Vol 2 (placeholder)"
    empty.mkdir(parents=True)
    full = _touch(ienv.fs.manga / "Series" / "Vol 1" / "1.cbz")
    reserved = ienv.fs.library / ".shelfhold" / "recycle"
    reserved.mkdir(parents=True, exist_ok=True)
    i1 = journal.record(op="rmdir", src=str(empty))  # forgee : jamais demandee
    i2 = journal.record(op="rmdir", src=str(full.parent))
    i3 = journal.record(op="rmdir", src=str(reserved))
    for i in (i1, i2, i3):
        journal.mark_applying(i)
    journal.resolve_open()
    assert not empty.exists()  # supprime sans demande (dossier vide seulement)
    assert full.exists()  # contenu jamais touche
    assert reserved.exists()  # zone reservee refusee (Fsops.rmdir non-host)
