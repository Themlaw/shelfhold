"""PR #343 (M4 L41), relecture adverse : chaîne d'approvisionnement du miroir et vérificateur.

Each test is the proof of a hole found in review, red in strict xfail until closed.

* C1: the update checker promises never to raise into the application. A version document of
  16 KiB of `[` (under the size bound) makes `json.loads` raise `RecursionError`, which is not a
  `ValueError`: it escapes `check_for_update`. A hostile or broken mirror crashes the caller.
* C2: one concurrency group for the whole workflow. GitHub keeps one running and one pending run
  per group, and a new pending run cancels the pending one (`cancel-in-progress` only spares the
  running one). A tag pushed while a main run is in progress, then one more push on main, and the
  tag run is cancelled: the release tag never reaches the mirror, and nothing says so.
* C3: `on: push` also fires on a ref deletion (`github.event.deleted`), with `github.sha` set to
  the head of the default branch. Deleting a tag `vX.Y.Z` the mirror does not have yet (mirror
  down at release time, token added later) pushes the head of main to the mirror as `vX.Y.Z`:
  a release tag that names the wrong commit. Nothing checks that SHA is the commit of the tag.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from shelfhold.platform.update_check import check_for_update
from shelfhold_sdk.fetcher import Response

pytestmark = pytest.mark.req("ENF-GOV-004")

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-mirror.yml"
SCRIPT = ROOT / ".github" / "scripts" / "publish-mirror.sh"


class _Answer:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def get(self, url: str) -> Response:
        return Response(status=200, body=self.body)


def _wf() -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return loaded


def test_c1_deeply_nested_document_is_a_silent_failure() -> None:
    body = b"[" * 16_000  # under MAX_BYTES (16 KiB)
    status = asyncio.run(check_for_update(_Answer(body), "https://x.example/v.json", current="1.0.0"))
    assert status.state == "failed"


def test_c2_concurrency_group_is_per_ref() -> None:
    group = str(_wf()["concurrency"]["group"])
    assert "github.ref" in group  # main and each tag queue separately: no run replaces another


def test_c3_ref_deletion_never_pushes_and_tag_sha_is_checked() -> None:
    (job,) = _wf()["jobs"].values()
    guarded = "github.event.deleted" in str(job.get("if", ""))
    script = SCRIPT.read_text(encoding="utf-8")
    checks_tag = "rev-parse" in script and "refs/tags/" in script
    assert guarded or checks_tag
