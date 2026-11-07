"""EF-SRC-019 / ENF-SEC-009 (M1b L07): the third-party plugin guard, through the real CLI."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from shelfhold.plugin_host import install as guard
from tests.plugin_guard_env import CANARY, PHRASE, Cli, build_wheel, make_cli, plugin_wheel

pytestmark = pytest.mark.req("ENF-SEC-009")

ENABLE = {"SHELFHOLD_ALLOW_UNSANDBOXED_PLUGINS": "1"}


@pytest.fixture
def cli(tmp_path: Path) -> Cli:
    return make_cli(tmp_path)


@pytest.fixture
def probe(tmp_path: Path) -> Path:
    return tmp_path / "probe.jsonl"


def snapshots(probe: Path) -> list[dict[str, object]]:
    if not probe.exists():
        return []
    return [json.loads(line) for line in probe.read_text(encoding="utf-8").splitlines()]


def install(
    cli: Cli, tmp_path: Path, probe: Path, dist: str = "demo-plug", key: str = "demo"
) -> Path:
    wheel = plugin_wheel(tmp_path, dist, key, probe, dist.replace("-", "_"))
    done = cli.run("plugin", "install", str(wheel.path), "--sha256", wheel.sha256)
    assert done.returncode == 0, done.stderr
    return wheel.path


def enable(cli: Cli, name: str = "demo-plug") -> None:
    done = cli.run("plugin", "enable", name, typed=PHRASE + "\n", **ENABLE)
    assert done.returncode == 0, done.stderr


@pytest.mark.req("EF-SRC-019")
def test_plugin_install_only_binary_require_hashes_no_deps(
    cli: Cli, tmp_path: Path, probe: Path
) -> None:
    # the command line that runs: binary only, hashes required, no resolution, no index
    argv = guard.install_command(["pip", "install"], tmp_path / "t", tmp_path / "r.txt")
    for flag in ("--only-binary", ":all:", "--require-hashes", "--no-deps", "--no-index"):
        assert flag in argv
    # a real offline install from a local wheel with its hash; nothing of the plugin runs
    install(cli, tmp_path, probe)
    site = cli.data / "plugins" / "site-packages"
    assert (site / "demo_plug" / "__init__.py").exists()
    assert snapshots(probe) == []
    # a wrong hash and a missing hash are refused, nothing is installed
    other = plugin_wheel(tmp_path, "other-plug", "other", probe, "other_plug")
    bad = cli.run("plugin", "install", str(other.path), "--sha256", "0" * 64)
    assert bad.returncode == 1 and "hash_mismatch" in bad.stderr
    assert cli.run("plugin", "install", str(other.path)).returncode == 2  # --sha256 is required
    assert not (site / "other_plug").exists()
    assert ("plugin.install_refused", "other_plug-1.0-py3-none-any.whl:hash_mismatch") in (
        cli.audit_actions()
    )


@pytest.mark.req("EF-SRC-019")
def test_third_party_plugin_requires_env_and_typed_confirmation(
    cli: Cli, tmp_path: Path, probe: Path
) -> None:
    wheel = install(cli, tmp_path, probe)
    # installed: not loaded, the reason is shown
    started = cli.run("plugin", "list", "--load")
    assert "disabled" in started.stdout and "ALLOW_UNSANDBOXED_PLUGINS" in started.stdout
    assert snapshots(probe) == []
    # the variable alone is not enough
    started = cli.run("plugin", "list", "--load", **ENABLE)
    assert "no typed confirmation" in started.stdout and snapshots(probe) == []
    # a confirmation needs the variable, and the right sentence
    refused = cli.run("plugin", "enable", "demo-plug", typed=PHRASE + "\n")
    assert refused.returncode == 1 and "SHELFHOLD_ALLOW_UNSANDBOXED_PLUGINS" in refused.stderr
    wrong = cli.run("plugin", "enable", "demo-plug", typed="yes\n", **ENABLE)
    assert wrong.returncode == 1
    assert cli.run("plugin", "enable", "demo-plug", typed="", **ENABLE).returncode == 1  # no stdin
    assert snapshots(probe) == []
    enable(cli)
    # the confirmation alone is not enough either: without the variable nothing loads
    started = cli.run("plugin", "list", "--load")
    assert "ALLOW_UNSANDBOXED_PLUGINS" in started.stdout and snapshots(probe) == []
    started = cli.run("plugin", "list", "--load", **ENABLE)
    assert "demo\tloaded" in started.stdout
    assert len(snapshots(probe)) == 1
    # every step is audited, refusals included
    actions = [a for a, _ in cli.audit_actions()]
    assert actions.count("plugin.enable_refused") == 3
    assert actions.count("plugin.enable") == 1 and actions.count("plugin.install") == 1
    # the confirmation is bound to the files: edited after the fact, it no longer counts
    site = cli.data / "plugins" / "site-packages" / "demo_plug" / "__init__.py"
    site.write_text(site.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    started = cli.run("plugin", "list", "--load", **ENABLE)
    assert "files changed" in started.stdout and len(snapshots(probe)) == 1
    # a reinstall voids the confirmation; so does editing the stored file by hand
    assert cli.run("plugin", "remove", "demo-plug").returncode == 0
    assert wheel.exists()
    done = cli.run("plugin", "install", str(wheel), "--sha256", _sha(wheel))
    assert done.returncode == 0
    assert "no typed confirmation" in cli.run("plugin", "list", "--load", **ENABLE).stdout
    state = cli.data / "plugins" / "state.json"
    raw = json.loads(state.read_text(encoding="utf-8"))
    raw["confirmed"] = {"demo-plug": {"digest": "0" * 64, "at": "2026-01-01"}}
    state.write_text(json.dumps(raw), encoding="utf-8")
    assert "files changed" in cli.run("plugin", "list", "--load", **ENABLE).stdout
    assert len(snapshots(probe)) == 1


def _sha(path: Path) -> str:
    return guard.file_sha256(path)


@pytest.mark.req("EF-SRC-019")
def test_third_party_site_packages_after_host_and_name_conflict_refused(
    cli: Cli, tmp_path: Path, probe: Path
) -> None:
    install(cli, tmp_path, probe)
    enable(cli)
    started = cli.run("plugin", "list", "--load", **ENABLE)
    assert "demo\tloaded" in started.stdout
    (snap,) = snapshots(probe)
    site = str(cli.data / "plugins" / "site-packages")
    # the directory is never on sys.path: only a finder at the very end of sys.meta_path serves it
    assert site not in [str(p) for p in snap["path"]]  # type: ignore[attr-defined]
    meta = snap["meta_path"]
    assert meta[-1] == "ThirdPartyFinder" and "PathFinder" in meta[:-1]  # type: ignore[index,operator]
    assert site not in str(snap["httpx_file"]) and site not in str(snap["shelfhold_file"])
    from shelfhold import __version__

    assert snap["shelfhold_version"] == __version__
    assert snap["group_names"] == [] or "demo" in snap["group_names"]  # type: ignore[operator]
    # the conflicting names are refused, before and after PEP 503 normalisation
    for dist, module in (
        ("shelfhold", "plug_a"),
        ("Shelfhold.SDK", "plug_b"),
        ("HTTPX", "plug_c"),
        ("Httpx2", "plug_e"),  # a dependency of the host, other spelling
        ("Starlette", "plug_f"),
        ("Demo_Plug", "plug_d"),  # the installed plugin, other spelling
        ("fine-name", "httpx"),  # a module of a host dependency
        ("fine-name2", "httpx2"),
        ("fine-name", "shelfhold"),
        ("fine-name", "json"),  # the standard library
    ):
        wheel = build_wheel(
            tmp_path, dist, {f"{module}/__init__.py": ""}, entry=("k", f"{module}:x")
        )
        done = cli.run("plugin", "install", str(wheel.path), "--sha256", wheel.sha256)
        assert done.returncode == 1 and "conflict" in done.stderr, (dist, module, done.stderr)
    # nothing else got installed
    names = {p.name for p in (cli.data / "plugins" / "site-packages").iterdir()}
    assert names == {"demo_plug", "demo_plug-1.0.dist-info"}


@pytest.mark.req("EF-SRC-019")
def test_third_party_cannot_mask_host_pth_never_run_fake_dist_info_ignored(
    cli: Cli, tmp_path: Path, probe: Path
) -> None:
    install(cli, tmp_path, probe)
    enable(cli)
    site = cli.data / "plugins" / "site-packages"
    marker = tmp_path / "pth-ran"
    payload = tmp_path / "payload-ran"
    # planted after the install: a .pth, a module shadowing the host's, a fake host dist-info
    (site / "evil.pth").write_text(f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n")
    (site / "shelfhold-9.9.dist-info").mkdir()
    (site / "shelfhold-9.9.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: shelfhold\nVersion: 9.9\n"
    )
    (site / "shelfhold-9.9.dist-info" / "entry_points.txt").write_text(
        "[shelfhold.plugins]\nevil = payload:run\n"
    )
    (site / "payload.py").write_text(f"import pathlib; pathlib.Path({str(payload)!r}).touch()\n")
    (site / "httpx_shadow.py").write_text("")
    # a file nobody installed voids the confirmations: the plugin does not even load
    started = cli.run("plugin", "list", "--load", **ENABLE)
    assert "unexpected files in site-packages" in started.stdout
    assert not marker.exists() and not payload.exists() and snapshots(probe) == []
    # a wheel carrying a .pth, a data directory or a foreign dist-info is refused at install
    for name, files in (
        ("pth-plug", {"pth_plug/__init__.py": "", "x.pth": "import os"}),
        ("data-plug", {"data_plug/__init__.py": "", "data_plug-1.0.data/scripts/s": ""}),
        ("fake-plug", {"fake_plug/__init__.py": "", "shelfhold-9.dist-info/METADATA": "Name: x"}),
    ):
        wheel = build_wheel(tmp_path, name, {}, extra=files)
        done = cli.run("plugin", "install", str(wheel.path), "--sha256", wheel.sha256)
        assert done.returncode == 1 and "bad_wheel" in done.stderr, name


@pytest.mark.req("EF-SRC-019")
def test_third_party_plugin_is_handed_no_secret_in_the_objects_it_receives(
    cli: Cli, tmp_path: Path, probe: Path
) -> None:
    (cli.data / "shelfhold.toml").write_text('public_url = "https://example.test"\n')
    install(cli, tmp_path, probe)
    enable(cli)
    done = cli.run(
        "plugin", "list", "--load", SHELFHOLD_SECRET_KEY_FILE=str(tmp_path / "key"), **ENABLE
    )
    assert "demo\tloaded" in done.stdout
    (snap,) = snapshots(probe)
    assert CANARY not in json.dumps(snap)  # token, key path and every secret-looking variable
    environ = snap["environ"]
    assert isinstance(environ, dict)
    assert "SHELFHOLD_API_TOKEN" not in environ and "SHELFHOLD_SECRET_KEY_FILE" not in environ
    assert snap["argv"][-2:] == ["list", "--load"]  # type: ignore[index]
    # the loader hands the plugin nothing: no argument, no settings object
    from shelfhold.plugin_host import registry

    class Entry:
        module = "m"
        name = "k"

        def load(self, *args: object, **kwargs: object) -> object:
            assert (args, kwargs) == ((), {})
            return object()

    load = registry._third_party_loader(tmp_path, Entry(), ())
    saved = list(sys.meta_path)
    try:
        assert load() is not None
    finally:
        sys.meta_path[:] = saved


@pytest.mark.req("EF-SRC-019")
def test_plugin_disabled_without_the_variable_when_the_server_starts(
    cli: Cli, tmp_path: Path, probe: Path
) -> None:
    install(cli, tmp_path, probe)
    enable(cli)
    code = (
        "from fastapi.testclient import TestClient\n"
        "from shelfhold.main import create_app\n"
        "with TestClient(create_app()): pass\n"
    )
    for extra in ({}, ENABLE):
        done = subprocess.run(  # noqa: S603
            [sys.executable, "-c", code],
            env={**cli.env, **extra},
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert done.returncode == 0, done.stderr
    assert len(snapshots(probe)) == 1  # loaded once, at start, and only with the variable


@pytest.mark.req("EF-SRC-019")
def test_review_pyc_planted_after_confirmation_never_runs(
    cli: Cli, tmp_path: Path, probe: Path
) -> None:
    """Review 97, blocker 1: an unchecked-hash .pyc dropped in `__pycache__` after the typed
    confirmation used to run with the confirmation still valid."""
    import py_compile

    install(cli, tmp_path, probe)
    enable(cli)
    site = cli.data / "plugins" / "site-packages" / "demo_plug"
    assert not (site / "__pycache__").exists()  # nothing is compiled at install
    pwned = tmp_path / "pwned"
    evil = tmp_path / "evil.py"
    evil.write_text(f"open({str(pwned)!r}, 'a').write('x')\n" + (site / "__init__.py").read_text())
    cache = site / "__pycache__"
    cache.mkdir()
    py_compile.compile(
        str(evil),
        cfile=str(cache / f"__init__.{sys.implementation.cache_tag}.pyc"),
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    started = cli.run("plugin", "list", "--load", **ENABLE)
    assert "files changed" in started.stdout and not pwned.exists() and snapshots(probe) == []


@pytest.mark.req("EF-SRC-019")
def test_review_loading_leaves_no_bytecode_behind(cli: Cli, tmp_path: Path, probe: Path) -> None:
    install(cli, tmp_path, probe)
    enable(cli)
    for _ in range(2):  # a second start still loads: the first one wrote no __pycache__
        assert "demo\tloaded" in cli.run("plugin", "list", "--load", **ENABLE).stdout
    assert not list((cli.data / "plugins" / "site-packages").rglob("__pycache__"))


@pytest.mark.req("EF-SRC-019")
def test_review_unconfirmed_plugin_stays_unimportable_when_another_is_granted(
    cli: Cli, tmp_path: Path, probe: Path
) -> None:
    """Review 97, blocker 2: once one plugin was granted the whole directory was importable, so
    an installed but unconfirmed plugin B ran when the host imported its module lazily (here
    `h2`, which `httpx2` imports when HTTP/2 is on)."""
    install(cli, tmp_path, probe)
    pwned = tmp_path / "pwned"
    other = build_wheel(tmp_path, "b-plug", {"h2/__init__.py": f"open({str(pwned)!r}, 'a')\n"})
    done = cli.run("plugin", "install", str(other.path), "--sha256", other.sha256)
    assert done.returncode == 0, done.stderr
    enable(cli)  # only demo-plug
    code = (
        "import importlib.util, sys\n"
        "from pathlib import Path\n"
        "from types import SimpleNamespace\n"
        "from shelfhold.plugin_host.public import start_plugins\n"
        f"s = SimpleNamespace(data_dir=Path({str(cli.data)!r}), plugin_repos=(),"
        " active_sources=())\n"
        "records = start_plugins(s)[1]\n"
        "print([(r.key, r.status.value) for r in records])\n"
        "print('h2 spec:', importlib.util.find_spec('h2'))\n"
        "try:\n"
        "    import h2\n"
        "except ImportError as exc:\n"
        "    print('h2 not importable')\n"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        env={**cli.env, **ENABLE},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert "('demo', 'loaded')" in done.stdout, done.stderr
    assert "h2 spec: None" in done.stdout and "h2 not importable" in done.stdout
    assert not pwned.exists()


@pytest.mark.req("EF-SRC-019")
def test_review_forged_state_cannot_remove_files_outside_the_plugin_directory(
    cli: Cli, tmp_path: Path, probe: Path
) -> None:
    install(cli, tmp_path, probe)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("x")
    state = cli.data / "plugins" / "state.json"
    raw = json.loads(state.read_text(encoding="utf-8"))
    entry = dict(raw["installed"]["demo-plug"])
    for name, tops, info in (
        ("up-plug", ["../../../victim"], "x.dist-info"),
        ("abs-plug", [str(victim)], "x.dist-info"),
        ("info-plug", ["demo_plug"], "../../../victim"),
        ("empty-plug", [], "x.dist-info"),
    ):
        raw["installed"][name] = {**entry, "top_levels": tops, "dist_info": info}
    state.write_text(json.dumps(raw), encoding="utf-8")
    for name in ("up-plug", "abs-plug", "info-plug", "empty-plug"):
        done = cli.run("plugin", "remove", name)
        assert done.returncode == 1 and "bad_state" in done.stderr, name
        assert (victim / "keep").exists()


@pytest.mark.req("EF-SRC-019")
@pytest.mark.parametrize(
    "files",
    [
        {
            "x_plug/__init__.py": "",
            "evil.egg-info/PKG-INFO": "Name: evil",
            "evil.egg-info/entry_points.txt": "",
        },
        {"x_plug/__init__.py": "", "sitecustomize.py": ""},
        {"x_plug/__init__.py": "", "usercustomize.py": ""},
        {"x_plug/__init__.py": "", "x_plug/__pycache__/m.cpython-313.pyc": ""},
        {"x_plug/__init__.py": "", "x_plug/native.cpython-313-x86_64-linux-gnu.so": "x"},
        {"x_plug/__init__.py": "", "x_plug.libs/lib.so": "x"},
        {"x_plug/__init__.py": "", "a_link": ""},
        {"json.cpython-313-x86_64-linux-gnu.so": "x"},  # the stdlib's name once its suffixes go
        {"json.py": ""},
    ],
    ids=[
        "egg-info",
        "sitecustomize",
        "usercustomize",
        "pycache",
        "extension",
        "libs",
        "stray-file",
        "so-json",
        "json",
    ],
)
def test_review_wheel_content_that_plugins_never_need_is_refused(
    cli: Cli, tmp_path: Path, files: dict[str, str]
) -> None:
    wheel = build_wheel(tmp_path, "x-plug", {}, extra=files)
    done = cli.run("plugin", "install", str(wheel.path), "--sha256", wheel.sha256)
    assert done.returncode == 1 and ("bad_wheel" in done.stderr or "conflict" in done.stderr)
    site = cli.data / "plugins" / "site-packages"
    assert not site.exists() or list(site.iterdir()) == []
    assert any(a == "plugin.install_refused" for a, _ in cli.audit_actions())


@pytest.mark.req("EF-SRC-019")
def test_review_a_failed_placement_is_a_refusal_not_a_traceback(
    cli: Cli, tmp_path: Path, probe: Path
) -> None:
    install(cli, tmp_path, probe)
    other = plugin_wheel(tmp_path, "other-plug", "other", probe, "other_plug")
    site = cli.data / "plugins" / "site-packages"
    site.chmod(0o500)  # the files cannot be placed
    try:
        done = cli.run("plugin", "install", str(other.path), "--sha256", other.sha256)
    finally:
        site.chmod(0o700)
    assert done.returncode == 1 and "bad_wheel" in done.stderr and "Traceback" not in done.stderr
    assert not (site / "other_plug").exists()
    assert ("plugin.install_refused", "other_plug-1.0-py3-none-any.whl:bad_wheel") in (
        cli.audit_actions()
    )


@pytest.mark.req("EF-SRC-019")
def test_review_nothing_third_party_loads_when_the_start_is_refused(
    cli: Cli, tmp_path: Path, probe: Path
) -> None:
    """Review 97: plugins load after the instance lock and the schema check, not before."""
    import sqlite3
    from contextlib import closing

    install(cli, tmp_path, probe)
    enable(cli)
    with closing(sqlite3.connect(cli.data / "shelfhold.db")) as db:
        db.execute("UPDATE alembic_version SET version_num = 'from_the_future'")
        db.commit()
    code = (
        "from fastapi.testclient import TestClient\n"
        "from shelfhold.main import create_app\n"
        "with TestClient(create_app()): pass\n"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        env={**cli.env, **ENABLE},
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode != 0 and "from_the_future" in done.stderr
    assert snapshots(probe) == []


@pytest.mark.req("EF-SRC-019")
def test_review_install_is_offline_and_resolves_no_dependency(cli: Cli, tmp_path: Path) -> None:
    """Regression guard for "no dependency resolution, offline": index variables point at a
    socket that records any contact, the wheel declares `Requires-Dist`, and the install must
    succeed without a single connection and without installing the dependency."""
    import hashlib
    import socket
    import zipfile

    built = build_wheel(tmp_path, "dep-plug", {"dep_plug/__init__.py": "x = 1\n"})
    with zipfile.ZipFile(built.path) as archive:
        files = {n: archive.read(n) for n in archive.namelist()}
    files["dep_plug-1.0.dist-info/METADATA"] += b"Requires-Dist: requests>=2\n"
    with zipfile.ZipFile(built.path, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    sha = hashlib.sha256(built.path.read_bytes()).hexdigest()

    with socket.socket() as dead_index:
        dead_index.bind(("127.0.0.1", 0))
        dead_index.listen(8)
        dead_index.setblocking(False)
        url = f"http://127.0.0.1:{dead_index.getsockname()[1]}/simple"
        done = cli.run(
            "plugin",
            "install",
            str(built.path),
            "--sha256",
            sha,
            UV_INDEX_URL=url,
            UV_EXTRA_INDEX_URL=url,
            PIP_INDEX_URL=url,
            PIP_EXTRA_INDEX_URL=url,
            UV_NO_BINARY="1",
            UV_OFFLINE="0",
        )
        assert done.returncode == 0, done.stderr
        with pytest.raises(BlockingIOError):
            dead_index.accept()  # nobody connected to the index
    site = cli.data / "plugins" / "site-packages"
    assert sorted(p.name for p in site.iterdir()) == ["dep_plug", "dep_plug-1.0.dist-info"]
