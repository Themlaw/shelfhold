"""EF-SRC-002: a plugin does no direct network or disk I/O; the audit-hook detector refuses it.

The detector is `sys.addaudithook`: it is not a sandbox (a plugin can step around it, see
docs/admin/plugins-tiers.md), but while plugin code runs, every socket, process, thread and file
outside the plugin's sandbox is refused before it happens (fail closed) and reported with the
plugin's id. The Fetcher the host hands in is host code and keeps working.
"""

import asyncio
import contextlib
import os
import socket
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
import structlog

from shelfhold.plugin_host import audit_hook
from shelfhold.plugin_host.audit_hook import audited
from shelfhold.plugin_host.registry import PluginCandidate, PluginOrigin, PluginRegistry
from shelfhold.sdk import io_guard
from shelfhold.sdk.io_guard import ForbiddenIOError, GuardedPlugin, host_io, plugin_io
from shelfhold.sdk.testing.fakes import FakeRangedConnectedService, fake_ranged_fetcher
from shelfhold.sdk.testing.kit import assert_conforms, run_source_conformance

pytestmark = pytest.mark.req("EF-SRC-002")

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def listener() -> Iterator[socket.socket]:
    """A loopback listener: whatever a plugin sends would arrive here."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.setblocking(False)
    yield server
    server.close()


def _nothing_arrived(server: socket.socket) -> bool:
    try:
        conn, _ = server.accept()
    except BlockingIOError:
        return True
    conn.close()
    return False


def _run_kit(service: Any) -> Any:
    return asyncio.run(
        run_source_conformance(service, fake_ranged_fetcher(), reinstall=lambda: type(service)())
    )


def test_plugin_has_no_direct_io_audit_hook_detects(listener: socket.socket) -> None:
    """Shelfhold form of `test_plugin_has_no_direct_io_audit_hook_detects`."""
    port = listener.getsockname()[1]

    class Leaky(FakeRangedConnectedService):
        async def search(self, f: Any, q: str, filters: Any, cursor: Any, **kw: Any) -> Any:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect(("127.0.0.1", port))
            s.sendall(b"stolen")
            return await super().search(f, q, filters, cursor, **kw)

    report = _run_kit(Leaky())
    assert not report.ok
    (check,) = [c for c in report.checks if c.name == "no_direct_io"]
    assert not check.ok and "ForbiddenIOError" in check.detail and "socket" in check.detail
    with pytest.raises(ForbiddenIOError):
        assert_conforms(Leaky(), fake_ranged_fetcher(), reinstall=Leaky)
    assert _nothing_arrived(listener)


def test_conforming_plugin_passes_the_io_check() -> None:
    report = _run_kit(FakeRangedConnectedService())
    assert report.ok, report.failures
    assert any(c.name == "no_direct_io" and c.ok for c in report.checks)


def test_swallowed_refusal_is_still_reported(listener: socket.socket) -> None:
    port = listener.getsockname()[1]

    class Quiet(FakeRangedConnectedService):
        async def search(self, f: Any, q: str, filters: Any, cursor: Any, **kw: Any) -> Any:
            with contextlib.suppress(Exception):  # the plugin hides the refusal
                socket.create_connection(("127.0.0.1", port)).sendall(b"x")
            return await super().search(f, q, filters, cursor, **kw)

    report = _run_kit(Quiet())
    assert any(c.name == "no_direct_io" and not c.ok for c in report.checks)
    assert _nothing_arrived(listener)


def test_file_outside_the_sandbox_refused_inside_allowed(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("host data", encoding="utf-8")
    seen: list[io_guard.IoViolation] = []
    with plugin_io("p-files", sandbox=sandbox, report=seen.append, mode="raise"):
        (sandbox / "own.txt").write_text("mine", encoding="utf-8")
        assert (sandbox / "own.txt").read_text(encoding="utf-8") == "mine"
        with pytest.raises(ForbiddenIOError):
            outside.read_text(encoding="utf-8")
        with pytest.raises(ForbiddenIOError):
            (tmp_path / "new.txt").write_text("leak", encoding="utf-8")
        with pytest.raises(ForbiddenIOError):
            os.open(tmp_path / "flags.txt", os.O_WRONLY | os.O_CREAT)
        with pytest.raises(ForbiddenIOError):
            os.listdir(tmp_path)
        with pytest.raises(ForbiddenIOError):
            os.remove(outside)
    assert not (tmp_path / "new.txt").exists() and not (tmp_path / "flags.txt").exists()
    assert outside.read_text(encoding="utf-8") == "host data"
    assert {v.plugin_id for v in seen} == {"p-files"} and len(seen) == 5


def test_symlink_out_of_the_sandbox_refused(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    secret = tmp_path / "keyring"
    secret.write_text("k", encoding="utf-8")
    (sandbox / "link").symlink_to(secret)
    with (
        plugin_io("p-link", sandbox=sandbox, report=lambda v: None, mode="raise"),
        pytest.raises(ForbiddenIOError),
    ):
        (sandbox / "link").read_text(encoding="utf-8")
    with (
        plugin_io("p-dots", sandbox=sandbox, report=lambda v: None, mode="raise"),
        pytest.raises(ForbiddenIOError),
    ):
        (sandbox / ".." / "keyring").read_text(encoding="utf-8")


def test_no_sandbox_means_no_file_at_all(tmp_path: Path) -> None:
    target = tmp_path / "x.txt"
    target.write_text("x", encoding="utf-8")
    with plugin_io("p-none", report=lambda v: None, mode="raise"), pytest.raises(ForbiddenIOError):
        target.read_bytes()


def test_subprocess_thread_and_ctypes_refused(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    seen: list[io_guard.IoViolation] = []
    with plugin_io("p-proc", report=seen.append, mode="raise"):
        with pytest.raises(ForbiddenIOError):
            subprocess.run(["touch", str(marker)], check=False)  # noqa: S603, S607
        with pytest.raises(ForbiddenIOError):
            os.system(f"touch {marker}")  # noqa: S605
        with pytest.raises(ForbiddenIOError):
            threading.Thread(target=lambda: None).start()
        with pytest.raises(ForbiddenIOError):
            socket.getaddrinfo("exfiltrate.example.org", 80)
        with pytest.raises(ForbiddenIOError):
            import ctypes

            ctypes.CDLL(None)
    assert not marker.exists()
    events = {v.event for v in seen}
    assert {"subprocess.Popen", "os.system", "socket.getaddrinfo"} <= events
    assert any(e.startswith("_thread.") for e in events)
    assert any(e.startswith("ctypes.") for e in events)


def test_host_io_inside_a_plugin_call_is_not_flagged(tmp_path: Path) -> None:
    """The Fetcher is host code: what it does on the plugin's behalf is not the plugin's I/O."""
    target = tmp_path / "host.txt"

    class HostFetcher:
        async def get(self, url: str, **kw: Any) -> str:
            target.write_text("written by the host", encoding="utf-8")
            return "ok"

        async def post(self, url: str, **kw: Any) -> str:
            return "ok"

        async def download(self, req: Any) -> str:
            return "ok"

        def cookies(self) -> dict[str, str]:
            return {}

    class Polite:
        manifest = None

        async def search(self, f: Any, q: str) -> Any:
            return await f.get("https://example.org/")

    seen: list[io_guard.IoViolation] = []
    plugin = GuardedPlugin(Polite(), "p-polite", report=seen.append)
    assert asyncio.run(plugin.search(HostFetcher(), "q")) == "ok"
    assert target.read_text(encoding="utf-8") == "written by the host" and seen == []
    with plugin_io("p-direct", report=seen.append), host_io():
        target.write_text("host again", encoding="utf-8")
    assert seen == []


def test_scope_ends_with_the_call(tmp_path: Path) -> None:
    class Plain:
        def name(self) -> str:
            return "plain"

    assert GuardedPlugin(Plain(), "p-plain").name() == "plain"
    (tmp_path / "after.txt").write_text("host", encoding="utf-8")  # back in host code


def test_hook_fails_closed_on_an_internal_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def broken(*args: Any) -> Any:
        raise RuntimeError("classifier bug")

    monkeypatch.setattr(io_guard, "_verdict", broken)
    seen: list[io_guard.IoViolation] = []
    with (
        plugin_io("p-bug", sandbox=tmp_path, report=seen.append, mode="raise"),
        pytest.raises(ForbiddenIOError),
    ):
        (tmp_path / "ok.txt").write_text("x", encoding="utf-8")
    assert not (tmp_path / "ok.txt").exists()
    assert seen and seen[0].plugin_id == "p-bug"


def test_production_reports_without_blocking(tmp_path: Path) -> None:
    """The host runs the detector in `report` mode: logged with the plugin id, never blocked
    (the spec of L05: it is a detector, the trust boundary is the M6 sandbox)."""
    target = tmp_path / "written-by-plugin.txt"

    class Chatty:
        manifest = None

        async def search(self, f: Any, q: str) -> Any:
            target.write_text("plugin", encoding="utf-8")
            return "done"

    with structlog.testing.capture_logs() as logs:
        assert asyncio.run(audited(Chatty(), "third-chatty").search(None, "q")) == "done"
    assert target.read_text(encoding="utf-8") == "plugin"  # not blocked
    (entry,) = [e for e in logs if e["event"] == "plugin.forbidden_io"]
    assert entry["plugin_id"] == "third-chatty" and entry["io_event"] == "open"
    assert entry["log_level"] == "warning"


def test_kit_raises_where_production_reports(tmp_path: Path) -> None:
    target = tmp_path / "x.txt"
    with plugin_io("p-kit", mode="raise"), pytest.raises(ForbiddenIOError):
        target.write_text("x", encoding="utf-8")
    assert not target.exists()
    with plugin_io("p-prod") as seen:  # the default is report
        target.write_text("x", encoding="utf-8")
    assert target.exists() and [v.plugin_id for v in seen] == ["p-prod"]
    with pytest.raises(ValueError, match="mode"), plugin_io("p-bad", mode="block"):  # type: ignore[arg-type]
        pass


def test_registry_hands_out_audited_plugins(tmp_path: Path) -> None:
    raw = FakeRangedConnectedService().manifest.model_dump(mode="json")
    at_import = tmp_path / "import.txt"
    at_call = tmp_path / "call.txt"

    class Plugin:
        manifest: ClassVar[dict[str, Any]] = {**raw, "key": "audited-one"}

        def __init__(self) -> None:
            at_import.write_text("import", encoding="utf-8")

        async def search(self, f: Any, q: str) -> Any:
            at_call.write_text("call", encoding="utf-8")

    registry = PluginRegistry()
    with structlog.testing.capture_logs() as logs:
        (record,) = registry.load_all(
            [PluginCandidate(key="audited-one", origin=PluginOrigin.FIRST_PARTY, load=Plugin)]
        )
    assert record.status.value == "loaded" and at_import.exists()
    assert [e["plugin_id"] for e in logs if e["event"] == "plugin.forbidden_io"] == ["audited-one"]
    assert isinstance(record.instance, GuardedPlugin)
    with structlog.testing.capture_logs() as logs:
        asyncio.run(record.instance.search(None, "q"))
    assert at_call.exists()
    assert [e["plugin_id"] for e in logs if e["event"] == "plugin.forbidden_io"] == ["audited-one"]


def test_hook_is_installed_once() -> None:
    audit_hook.install()
    audit_hook.install()
    assert io_guard.installed()


def test_admin_doc_says_the_detector_is_not_a_sandbox() -> None:
    text = (ROOT / "docs" / "admin" / "plugins-tiers.md").read_text(encoding="utf-8")
    assert "sys.addaudithook" in text
    assert "détecteur, pas une isolation" in text


@pytest.mark.parametrize("mode", ["report", "raise"])
def test_real_socket_process_ctypes_and_file_in_both_modes(
    mode: io_guard.Mode, listener: socket.socket, tmp_path: Path
) -> None:
    """Real calls: in `raise` (the kit) each one is refused before it happens; in `report` (the
    host) each one happens and is reported with the plugin id."""
    port = listener.getsockname()[1]
    target = tmp_path / "file.txt"
    marker = tmp_path / "ran"
    refused = pytest.raises(ForbiddenIOError) if mode == "raise" else contextlib.nullcontext()
    with plugin_io("p-both", mode=mode) as seen:
        with refused:
            target.write_text("x", encoding="utf-8")
        with refused:
            conn = socket.create_connection(("127.0.0.1", port))
            conn.sendall(b"data")
            conn.close()
        with refused:
            subprocess.run(["touch", str(marker)], check=True)  # noqa: S603, S607
        with refused:
            import ctypes

            ctypes.CDLL(None)
    events = [v.event for v in seen]
    assert all(v.plugin_id == "p-both" for v in seen)
    assert "open" in events and "subprocess.Popen" in events
    assert any(e.startswith("socket.") for e in events)
    assert any(e.startswith("ctypes.") for e in events)
    if mode == "raise":
        assert not target.exists() and not marker.exists() and _nothing_arrived(listener)
    else:
        assert target.exists() and marker.exists() and not _nothing_arrived(listener)


def test_file_descriptor_and_bytecode_cache(tmp_path: Path) -> None:
    sandbox = tmp_path / "sandbox"
    cache = tmp_path / "__pycache__"
    sandbox.mkdir()
    cache.mkdir()
    with plugin_io("p-fd", sandbox=sandbox, mode="raise") as seen:
        fd = os.open(sandbox / "own", os.O_WRONLY | os.O_CREAT)
        with pytest.raises(ForbiddenIOError):
            open(fd, "wb")  # noqa: SIM115 - an unnamed file is never judged by its path
        os.close(fd)
        with pytest.raises(ForbiddenIOError):
            (cache / "m.cpython-313.pyc").write_bytes(b"")  # refused, not a finding
    assert [v.detail for v in seen] == ["a file descriptor or an unnamed file"]
    assert not (cache / "m.cpython-313.pyc").exists()


def test_extra_code_root_is_readable(tmp_path: Path) -> None:
    root = tmp_path / "site"
    root.mkdir()
    (root / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (root / "data.txt").write_text("no", encoding="utf-8")
    audit_hook.allow_code_root(root)
    with plugin_io("p-root", mode="raise") as seen:
        assert (root / "mod.py").read_text(encoding="utf-8") == "x = 1\n"
        with pytest.raises(ForbiddenIOError):
            (root / "data.txt").read_text(encoding="utf-8")  # code only, not data
    assert len(seen) == 1


def test_attributes_set_on_the_proxy_reach_the_plugin() -> None:
    class Plain:
        value = 0

    inner = Plain()
    proxy = GuardedPlugin(inner, "p-attr")
    proxy.value = 3
    assert inner.value == 3 and io_guard.unwrap(proxy) is inner
