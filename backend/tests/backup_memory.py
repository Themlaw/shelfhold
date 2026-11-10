"""Peak memory of the backup's three streams (M1b L23, ENF-PERF-001: 500 MB for the whole
instance): writing an archive, checking it, restoring it. Each runs in a child interpreter of its
own and reports `VmHWM` (the kernel's high-water mark of its resident memory); the floor is the
child that imports the code and does nothing else. age's scrypt, which seals the passphrase
layers, takes a few hundred MiB for a moment whatever the size of the set and is taken out of
the child (see `WORKER`). A design that held the database (or its copy, the tar or the cipher
text) in memory would show a peak that grows with the database."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from tests.backup_env import write_passphrase

MIB = 1024 * 1024

WORKER = r"""
import json, sys
from datetime import UTC, datetime
from pathlib import Path

def peak():
    for line in open("/proc/self/status"):
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) * 1024

phase, database, path, passphrase, target = sys.argv[1:6]
from shelfhold.infrastructure.secrets.export import read_passphrase_file
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.ops.backup import archive, verify
from shelfhold.ops.backup.restore import restore_archive

# age's scrypt takes a few hundred MiB for a moment, sized by a one-second calibration of the
# machine: it does not depend on the size of the set, so it is taken out here (the header key and
# the keyring member pass through unsealed) to measure what does: the streams.
archive.encrypt_blob = lambda clear, passphrase: clear
archive.decrypt_blob = lambda binary, passphrase: binary
verify.decrypt_export = lambda text, passphrase: Keyring.from_json(text.encode(), source="export")
secret = read_passphrase_file(Path(passphrase))
now = datetime(2026, 11, 8, 1, 40, tzinfo=UTC)
keyring = Keyring.generate(now).to_json()
if phase != "base":
    if phase == "write":
        sources = {"shelfhold.db": Path(database), "keyring.age": keyring}
        with open(path, "wb") as sink:
            archive.write_archive(
                sink, archive.iter_tar(sources, created_at=now, schema_revision="0099"), secret
            )
    elif phase == "verify":
        verify.check_file(Path(path), secret, Path(target).parent)
    else:
        restore_archive(Path(path), Path(target), Path(passphrase))
print(json.dumps({"peak": peak()}))
"""


def make_database(path: Path, mib: int) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
    connection.execute("INSERT INTO alembic_version VALUES ('0099')")
    connection.execute("CREATE TABLE blob (id INTEGER PRIMARY KEY, data BLOB)")
    for _ in range(mib):
        connection.execute("INSERT INTO blob (data) VALUES (?)", (os.urandom(MIB),))
    connection.commit()
    connection.close()


def run_phase(phase: str, database: Path, archive: Path, passphrase: Path, target: Path) -> int:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", WORKER, phase, str(database), str(archive), str(passphrase),
         str(target)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return int(json.loads(result.stdout.strip().splitlines()[-1])["peak"])


def measure(folder: Path, mib: int) -> dict[str, int]:
    """The peak resident memory, in bytes, of `base`, `write`, `verify` and `restore` for a
    database of `mib` MiB."""
    folder.mkdir(parents=True, exist_ok=True)
    database, archive = folder / "source.db", folder / "set.tar.age"
    passphrase = write_passphrase(folder / "passphrase")
    make_database(database, mib)
    target = folder / "restored"
    peaks = {"base": run_phase("base", database, archive, passphrase, target)}
    for phase in ("write", "verify", "restore"):
        peaks[phase] = run_phase(phase, database, archive, passphrase, target)
    return peaks
