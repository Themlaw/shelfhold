"""ENF-SEC-005: `shelfhold secrets export` writes an age v1 / scrypt file, never the bare key."""

import base64
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pyrage
import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.secrets import diagnostics
from shelfhold.infrastructure.secrets.errors import ExportError
from shelfhold.infrastructure.secrets.export import (
    ARMOR_BEGIN,
    ARMOR_END,
    decrypt_export,
    encrypt_export,
    read_passphrase_file,
)
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.interfaces import cli

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
WEAK = "".join(["not", "-a-", "real", "-pass"])
PASSPHRASE = " ".join(["correct", "horse", "battery", "staple"])


def dearmor(text: str) -> bytes:
    lines = text.strip().splitlines()
    assert lines[0] == ARMOR_BEGIN
    assert lines[-1] == ARMOR_END
    return base64.b64decode("".join(lines[1:-1]), validate=True)


@pytest.fixture
def cli_env(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.fixture
def passphrase_file(tmp_path: Path) -> Path:
    path = tmp_path / "passphrase"
    path.write_text(PASSPHRASE + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.mark.req("ENF-SEC-005")
def test_secrets_export_is_age_scrypt_same_as_backup() -> None:
    keyring = Keyring.generate(NOW).rotated(NOW)
    armored = encrypt_export(keyring, PASSPHRASE)
    assert armored.isascii()
    binary = dearmor(armored)
    assert binary.startswith(b"age-encryption.org/v1\n-> scrypt ")  # age v1, scrypt recipient
    for key in keyring.keys:  # the key never appears in the clear, in any encoding
        assert key.material not in binary
        assert base64.b64encode(key.material) not in binary
    # a stock age reader (pyrage) opens it, which is also what the backup of M1b stores
    assert decrypt_export(armored, PASSPHRASE) == keyring
    assert json.loads(pyrage.passphrase.decrypt(binary, PASSPHRASE))["format"] == 1
    with pytest.raises(ExportError):
        decrypt_export(armored, PASSPHRASE + "x")


@pytest.mark.req("ENF-SEC-005")
def test_export_contains_the_whole_keyring_retired_keys_included() -> None:
    first = Keyring.generate(NOW)
    rotated = first.rotated(NOW)
    restored = decrypt_export(encrypt_export(rotated, PASSPHRASE), PASSPHRASE)
    assert {k.kid for k in restored.keys} == {k.kid for k in rotated.keys}
    assert restored.get(first.active.kid).state == "retired"


@pytest.mark.req("ENF-SEC-005")
@pytest.mark.parametrize("content", [b"", b"\n", b"short\n", b"\xff\xfe"])
def test_unusable_passphrase_file_is_refused(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "pass"
    path.write_bytes(content)
    with pytest.raises(ExportError):
        read_passphrase_file(path)


@pytest.mark.req("ENF-SEC-005")
def test_passphrase_file_trailing_newline_is_not_part_of_the_passphrase(tmp_path: Path) -> None:
    path = tmp_path / "pass"
    path.write_bytes(b"a long enough passphrase\r\n")
    assert read_passphrase_file(path) == "a long enough passphrase"


@pytest.mark.req("ENF-SEC-005")
def test_cli_secrets_export_encrypted(
    cli_env: dict[str, str],
    settings: Settings,
    passphrase_file: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "export" / "keyring.age"
    out.parent.mkdir()
    code = cli.main(
        ["secrets", "export", "--passphrase-file", str(passphrase_file), "--out", str(out)]
    )
    assert code == 0
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    text = out.read_text(encoding="ascii")
    assert PASSPHRASE not in capsys.readouterr().out
    keyring = diagnostics.store_for(settings).load()
    assert decrypt_export(text, PASSPHRASE).keys == keyring.keys
    assert keyring.exported_at is not None  # remembered: `status` stops warning
    for key in keyring.keys:
        assert base64.b64encode(key.material).decode() not in text


@pytest.mark.req("ENF-SEC-005")
def test_cli_export_to_stdout_only_with_an_explicit_dash(
    cli_env: dict[str, str], passphrase_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):  # --out is mandatory: no implicit stdout
        cli.main(["secrets", "export", "--passphrase-file", str(passphrase_file)])
    capsys.readouterr()
    assert (
        cli.main(["secrets", "export", "--passphrase-file", str(passphrase_file), "--out", "-"])
        == 0
    )
    assert capsys.readouterr().out.startswith(ARMOR_BEGIN)


@pytest.mark.req("ENF-SEC-005")
@pytest.mark.parametrize("flag", ["--passphrase", "--pass", "--passphrase=" + WEAK])
def test_passphrase_is_never_accepted_on_the_command_line(
    cli_env: dict[str, str], tmp_path: Path, flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["secrets", "export", flag, "--out", str(tmp_path / "x")]
    if "=" not in flag:
        argv.insert(3, WEAK)
    with pytest.raises(SystemExit) as info:
        cli.main(argv)
    assert info.value.code == 2
    assert not (tmp_path / "x").exists()
    capsys.readouterr()


@pytest.mark.req("ENF-SEC-005")
def test_cli_export_with_a_missing_or_weak_passphrase_file_fails_without_output(
    cli_env: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "keyring.age"
    missing = tmp_path / "nope"
    assert (
        cli.main(["secrets", "export", "--passphrase-file", str(missing), "--out", str(out)]) == 1
    )
    weak = tmp_path / "weak"
    weak.write_text("abc\n", encoding="utf-8")
    assert cli.main(["secrets", "export", "--passphrase-file", str(weak), "--out", str(out)]) == 1
    assert not out.exists()
    assert "abc" not in capsys.readouterr().err


@pytest.mark.req("ENF-SEC-005")
def test_cli_export_replaces_atomically_and_leaves_no_temporary_file(
    cli_env: dict[str, str], passphrase_file: Path, tmp_path: Path
) -> None:
    out = tmp_path / "keyring.age"
    out.write_text("previous", encoding="utf-8")
    argv = ["secrets", "export", "--passphrase-file", str(passphrase_file), "--out", str(out)]
    assert cli.main(argv) == 0
    assert out.read_text(encoding="ascii").startswith(ARMOR_BEGIN)
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".")) == []
    assert os.listdir(tmp_path).count("keyring.age") == 1


@pytest.mark.req("ENF-SEC-005")
def test_cli_secrets_status_warns_key_inside_data(
    cli_env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["secrets", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "key_in_data" in {w["code"] for w in payload["warnings"]}
