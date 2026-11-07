# ruff: noqa: E501  (GraphQL documents and multipart lines read better unbroken)
"""Nightly: the real download queue of the pinned Suwayomi, played with the synthetic extension
(ENF-LEG-013, EF-SRC-008, passe 12). No third-party content: the extension reads the fixture
server of `contract/suwayomi-synthetic-extension` (generated pages) on the bench's internal network.

The APK is built by the job (`build.sh`) and installed from that file; the extension-store list is
asserted empty before and after. Without an APK (`SYNTHETIC_EXTENSION_APK`) the tests fail when `CI` is set, else they are skipped.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import time
import urllib.request
import uuid
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.contract.suwayomi.harness import PASSWORD, USER, Bench

pytestmark = pytest.mark.nightly
EXTENSION = Path(__file__).resolve().parents[1] / "contract" / "suwayomi-synthetic-extension"
APK = Path(os.environ.get("SYNTHETIC_EXTENSION_APK", EXTENSION / "build" / "synthetic.apk"))
MULTIPART = "----shelfholdnightlyboundary"


class ExtensionBench(Bench):
    """The S5 bench plus the fixture server on its network."""

    def _compose(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "SUWAYOMI_DATA": str(self.data_dir),
            "SUWAYOMI_AUTH_MODE": self.mode,
            "SUWAYOMI_USER": USER,
            "SUWAYOMI_PASSWORD": self.password,
        }
        return subprocess.run(  # noqa: S603
            ["docker", "compose", "-f", str(EXTENSION / "compose.yaml"), "-p", self.project, *args],  # noqa: S607
            env=env, check=True, capture_output=True, text=True, timeout=300,
        )  # fmt: skip

    def install_extension(self, apk: bytes) -> dict[str, Any]:
        """GraphQL multipart request (the `installExternalExtension` mutation takes an upload)."""
        operations = {
            "query": "mutation($file: Upload!) { installExternalExtension(input: {extensionFile: $file}) "
            "{ extension { pkgName name isInstalled } } }",
            "variables": {"file": None},
        }
        parts = [
            ("operations", None, json.dumps(operations).encode(), "application/json"),
            ("map", None, json.dumps({"0": ["variables.file"]}).encode(), "application/json"),
            ("0", "synthetic.apk", apk, "application/vnd.android.package-archive"),
        ]
        body = b""
        for name, filename, content, kind in parts:
            disposition = f'form-data; name="{name}"' + (
                f'; filename="{filename}"' if filename else ""
            )
            body += f"--{MULTIPART}\r\nContent-Disposition: {disposition}\r\nContent-Type: {kind}\r\n\r\n".encode()
            body += content + b"\r\n"
        body += f"--{MULTIPART}--\r\n".encode()
        request = urllib.request.Request(  # noqa: S310
            self.base_url + "/api/graphql", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={MULTIPART}", **self.auth_header()},
        )  # fmt: skip
        with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
            payload = json.loads(response.read())
        assert "errors" not in payload, str(payload["errors"])[:600]
        return dict(payload["data"]["installExternalExtension"]["extension"])


@pytest.fixture(scope="module")
def bench(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ExtensionBench]:
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")
    if not APK.is_file():
        if os.environ.get("CI"):  # in the workflow a missing build is a failure, not a skip
            pytest.fail(f"the synthetic extension was not built at {APK}")
        pytest.skip(f"no synthetic extension built at {APK} (run build.sh)")
    data = tmp_path_factory.mktemp("suwayomi-extension")
    data.chmod(0o777)  # the container's user is not the runner's: it writes its data here
    instance = ExtensionBench(
        mode="basic_auth",
        data_dir=data,
        project=f"nightly-ext-{uuid.uuid4().hex[:6]}",
        password=PASSWORD,
    )
    try:
        instance.start()
        yield instance
    finally:
        try:
            instance.stop()
        finally:
            shutil.rmtree(data, ignore_errors=True)


@pytest.mark.req("ENF-LEG-013")
def test_contract_job_has_no_extension_repo_and_no_egress(bench: ExtensionBench) -> None:
    """The start-of-job assertion: empty store list, no extension yet, no route out."""
    stores = bench.gql("{ extensionStores { nodes { __typename } } }")["extensionStores"]["nodes"]
    assert stores == []
    installed = bench.gql("{ extensions { nodes { pkgName isInstalled } } }")["extensions"]["nodes"]
    assert [e for e in installed if e["isInstalled"]] == []
    _, probe = bench.probe_egress()
    assert "rc=1" in probe.stdout or "rc=124" in probe.stdout, probe.stdout


@pytest.mark.req("ENF-LEG-013", "EF-SRC-008")
def test_contract_synthetic_extension_plays_download_queue(bench: ExtensionBench) -> None:
    installed = bench.install_extension(APK.read_bytes())
    assert installed["isInstalled"] and installed["pkgName"].endswith(".synthetic")
    assert (
        bench.gql("{ extensionStores { nodes { __typename } } }")["extensionStores"]["nodes"] == []
    )
    sources = bench.gql("{ sources { nodes { id name } } }")["sources"]["nodes"]
    synthetic = next(s for s in sources if s["name"] == "Synthetic")
    popular = bench.gql(
        f'mutation {{ fetchSourceManga(input: {{source: "{synthetic["id"]}", type: POPULAR, page: 1}}) '
        "{ mangas { id title url sourceId } } }"
    )["fetchSourceManga"]["mangas"]
    alpha = next(m for m in popular if m["title"] == "Synthetic Alpha")
    assert alpha["sourceId"] == synthetic["id"] and alpha["url"] == "/series/synthetic-alpha"
    chapters = bench.gql(
        f"mutation {{ fetchChapters(input: {{mangaId: {alpha['id']}}}) {{ chapters {{ id url name }} }} }}"
    )["fetchChapters"]["chapters"]
    first = next(c for c in chapters if c["name"] == "Chapter 1")
    assert first["url"] == "/pages/synthetic-alpha/ch-001"  # the stable identity: source id + url
    bench.gql(
        f"mutation {{ enqueueChapterDownload(input: {{id: {first['id']}}}) {{ __typename }} }}"
    )
    try:  # the host waits for a state change: it times out when the queue already runs
        bench.gql("mutation { startDownloader(input: {}) { __typename } }")
    except AssertionError as error:
        if "Timed out waiting" not in str(error):
            raise
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if bench.gql(f"{{ chapter(id: {first['id']}) {{ isDownloaded }} }}")["chapter"][
            "isDownloaded"
        ]:
            break
        time.sleep(2)
    else:
        raise AssertionError("the chapter was never downloaded\n" + bench.logs()[-2000:])
    response = bench.request("GET", f"/api/v1/chapter/{first['id']}/download")
    assert response.status == 200 and response.body[:2] == b"PK"
    assert response.headers["Content-Type"] == "application/vnd.comicbook+zip"
    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
        assert archive.testzip() is None
        pages = [n for n in archive.namelist() if n.lower().endswith(".png")]
        assert len(pages) == 3  # the three generated pages, nothing else came from anywhere
