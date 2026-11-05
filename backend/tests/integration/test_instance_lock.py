"""ENF-OPS-015: one instance per volumes. `flock` on /data/.lock, released by the kernel."""

import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.instance import InstanceLock, InstanceLockHeldError
from shelfhold.infrastructure.secrets.diagnostics import store_for
from shelfhold.infrastructure.secrets.errors import RotationInProgressError

HOLDER = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from shelfhold.infrastructure.instance import InstanceLock

    lock = InstanceLock(Path(sys.argv[1]))
    lock.acquire()
    print("held", flush=True)
    time.sleep(60)
    """
)


@pytest.fixture
def other_instance(tmp_path: Path) -> Iterator[subprocess.Popen[str]]:
    data = tmp_path / "data"
    data.mkdir()
    process = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", HOLDER, str(data)], stdout=subprocess.PIPE, text=True
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "held"
    try:
        yield process
    finally:
        process.kill()
        process.wait()


@pytest.mark.req("ENF-OPS-015")
def test_second_instance_on_same_volumes_refuses_to_start(
    tmp_path: Path, other_instance: subprocess.Popen[str]
) -> None:
    lock = InstanceLock(tmp_path / "data")
    with pytest.raises(InstanceLockHeldError) as raised:
        lock.acquire()
    assert raised.value.code == "instance.lease_conflict"
    assert not lock.is_held()
    assert (tmp_path / "data" / ".lock").exists()  # the file the spec names


@pytest.mark.req("ENF-OPS-015")
def test_lock_is_released_by_the_kernel_when_the_holder_is_killed(
    tmp_path: Path, other_instance: subprocess.Popen[str]
) -> None:
    other_instance.kill()
    other_instance.wait()
    lock = InstanceLock(tmp_path / "data")
    lock.acquire()
    assert lock.is_held()
    lock.release()
    assert not lock.is_held()


@pytest.mark.req("ENF-OPS-015", "ENF-OPS-007")
def test_is_held_is_what_boot_jobs_asks(tmp_path: Path) -> None:
    from shelfhold.jobs.huey_app import InstanceLock as Wanted

    data = tmp_path / "data"
    data.mkdir()
    lock = InstanceLock(data)
    wanted: Wanted = lock  # structural: `is_held() -> bool`
    assert wanted.is_held() is False  # before acquire(), boot_jobs refuses
    with lock:
        assert wanted.is_held() is True
    assert wanted.is_held() is False


@pytest.mark.req("ENF-OPS-015", "ENF-SEC-005")
def test_keyring_lock_is_the_instance_lock(
    settings: Settings, tmp_path: Path, other_instance: subprocess.Popen[str]
) -> None:
    """PLAN-M1a section 7: `keyring.lock` is taken over by the instance lock. While another
    process runs the instance, `secrets rotate` (which takes the keyring lock) is refused."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    holder_data = tmp_path / "data"
    assert settings.data_dir == holder_data
    store = store_for(settings)
    assert store.lock_path == holder_data / ".lock"
    with pytest.raises(RotationInProgressError), store.lock():
        pytest.fail("rotated while another instance holds the volumes")
