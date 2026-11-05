"""ENF-OPS-010: `/data` on NFS or CIFS is refused (statfs), unless the admin insists."""

from pathlib import Path

import pytest

from shelfhold.infrastructure.observability.network_fs import (
    CIFS_MAGIC,
    NFS_MAGIC,
    SMB2_MAGIC,
    NetworkDataDirError,
    filesystem_kind,
    refuse_network_data_dir,
)


@pytest.mark.req("ENF-OPS-010")
def test_data_on_nfs_refused_unless_overridden(tmp_path: Path) -> None:
    on_nfs = lambda _path: NFS_MAGIC  # noqa: E731
    with pytest.raises(NetworkDataDirError) as raised:
        refuse_network_data_dir(tmp_path, allow=False, statfs=on_nfs)
    assert "nfs" in str(raised.value)
    assert refused_kinds() == {"nfs", "cifs", "smb2", "smb", "9p", "ceph", "afs", "fuse.sshfs"}
    for magic in (CIFS_MAGIC, SMB2_MAGIC):
        with pytest.raises(NetworkDataDirError):
            refuse_network_data_dir(tmp_path, allow=False, statfs=lambda _p, m=magic: m)
    assert refuse_network_data_dir(tmp_path, allow=True, statfs=on_nfs) == "nfs"  # the option
    local = lambda _path: 0xEF53  # noqa: E731  (ext4)
    assert refuse_network_data_dir(tmp_path, allow=False, statfs=local) is None


@pytest.mark.req("ENF-OPS-010")
def test_the_real_statfs_answers_for_a_local_folder(tmp_path: Path) -> None:
    kind = filesystem_kind(tmp_path)
    assert kind not in {
        "nfs",
        "cifs",
        "smb2",
        "smb",
        "9p",
        "ceph",
        "afs",
        "fuse.sshfs",
    }  # CI runs on a local disk or a tmpfs


@pytest.mark.req("ENF-OPS-010")
def test_start_up_refuses_a_network_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shelfhold.config import Settings
    from shelfhold.infrastructure import volumes
    from shelfhold.infrastructure.observability import network_fs

    monkeypatch.setattr(network_fs, "filesystem_magic", lambda _path: NFS_MAGIC)
    settings = Settings(data_dir=tmp_path / "data", store_dir=tmp_path / "store")
    with pytest.raises(NetworkDataDirError):
        volumes.ensure_volumes(settings)
    allowed = Settings(
        data_dir=tmp_path / "data", store_dir=tmp_path / "store", allow_network_data_dir=True
    )
    volumes.ensure_volumes(allowed)  # the option passes, with a warning


def refused_kinds() -> set[str]:
    from shelfhold.infrastructure.observability.network_fs import REFUSED_KINDS

    return set(REFUSED_KINDS)


@pytest.mark.req("ENF-OPS-010")
def test_other_network_file_systems_are_refused_and_fuse_is_read_from_mountinfo(
    tmp_path: Path,
) -> None:
    from shelfhold.infrastructure.observability.network_fs import (
        AFS_MAGIC,
        CEPH_MAGIC,
        FUSE_MAGIC,
        NINEP_MAGIC,
    )

    for magic in (NINEP_MAGIC, CEPH_MAGIC, AFS_MAGIC):
        with pytest.raises(NetworkDataDirError):
            refuse_network_data_dir(tmp_path, allow=False, statfs=lambda _p, m=magic: m)
    info = tmp_path / "mountinfo"
    info.write_text(
        f"20 1 0:5 / / rw - ext4 /dev/sda1 rw\n"
        f"41 20 0:40 / {tmp_path} rw - fuse.sshfs me@host:/dir rw\n",
        encoding="utf-8",
    )
    assert filesystem_kind(tmp_path, statfs=lambda _p: FUSE_MAGIC, mountinfo=info) == "fuse.sshfs"
    other = tmp_path / "other"
    other.write_text("20 1 0:5 / / rw - fuse.rclone remote rw\n", encoding="utf-8")
    assert filesystem_kind(tmp_path, statfs=lambda _p: FUSE_MAGIC, mountinfo=other) == "fuse.rclone"


@pytest.mark.req("ENF-OPS-010")
def test_a_failed_statfs_warns_and_never_refuses(tmp_path: Path) -> None:
    assert refuse_network_data_dir(tmp_path, allow=False, statfs=lambda _p: 0) is None


@pytest.mark.req("ENF-OPS-010")
def test_the_override_is_documented_and_shown_by_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shelfhold.config import ResolvedSettings, Settings
    from shelfhold.infrastructure.observability import network_fs
    from shelfhold.interfaces.cli import status

    docs = Path(__file__).resolve().parents[3] / "docs" / "configuration.md"
    assert "SHELFHOLD_ALLOW_NETWORK_DATA_DIR" in docs.read_text(encoding="utf-8")
    monkeypatch.setattr(network_fs, "filesystem_magic", lambda _p: NFS_MAGIC)
    settings = Settings(
        data_dir=tmp_path / "d", store_dir=tmp_path / "s", allow_network_data_dir=True
    )
    found = status._environment_warnings(ResolvedSettings(settings, {}, "1.0"))
    assert "data_dir_on_network_filesystem" in {w["code"] for w in found}
