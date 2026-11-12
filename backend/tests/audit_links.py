"""A watch on `os.link` and `os.symlink` for the integration tests (EF-EXP-015): whatever
the source does, a hard link made from `src/shelfhold` must come from one of the known roads, and
the option's own (`ConfinedRoot.link_fd`) only under `writer.link_replaceable`. A symbolic link
is never made by the source. Code of the tests themselves (setting up a world) is not judged."""

import os
import traceback
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src" / "shelfhold")
ROADS = {
    ("infrastructure/fsops/confined.py", "_rename_noreplace"),
    ("infrastructure/fsops/confined.py", "link"),  # the lease and the no-clobber put
    ("infrastructure/fsops/confined.py", "link_to"),  # the probe
    ("infrastructure/secrets/atomic.py", "write_private"),
    ("ops/backup/store.py", "write"),
}
OPTION = ("infrastructure/fsops/confined.py", "link_fd")
OPTION_CALLER = ("infrastructure/fsops/writer.py", "link_replaceable")
violations: list[str] = []
_installed = False


def _frames() -> list[tuple[str, str]]:
    """The source frames of the stack; empty when the caller of `os.link` is test code (a world
    being set up, or an attack played from a patched method)."""
    if not traceback.extract_stack()[-4].filename.startswith(SRC):
        return []
    return [
        (Path(f.filename).as_posix().removeprefix(Path(SRC).as_posix() + "/"), f.name)
        for f in traceback.extract_stack()
        if f.filename.startswith(SRC)
    ]


def _judge(event: str) -> None:
    frames = _frames()
    if not frames:  # a test setting up its world
        return
    seen = set(frames)
    ok = event == "os.link" and (bool(seen & ROADS) or (OPTION in seen and OPTION_CALLER in seen))
    if not ok:
        violations.append(f"{event} from {frames[-3:]}")
        raise PermissionError(f"{event} outside the roads of the link guard: {frames[-3:]}")


def install() -> None:
    """Wrap `os.link` and `os.symlink` (a `sys.addaudithook` stays for the life of the process and
    slows every event of every test; the wrapper costs only when a link is made). Once only."""
    global _installed
    if _installed:
        return
    _installed = True
    real_link, real_symlink = os.link, os.symlink

    def link(*args, **kwargs):  # type: ignore[no-untyped-def]
        _judge("os.link")
        return real_link(*args, **kwargs)

    def symlink(*args, **kwargs):  # type: ignore[no-untyped-def]
        _judge("os.symlink")
        return real_symlink(*args, **kwargs)

    os.link, os.symlink = link, symlink  # type: ignore[assignment]
