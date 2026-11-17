"""ENF-GOV-004 (L41): the forge can be lost without the family losing its updates.

Update check off by default and silent when it fails, plugin index address configurable, the
repository mirror pushed with the token in one step only, the two image names written in the
reference compose, the continuity procedure written down. No test calls the network: the fetcher is
a recording fake."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import structlog
import yaml

from shelfhold.config import ConfigError, Settings, load_settings
from shelfhold.infrastructure.fetcher.errors import FetchTimeout, SsrfBlocked
from shelfhold.platform.update_check import UpdateStatus, check_for_update, update_check_enabled
from shelfhold.sdk.fetcher import Response

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-mirror.yml"
URL = "https://updates.example.org/shelfhold/latest.json"
OK = json.dumps({"version": "1.2.0"}).encode()


class RecordingFetcher:
    """The SDK `Fetcher` as far as the checker uses it: records every call, answers a script."""

    def __init__(self, answer: Response | BaseException) -> None:
        self.answer = answer
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get(self, url: str, **kwargs: Any) -> Response:
        self.calls.append((url, kwargs))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    async def post(self, url: str, **kwargs: Any) -> Response:  # pragma: no cover - must not run
        self.calls.append((url, {"post": True, **kwargs}))
        raise AssertionError("the update check only reads")


def _run(fetcher: RecordingFetcher, url: str | None = URL, current: str = "1.1.0") -> UpdateStatus:
    return asyncio.run(check_for_update(fetcher, url, current=current))


# --- off by default --------------------------------------------------------------------------


@pytest.mark.req("ENF-GOV-004")
def test_update_check_off_by_default(env: dict[str, str]) -> None:
    assert Settings().update_check_url is None
    assert Settings().plugin_index_url is None
    assert not update_check_enabled(load_settings(env).values)
    fetcher = RecordingFetcher(AssertionError("must not be asked"))
    status = _run(fetcher, url=None)
    assert status.state == "off" and status.latest is None
    assert fetcher.calls == []  # not one request leaves the instance by default (ENF-PRIV-001)
    env["SHELFHOLD_UPDATE_CHECK_URL"] = URL
    assert update_check_enabled(load_settings(env).values)


# --- failure is silent -----------------------------------------------------------------------


@pytest.mark.req("ENF-GOV-004")
@pytest.mark.parametrize(
    "answer",
    [
        FetchTimeout("slow"),
        SsrfBlocked("blocked"),
        OSError("down"),
        Response(status=500, body=b"oops"),
        Response(status=404),
        Response(status=200, body=b"<html>not json</html>"),
        Response(status=200, body=json.dumps({"version": "<script>x</script>"}).encode()),
        Response(status=200, body=json.dumps({"version": "1.2"}).encode()),
        Response(status=200, body=json.dumps(["1.2.0"]).encode()),
        Response(status=200, body=b"{" * 10 + b"x" * 2_000_000),  # far over the bound
    ],
)
def test_update_check_failure_is_silent(answer: Response | BaseException) -> None:
    with structlog.testing.capture_logs() as logs:
        status = _run(RecordingFetcher(answer))
    assert status.state == "failed" and status.latest is None and not status.newer
    levels = {entry["log_level"] for entry in logs}
    assert levels == {"info"}, logs  # never a warning or an error: it blocks nothing
    assert len(logs) == 1 and logs[0]["event"] == "update_check.failed"


@pytest.mark.req("ENF-GOV-004")
def test_update_check_does_not_swallow_a_cancellation() -> None:
    fetcher = RecordingFetcher(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        _run(fetcher)


@pytest.mark.req("ENF-GOV-004")
@pytest.mark.parametrize(
    ("latest", "newer"),
    [("1.2.0", True), ("1.1.0", False), ("1.0.9", False), ("1.10.0", True)],
)
def test_update_check_reports_only_a_newer_version(latest: str, newer: bool) -> None:
    body = json.dumps({"version": latest, "notes": "<b>ignored</b>"}).encode()
    status = _run(RecordingFetcher(Response(status=200, body=body)))
    assert (status.state, status.latest, status.newer) == ("ok", latest, newer)


@pytest.mark.req("ENF-GOV-004")
def test_update_check_never_blocks_on_a_hung_server() -> None:
    class Hung(RecordingFetcher):
        async def get(self, url: str, **kwargs: Any) -> Response:
            await asyncio.sleep(3600)
            raise AssertionError

    with structlog.testing.capture_logs() as logs:
        status = asyncio.run(
            check_for_update(Hung(OSError()), URL, current="1.1.0", timeout_s=0.05)
        )
    assert status.state == "failed" and {e["log_level"] for e in logs} == {"info"}


# --- ADR-0030: nothing that names the instance leaves it --------------------------------------


@pytest.mark.req("ENF-GOV-004", "ENF-PRIV-001")
def test_update_check_via_fetcher_sends_no_instance_identifier(
    env: dict[str, str], tmp_path: Path
) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / ".shelfhold-mount-id").write_text("mount-id-aaaa-bbbb", encoding="utf-8")
    env["SHELFHOLD_PUBLIC_URL"] = "https://family-instance.example.net"
    env["SHELFHOLD_UPDATE_CHECK_URL"] = URL
    settings = load_settings(env).values
    fetcher = RecordingFetcher(Response(status=200, body=OK))
    asyncio.run(check_for_update(fetcher, settings.update_check_url, current="1.1.0"))
    assert len(fetcher.calls) == 1
    url, kwargs = fetcher.calls[0]
    assert url == URL  # exactly the configured address: no query, no path suffix
    assert not kwargs.get("params") and not kwargs.get("headers")
    assert not kwargs.get("data") and not kwargs.get("json") and "post" not in kwargs
    sent = json.dumps([url, kwargs], default=str)
    for identifier in ("mount-id-aaaa-bbbb", "family-instance", str(tmp_path), "1.1.0"):
        assert identifier not in sent, identifier  # not the instance, its host, nor its version


# --- the index address is a setting; the signature, not the host, authenticates --------------


@pytest.mark.req("ENF-GOV-004")
def test_plugin_index_url_is_configurable_and_credential_free(env: dict[str, str]) -> None:
    env["SHELFHOLD_PLUGIN_INDEX_URL"] = "https://mirror.example.org/shelfhold/index.json"
    assert load_settings(env).values.plugin_index_url == env["SHELFHOLD_PLUGIN_INDEX_URL"]
    for bad in (
        "javascript:alert(1)",
        "ftp://mirror.example.org/index.json",
        "https://user:secret@mirror.example.org/index.json",
        "https://mirror.example.org/index.json?token=abc",
        "https://mirror.example.org/a b",
    ):
        for name in ("SHELFHOLD_PLUGIN_INDEX_URL", "SHELFHOLD_UPDATE_CHECK_URL"):
            wrong = dict(env)
            wrong.pop("SHELFHOLD_PLUGIN_INDEX_URL", None)
            wrong[name] = bad
            with pytest.raises(ConfigError):
                load_settings(wrong)


@pytest.mark.req("ENF-GOV-004")
def test_the_index_documents_that_the_signature_authenticates_not_the_host() -> None:
    text = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if "SHELFHOLD_PLUGIN_INDEX_URL" in line)
    assert re.search(r"signature", row, re.I) and re.search(r"h[ôo]te?|host", row, re.I)
    row = next(line for line in text.splitlines() if "SHELFHOLD_UPDATE_CHECK_URL" in line)
    assert re.search(r"défaut|default", row, re.I) and re.search(r"identifiant", row, re.I)


# --- the mirror workflow ----------------------------------------------------------------------


def _wf() -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return loaded


@pytest.mark.req("ENF-GOV-004")
def test_publish_mirror_token_only_in_push_step() -> None:
    wf: dict[Any, Any] = _wf()
    triggers: dict[str, Any] = wf.get("on") or dict(wf)[True]  # YAML 1.1 reads `on` as True
    assert set(triggers) <= {"push", "workflow_dispatch"}  # never a pull request
    assert triggers["push"]["branches"] == ["main"] and triggers["push"]["tags"] == ["v*"]
    assert wf["permissions"] == {}
    (job,) = wf["jobs"].values()
    assert job["permissions"] == {"contents": "read"}  # no write, no id-token
    assert job.get("timeout-minutes")
    steps = job["steps"]
    holders = [s for s in steps if "CODEBERG_TOKEN" in yaml.safe_dump(s) and "id" not in s]
    pushes = [
        s
        for s in steps
        if "git push" in str(s.get("run", "")) or "mirror.sh" in str(s.get("run", ""))
    ]
    assert len(holders) == 1 and holders[0] in pushes  # the secret reaches the push step only
    assert "secrets.CODEBERG_TOKEN" in yaml.safe_dump(holders[0]["env"])
    assert "secrets." not in yaml.safe_dump(wf["jobs"]).replace("secrets.CODEBERG_TOKEN", "")
    # detection hands over a boolean, never the value, and never fails; the push waits for it
    detect = next(s for s in steps if s.get("id") == "token")
    assert "secrets.CODEBERG_TOKEN != ''" in yaml.safe_dump(detect)
    assert "secrets.CODEBERG_TOKEN" not in str(detect.get("run", ""))
    assert not re.search(r"\bexit [1-9]", str(detect.get("run", "")))
    assert "steps.token.outputs.enabled == 'true'" in str(holders[0].get("if", ""))
    assert steps.index(detect) < steps.index(holders[0])
    # without the token the job succeeds and says so
    assert "absent" in str(detect["run"]) and "GITHUB_STEP_SUMMARY" in str(detect["run"])
    # the checkout keeps no credential of the source repository next to the mirror token
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] is False
    assert checkout["with"]["fetch-depth"] == 0
    # the token is never on a command line or echoed
    text = WORKFLOW.read_text(encoding="utf-8")
    assert not re.search(
        r"echo[^\n]*(\$\{?\w*TOKEN|secrets\.)|--password|https://[^\s/]*:[^\s/@]*@", text
    )
    assert "pull_request" not in text


@pytest.mark.req("ENF-GOV-004")
def test_publish_mirror_script_refuses_an_unknown_host_and_never_sees_the_token_on_argv() -> None:
    script = (ROOT / ".github" / "scripts" / "publish-mirror.sh").read_text(encoding="utf-8")
    assert re.search(r"^re=.*https://codeberg", script, re.M) and "GIT_ASKPASS" in script
    assert "set -euo pipefail" in script
    assert not re.search(r"git [^\n]*\$\{?MIRROR_TOKEN", script)  # via askpass, not the arguments


# --- the reference compose names both images ---------------------------------------------------


@pytest.mark.req("ENF-GOV-004")
def test_compose_documents_both_image_names() -> None:
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "ghcr.io/themlaw/shelfhold" in text
    assert "codeberg.org/<owner>/shelfhold" in text  # a placeholder, never a real organisation
    assert not re.search(r"codeberg\.org/(?!<owner>)[\w.-]+", text)
    # only GHCR is live: the second name is a comment, one line to change if the forge is lost
    service = yaml.safe_load(text)["services"]["shelfhold"]
    assert service["image"].startswith("ghcr.io/themlaw/shelfhold:")
    assert "docs/admin/verifier-les-images.md" in text


# --- the continuity procedure -------------------------------------------------------------------


@pytest.mark.req("ENF-GOV-004")
def test_governance_has_a_continuity_procedure() -> None:
    text = (ROOT / "GOVERNANCE.md").read_text(encoding="utf-8")
    section = text.split("## Continuité en cas de retrait", 1)[1].split("\n## ", 1)[0]
    for needle in (
        "codeberg.org/<owner>/shelfhold",
        "SHELFHOLD_PLUGIN_INDEX_URL",
        "SHELFHOLD_UPDATE_CHECK_URL",
        "cosign verify",
        "CODEBERG_TOKEN",
        "miroir",
    ):
        assert needle in section, needle
    assert len(re.findall(r"^\d+\. ", section, re.M)) >= 4  # numbered steps, not a wish
    assert not re.search(r"codeberg\.org/(?!<owner>)[\w.-]+", section)


@pytest.mark.req("ENF-GOV-004")
def test_the_requirement_stays_deferred_until_the_first_signed_publication() -> None:
    """The live part (token, first publication verified on the second registry) is D-P1: the
    deferral stays, narrowed to that, until it is done or re-dated in writing by the maintainer."""
    import tomllib

    exceptions = tomllib.loads((ROOT / "tools" / "traceability-exceptions.toml").read_text("utf-8"))
    (deferred,) = [d for d in exceptions["deferred"] if d["requirement"] == "ENF-GOV-004"]
    assert deferred["reason"].startswith("M4") and "D-P1" in deferred["reason"]


# --- the push script: the token only ever goes to the known host ---------------------------------

SCRIPT = ROOT / ".github" / "scripts" / "publish-mirror.sh"
SHA = "a" * 40


def _script(mode: str, tmp_path: Path, **env: str) -> tuple[int, str]:
    base = {
        "PATH": "/usr/bin:/bin",
        "OWNER": "Acme",
        "REPO_NAME": "Shelfhold",
        "REF_TYPE": "branch",
        "REF_NAME": "main",
        "SHA": SHA,
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
    }
    done = subprocess.run(  # noqa: S603
        ["/bin/bash", str(SCRIPT), mode],
        env={**base, **env},
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, done.stdout + done.stderr


@pytest.mark.req("ENF-GOV-004")
def test_publish_mirror_default_target_and_refs(tmp_path: Path) -> None:
    code, out = _script("check", tmp_path)
    assert code == 0 and "https://codeberg.org/acme/shelfhold.git" in out
    assert "refs/heads/main" in out
    code, out = _script("check", tmp_path, REF_TYPE="tag", REF_NAME="v1.2.0")
    assert code == 0 and "refs/tags/v1.2.0" in out


@pytest.mark.req("ENF-GOV-004")
@pytest.mark.parametrize(
    "env",
    [
        {"MIRROR_URL": "https://evil.example.net/acme/shelfhold.git"},
        {"MIRROR_URL": "http://codeberg.org/acme/shelfhold.git"},
        {"MIRROR_URL": "https://user:pw@codeberg.org/acme/shelfhold.git"},
        {"MIRROR_URL": "https://codeberg.org.evil.example/acme/shelfhold.git"},
        {"MIRROR_URL": "https://codeberg.org/acme/../x.git"},
        {"REF_NAME": "feature"},
        {"REF_TYPE": "tag", "REF_NAME": "v1.2"},
        {"REF_TYPE": "tag", "REF_NAME": "v1.2.0;rm -rf x"},
        {"SHA": "HEAD"},
    ],
)
def test_publish_mirror_script_refuses_hostile_input(tmp_path: Path, env: dict[str, str]) -> None:
    fake = "not-a-real-token"
    code, out = _script("push", tmp_path, MIRROR_TOKEN=fake, **env)
    assert code != 0
    assert fake not in out
