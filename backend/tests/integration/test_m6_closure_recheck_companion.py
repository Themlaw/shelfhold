"""Contre-relecture du lot B (#445) : ce que le confinement du compagnon laisse encore passer.

Non commité. Rouge = défaut (sauf les témoins `control_`, verts attendus).
"""

from __future__ import annotations

import ast
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from shelfhold.plugin_host.adapters.rpc.stdio import closed_environment
from shelfhold.plugin_host.runner import confine

needs_seccomp = pytest.mark.skipif(not confine.seccomp_ready(), reason="no seccomp")
needs_abi6 = pytest.mark.skipif(confine.landlock_abi() < 6, reason="needs Landlock ABI 6")
needs_landlock = pytest.mark.skipif(confine.landlock_abi() < 3, reason="needs Landlock ABI 3")

# Un plugin désigne un autre processus comme « propriétaire » d'un descripteur (F_SETOWN), choisit
# le signal livré (F_SETSIG), active O_ASYNC, puis provoque une E/S : c'est le noyau qui envoie le
# signal, aucun kill/tgkill/pidfd_send_signal n'est appelé. sigio_perm() ne compare que les uid.
SIGIO = """
import fcntl, os, sys, time
target, number = int(sys.argv[1]), int(sys.argv[2])
F_SETOWN, F_SETSIG = 8, 10
r, w = os.pipe()
fcntl.fcntl(r, F_SETOWN, target)
fcntl.fcntl(r, F_SETSIG, number)
fcntl.fcntl(r, fcntl.F_SETFL, fcntl.fcntl(r, fcntl.F_GETFL) | os.O_ASYNC)
os.write(w, b"x")
time.sleep(0.3)
try:
    os.kill(target, number)
    print("kill:ok")
except OSError as exc:
    print("kill:" + type(exc).__name__)
"""


def _sigio_scenario(bind) -> tuple[list[int], int | None, str]:  # type: ignore[no-untyped-def]
    got: list[int] = []
    previous = signal.signal(signal.SIGUSR1, lambda n, _f: got.append(n))
    sibling = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"])
    try:
        companion = subprocess.run(  # noqa: S603
            [sys.executable, "-I", "-c", SIGIO, str(os.getpid()), str(int(signal.SIGUSR1))],
            preexec_fn=bind, start_new_session=True, capture_output=True, text=True,
            timeout=60, check=False,
        )  # fmt: skip
        other = subprocess.run(  # noqa: S603
            [sys.executable, "-I", "-c", SIGIO, str(sibling.pid), str(int(signal.SIGKILL))],
            preexec_fn=bind, start_new_session=True, capture_output=True, text=True,
            timeout=60, check=False,
        )  # fmt: skip
        time.sleep(0.3)
        return got, sibling.poll(), companion.stdout + companion.stderr + other.stdout
    finally:
        signal.signal(signal.SIGUSR1, previous)
        if sibling.poll() is None:
            sibling.kill()
        sibling.wait()


@needs_seccomp
def test_fallback_seccomp_lets_a_plugin_signal_through_f_setown() -> None:
    """Repli (Landlock ABI 3 à 5, Ubuntu 24.04 par ex.) : le filtre refuse kill(), mais F_SETOWN +
    F_SETSIG + O_ASYNC fait envoyer SIGUSR1 au compagnon et SIGKILL à un autre plugin."""
    filt = confine.Filter(signals=True)
    got, sibling_code, out = _sigio_scenario(filt.install)
    assert "kill:PermissionError" in out, out  # le filtre refuse bien kill()...
    assert (got, sibling_code) == ([], None), (
        f"via SIGIO : compagnon a reçu {got}, autre plugin code {sibling_code} ({out})"
    )


@needs_abi6
@needs_seccomp
def test_control_abi6_scope_also_stops_f_setown() -> None:
    """Témoin : avec la portée SIGNAL de Landlock ABI 6, la même voie est fermée."""
    scratch = Path(tempfile.mkdtemp(prefix="plugin-"))
    rules = confine.Ruleset([scratch], confine.read_roots())
    filt = confine.Filter()

    def bind() -> None:
        rules.restrict()
        filt.install()

    try:
        got, sibling_code, out = _sigio_scenario(bind)
    finally:
        rules.close()
    assert got == [] and sibling_code is None, out


# -- régressions du repli seccomp : un processus fils du plugin -----------------------------

CHILDREN = """
import os, signal, subprocess, sys
out = {}
try:
    subprocess.run(["sleep", "5"], timeout=0.3)
    out["subprocess_timeout"] = "no timeout?"
except subprocess.TimeoutExpired:
    out["subprocess_timeout"] = "TimeoutExpired"
except OSError as exc:
    out["subprocess_timeout"] = type(exc).__name__
p = subprocess.Popen(["sleep", "5"])
try:
    p.terminate(); out["terminate"] = "ok"
except OSError as exc:
    out["terminate"] = type(exc).__name__
p.wait(timeout=10) if out["terminate"] == "ok" else None
r, w = os.pipe()
pid = os.fork()
if pid == 0:
    signal.signal(signal.SIGUSR2, lambda *_: None)
    try:
        signal.raise_signal(signal.SIGUSR2); os.write(w, b"ok")
    except OSError as exc:
        os.write(w, type(exc).__name__.encode())
    os._exit(0)
os.waitpid(pid, 0)
out["forked_raise"] = os.read(r, 100).decode()
print(out)
"""


@needs_seccomp
def test_fallback_seccomp_breaks_ordinary_child_handling() -> None:
    """Repli : un plugin ne peut plus arrêter un sous-processus qu'il a lancé (timeout de
    subprocess.run, Popen.terminate), et un processus forké ne peut plus se signaler lui-même :
    comportement accepté, documenté pour les auteurs."""
    filt = confine.Filter(signals=True)
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", CHILDREN], preexec_fn=filt.install,
        start_new_session=True, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    seen = ast.literal_eval(done.stdout.strip() or "{}") or {"stderr": done.stderr}
    # Décision R2 (docs/decisions/M6.md, docs/plugins/isolation.md) : BPF ne peut pas exprimer
    # « mes descendants », donc dans le repli un plugin ne signale ses sous-processus que par
    # killpg ; ces trois cas lèvent PermissionError, comportement accepté et documenté.
    assert seen.get("subprocess_timeout") == "PermissionError", seen
    assert seen.get("terminate") == "PermissionError", seen
    assert seen.get("forked_raise") == "PermissionError", seen


# -- régression de la liste de lecture : /etc/passwd fermé --------------------------------

HOME = """
import getpass, pathlib
out = {}
for name, action in (("home", pathlib.Path.home), ("getuser", getpass.getuser)):
    try:
        action(); out[name] = "ok"
    except Exception as exc:
        out[name] = type(exc).__name__
print(out)
"""


@needs_landlock
def test_a_bound_plugin_can_still_find_its_home() -> None:
    """Le plugin reçoit un environnement fermé (ni HOME ni USER) : Path.home() et getuser()
    passaient par /etc/passwd, que la nouvelle liste de lecture ne contient pas."""
    scratch = Path(tempfile.mkdtemp(prefix="plugin-"))
    rules = confine.Ruleset([scratch], confine.read_roots())
    try:
        done = subprocess.run(  # noqa: S603
            [sys.executable, "-I", "-c", HOME], preexec_fn=rules.restrict,
            env=closed_environment(), start_new_session=True, capture_output=True, text=True,
            timeout=60, check=False,
        )  # fmt: skip
    finally:
        rules.close()
    seen = ast.literal_eval(done.stdout.strip() or "{}") or {"stderr": done.stderr}
    assert seen == {"home": "ok", "getuser": "ok"}, seen


# -- témoins : ptrace entre plugins, et les lectures légitimes -----------------------------

PTRACE = """
import ctypes, os, sys
target = int(sys.argv[1])
libc = ctypes.CDLL(None, use_errno=True)
out = {"dumpable": libc.prctl(3, 0, 0, 0, 0)}
out["attach"] = "ok" if libc.ptrace(16, target, 0, 0) == 0 else os.strerror(ctypes.get_errno())
try:
    open(f"/proc/{target}/environ", "rb").read(1); out["environ"] = "ok"
except OSError as exc:
    out["environ"] = type(exc).__name__
LEGIT = []
import zoneinfo, datetime, locale, ssl
try:
    datetime.datetime.now(zoneinfo.ZoneInfo("Europe/Paris")); LEGIT.append("tz")
except Exception as exc:
    LEGIT.append("tz:" + type(exc).__name__)
try:
    locale.setlocale(locale.LC_ALL, "C.UTF-8"); LEGIT.append("locale")
except Exception as exc:
    LEGIT.append("locale:" + type(exc).__name__)
try:
    ssl.create_default_context(); LEGIT.append("ssl")
except Exception as exc:
    LEGIT.append("ssl:" + type(exc).__name__)
out["legit"] = LEGIT
print(out)
"""


@needs_landlock
def test_control_a_plugin_cannot_ptrace_another_and_reads_tz_locale_ssl() -> None:
    def bound() -> tuple[confine.Ruleset, object]:
        scratch = Path(tempfile.mkdtemp(prefix="plugin-"))
        return confine.Ruleset([scratch], confine.read_roots()), None

    rb, _ = bound()
    b = subprocess.Popen(  # noqa: S603
        [sys.executable, "-I", "-c", "import time; time.sleep(30)"],
        preexec_fn=rb.restrict, start_new_session=True,
    )  # fmt: skip
    ra, _ = bound()
    try:
        done = subprocess.run(  # noqa: S603
            [sys.executable, "-I", "-c", PTRACE, str(b.pid)], preexec_fn=ra.restrict,
            env=closed_environment(), start_new_session=True, capture_output=True, text=True,
            timeout=60, check=False,
        )  # fmt: skip
    finally:
        b.kill()
        b.wait()
        ra.close()
        rb.close()
    seen = ast.literal_eval(done.stdout.strip() or "{}") or {"stderr": done.stderr}
    assert seen["attach"] != "ok" and seen["environ"] != "ok", seen
    assert seen["legit"] == ["tz", "locale", "ssl"], seen
    print("plugin dumpable =", seen["dumpable"])
