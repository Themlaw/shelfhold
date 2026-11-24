"""What the M6 journeys bring to the stack: wheels, a signed plugin index, and the way to publish
them on the simulated repository (`fakes/plugin_index`).

Every key is drawn by the test that needs one (`pynacl`): no secret is a literal, a fixture or a
file of the repository. The signature written here is minisign's prehashed form (`ED`, Ed25519 over
the BLAKE2b-512 of the file), the one `shelfhold plugin index` verifies. Synthetic data only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import tomllib
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from journey import Journey
from m6_hosts import CATALOGUE_HOST, INDEX_HOST
from nacl.signing import SigningKey

E2E = Path(__file__).resolve().parents[1]
REPO = E2E.parent
EXAMPLE = REPO / "docs" / "plugins" / "exemple"
CASSETTE = EXAMPLE / "tests" / "cassettes" / "catalogue.json"
CRASH_PLUGIN = (E2E / "fakes" / "m6" / "crash_plugin.py").read_text(encoding="utf-8")

SOURCES = "sources"  # the simulated sources (`fake-sources`), where the repository is served
INDEX_URL = f"https://{INDEX_HOST}/index.json"
WHEEL_BASE = f"https://{INDEX_HOST}/wheels/"
PUBLISHED = "/tmp/plugin-index"  # noqa: S108 - the tmpfs of the simulated server


# -- wheels ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Wheel:
    name: str  # the file name
    data: bytes
    sha256: str  # hex


def build_wheel(
    dist: str, modules: dict[str, str], *, entry: tuple[str, str], version: str
) -> Wheel:
    """A pure wheel zipped offline: its modules, its metadata and its `shelfhold.plugins` entry."""
    base = dist.replace("-", "_").replace(".", "_")
    info = f"{base}-{version}.dist-info"
    files = dict(modules)
    files[f"{info}/METADATA"] = f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n"
    files[f"{info}/WHEEL"] = (
        "Wheel-Version: 1.0\nGenerator: e2e\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    files[f"{info}/entry_points.txt"] = f"[shelfhold.plugins]\n{entry[0]} = {entry[1]}\n"
    files[f"{info}/RECORD"] = "".join(f"{n},,\n" for n in [*files, f"{info}/RECORD"])
    name = f"{base}-{version}-py3-none-any.whl"
    buffer = _zip(files)
    return Wheel(name, buffer, hashlib.sha256(buffer).hexdigest())


def _zip(files: dict[str, str]) -> bytes:
    import io

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for member, text in files.items():
            archive.writestr(member, text)
    return out.getvalue()


def example_project() -> dict[str, object]:
    data = tomllib.loads((EXAMPLE / "pyproject.toml").read_text(encoding="utf-8"))
    project: dict[str, object] = data["project"]
    return project


def example_entry() -> tuple[str, str]:
    """The `shelfhold.plugins` entry point the example's own `pyproject.toml` declares."""
    declared = example_project()["entry-points"]["shelfhold.plugins"]  # type: ignore[index]
    ((key, target),) = declared.items()  # type: ignore[attr-defined]
    return str(key), str(target)


def example_wheel(*, tamper: bool = False) -> Wheel:
    """The wheel of `docs/plugins/exemple`, from its own sources, name, version and entry point.
    `tamper`: one more module, so the bytes are not the ones an index vouched for."""
    modules = {
        f"plugin_exemple/{path.name}": path.read_text(encoding="utf-8")
        for path in sorted((EXAMPLE / "src" / "plugin_exemple").glob("*.py"))
    }
    if tamper:
        modules["plugin_exemple/extra.py"] = "# not in the index\n"
    key, target = example_entry()
    return build_wheel(
        str(example_project()["name"]), modules, entry=(key, target),
        version=str(example_project()["version"]),
    )  # fmt: skip


def small_plugin_wheel(key: str) -> Wheel:
    """A one-module source plugin (`fakes/m6/crash_plugin.py`) under its own key."""
    module = f"e2e_{key}"
    return build_wheel(
        f"e2e-{key}",
        {f"{module}/__init__.py": CRASH_PLUGIN.replace("__KEY__", key)},
        entry=(key, f"{module}:make"),
        version="1.0.0",
    )


def dist_of(key: str) -> str:
    """The distribution name `plugin enable` takes for a small plugin."""
    return f"e2e-{key}"


# -- the signed index ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexKey:
    signing: SigningKey
    public_b64: str  # what the administrator pins
    key_id: str
    raw_id: bytes


def make_key() -> IndexKey:
    signing = SigningKey.generate()
    raw_id = secrets.token_bytes(8)
    blob = b"Ed" + raw_id + bytes(signing.verify_key)
    return IndexKey(signing, base64.b64encode(blob).decode("ascii"), raw_id.hex().upper(), raw_id)


def expires(days: int = 30) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def index_document(version: int, when: str, plugins: list[dict[str, object]]) -> bytes:
    document = {
        "format": 1,
        "version": version,
        "expires": when,
        "plugins": plugins,
        "revoked": {"plugin_keys": [], "wheel_sha256": []},
    }
    return json.dumps(document).encode("utf-8")


def entry_for(key: str, version: str, wheel: Wheel, *, sha256: str | None = None) -> dict[str, object]:
    return {
        "key": key,
        "version": version,
        "api": 2,
        "rpc_protocol": 1,
        "wheel_url": WHEEL_BASE + wheel.name,
        "sha256": "sha256:" + (sha256 or wheel.sha256),
        "min_host": "1.0.0",
        "deprecated": False,
        "replaced_by": None,
    }


def sign(document: bytes, signer: IndexKey, comment: str) -> bytes:
    """A detached minisign signature, as `index.json.minisig` carries it."""
    message = hashlib.blake2b(document, digest_size=64).digest()
    signature = bytes(signer.signing.sign(message).signature)
    blob = b"ED" + signer.raw_id + signature
    global_sig = bytes(signer.signing.sign(comment.encode("utf-8") + signature).signature)
    return (
        f"untrusted comment: signature by {signer.key_id}\n"
        f"{base64.b64encode(blob).decode('ascii')}\n"
        f"trusted comment: {comment}\n"
        f"{base64.b64encode(global_sig).decode('ascii')}\n"
    ).encode("ascii")


def publish(j: Journey, host: str, path: str, data: bytes) -> None:
    """Put a file where the simulated repository serves it (`host`, `path` as asked)."""
    j.put_bytes(f"{PUBLISHED}/{host}/{path.lstrip('/')}", data, service="fake-" + SOURCES, old=False)


def publish_index(
    j: Journey, signer: IndexKey, version: int, plugins: list[dict[str, object]]
) -> None:
    """A signed index of that version, and its signature, on the repository."""
    when = expires()
    document = index_document(version, when, plugins)
    comment = f"shelfhold-index v1 version={version} expires={when}"
    publish(j, INDEX_HOST, "index.json", document)
    publish(j, INDEX_HOST, "index.json.minisig", sign(document, signer, comment))


def publish_wheel(j: Journey, wheel: Wheel, data: bytes | None = None) -> None:
    publish(j, INDEX_HOST, f"wheels/{wheel.name}", wheel.data if data is None else data)


def publish_catalogue(j: Journey) -> None:
    """The synthetic catalogue the example plugin asks for, at the address it ships with."""
    cassette = json.loads(CASSETTE.read_text(encoding="utf-8"))
    body = next(i["body"] for i in cassette["interactions"] if i["url"].endswith("catalogue.json"))
    publish(j, CATALOGUE_HOST, "catalogue.json", str(body).encode("utf-8"))
