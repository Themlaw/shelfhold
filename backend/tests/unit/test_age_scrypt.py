"""ENF-SEC-005, EF-INT-015 (M1b L23): the age v1 scrypt envelope written and read by Shelfhold.

Interoperability in both directions: what `seal` writes is opened by `pyrage` (and by the `age`
1.2.1 command when it is installed, which CI does not require), what `pyrage` writes is opened by
`unseal`, and the scrypt vectors of C2SP/CCTV (the official age test suite, copied in
`tests/unit/vectors/age/`) give the verdict the reference implementation gives.
"""

from __future__ import annotations

import hashlib
import os
import pty
import shutil
import sys
from pathlib import Path

import pyrage
import pytest

from shelfhold.infrastructure.secrets import age_scrypt
from shelfhold.infrastructure.secrets.errors import ExportError, WorkFactorError

PASSPHRASE = "synthetic passphrase for the age tests"  # noqa: S105
VECTORS = Path(__file__).parent / "vectors" / "age"
SIZES = [0, 1, 100, age_scrypt.CHUNK - 1, age_scrypt.CHUNK, age_scrypt.CHUNK + 1, 3 * 65536 + 7]


def clear_of(size: int) -> bytes:
    return (bytes(range(256)) * (size // 256 + 1))[:size]


@pytest.mark.req("ENF-SEC-005")
@pytest.mark.parametrize("size", SIZES)
def test_what_we_write_is_read_by_pyrage_and_by_us(size: int) -> None:
    sealed = age_scrypt.seal(clear_of(size), PASSPHRASE)
    assert bytes(pyrage.passphrase.decrypt(sealed, PASSPHRASE)) == clear_of(size)
    assert age_scrypt.unseal(sealed, PASSPHRASE) == clear_of(size)


@pytest.mark.req("ENF-SEC-005")
@pytest.mark.parametrize("size", SIZES)
def test_what_pyrage_writes_is_read_by_us(size: int) -> None:
    """Also the exports and backups already written: their cost was calibrated by age."""
    sealed = bytes(pyrage.passphrase.encrypt(clear_of(size), PASSPHRASE))
    assert age_scrypt.unseal(sealed, PASSPHRASE) == clear_of(size)


@pytest.mark.req("ENF-SEC-005")
def test_the_cost_is_fixed_and_the_file_is_the_standard_shape() -> None:
    sealed = age_scrypt.seal(b"x", PASSPHRASE)
    lines = sealed.split(b"\n", 3)
    assert lines[0] == b"age-encryption.org/v1"
    assert lines[1].startswith(b"-> scrypt ")
    assert lines[1].endswith(b" 18")
    assert age_scrypt.LOG_N == 18 and 16 <= age_scrypt.LOG_N <= 18
    assert lines[2].count(b" ") == 0 and len(lines[2]) == 43  # one 32-byte line, unpadded
    assert age_scrypt.seal(b"x", PASSPHRASE) != sealed  # a new salt, file key and nonce each time


# -- the official vectors ------------------------------------------------------------


def _vector(path: Path) -> tuple[dict[str, str], bytes]:
    raw = path.read_bytes()
    head, _blank, body = raw.partition(b"\n\n")
    fields: dict[str, str] = {}
    for line in head.decode().splitlines():
        key, _sep, value = line.partition(": ")
        fields.setdefault(key, value)
    return fields, body


@pytest.mark.req("ENF-SEC-005")
@pytest.mark.parametrize("path", sorted(VECTORS.glob("scrypt*")), ids=lambda p: p.name)
def test_the_official_scrypt_vectors_get_the_reference_verdict(path: Path) -> None:
    fields, binary = _vector(path)
    if fields["expect"] == "success":
        clear = age_scrypt.unseal(binary, fields["passphrase"])
        assert hashlib.sha256(clear).hexdigest() == fields["payload"]
        return
    with pytest.raises(ExportError) as refused:
        age_scrypt.unseal(binary, fields["passphrase"])
    if path.name == "scrypt_work_factor_23":  # too costly to open: said so, not "wrong passphrase"
        assert isinstance(refused.value, WorkFactorError)
    else:
        assert not isinstance(refused.value, WorkFactorError)


# -- damage -------------------------------------------------------------------------------------


@pytest.mark.req("ENF-SEC-005")
def test_any_altered_byte_or_a_wrong_passphrase_is_refused() -> None:
    sealed = age_scrypt.seal(clear_of(100), PASSPHRASE)
    with pytest.raises(ExportError, match="wrong passphrase or damaged file"):
        age_scrypt.unseal(sealed, PASSPHRASE + "x")
    for where in (30, 100, 160, len(sealed) - 40, len(sealed) - 1):  # header, MAC, nonce, body, tag
        altered = bytearray(sealed)
        altered[where] ^= 1
        with pytest.raises(ExportError):
            age_scrypt.unseal(bytes(altered), PASSPHRASE)
    for cut in (len(sealed) - 1, len(sealed) - 20, 100, 0):
        with pytest.raises(ExportError):
            age_scrypt.unseal(sealed[:cut], PASSPHRASE)
    with pytest.raises(ExportError):
        age_scrypt.unseal(sealed + b"x", PASSPHRASE)  # an extra byte is a longer last chunk: no


@pytest.mark.req("ENF-SEC-005")
def test_a_chunk_stream_that_is_cut_or_extended_at_a_boundary_is_refused() -> None:
    sealed = age_scrypt.seal(clear_of(2 * age_scrypt.CHUNK), PASSPHRASE)
    size = age_scrypt.CHUNK + age_scrypt.TAG
    with pytest.raises(ExportError):  # the first chunk as if it were the last: its flag is 0
        age_scrypt.unseal(sealed[: len(sealed) - size], PASSPHRASE)


# -- the `age` command (outside CI) ----------------------------------------


def _age_with_passphrase(workdir: Path, args: list[str], stdin: bytes, passphrase: str) -> bytes:
    """Run `age` with the passphrase typed on a terminal, as the command insists."""
    source, target = workdir / "in", workdir / "out"
    source.write_bytes(stdin)
    command = f"{shutil.which('age')} {' '.join(args)} < {source} > {target}"
    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - the child process
        os.execv("/bin/sh", ["sh", "-c", command])  # noqa: S606
    for _prompt in range(2 if "-p" in args else 1):  # encrypting asks to confirm
        seen = b""
        while b"passphrase" not in seen.lower():
            seen += os.read(fd, 1024)
        os.write(fd, passphrase.encode() + b"\n")
    try:
        while os.read(fd, 1024):
            pass
    except OSError:
        pass
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    return target.read_bytes()


@pytest.mark.req("ENF-SEC-005")
@pytest.mark.skipif(
    shutil.which("age") is None or sys.platform != "linux",
    reason="the age command is not installed",
)
def test_the_age_command_opens_our_file_and_we_open_its_file(tmp_path: Path) -> None:
    sealed = age_scrypt.seal(clear_of(70000), PASSPHRASE)
    assert _age_with_passphrase(tmp_path, ["-d"], sealed, PASSPHRASE) == clear_of(70000)
    made = _age_with_passphrase(tmp_path, ["-p"], clear_of(70000), PASSPHRASE)
    assert age_scrypt.unseal(made, PASSPHRASE) == clear_of(70000)
