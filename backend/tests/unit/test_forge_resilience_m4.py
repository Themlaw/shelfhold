# ruff: noqa: S106 (synthetic secrets)
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
from shelfhold_sdk.fetcher import Response

from shelfhold.config import ConfigError, Settings, load_settings
from shelfhold.infrastructure.fetcher.errors import FetchTimeout, SsrfBlocked
from shelfhold.platform.update_check import UpdateStatus, check_for_update, update_check_enabled

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


def _script(
    mode: str, tmp_path: Path, *, cwd: str | Path | None = None, **env: str
) -> tuple[int, str]:
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
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.returncode, done.stdout + done.stderr


def _git(repo: Path, *args: str) -> str:
    env = {
        "PATH": "/usr/bin:/bin",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org",
        "HOME": str(repo),
    }  # fmt: skip
    done = subprocess.run(  # noqa: S603
        ["/usr/bin/git", "-c", "commit.gpgsign=false", *args],
        cwd=repo, env=env, capture_output=True, text=True, check=True,
    )  # fmt: skip
    return done.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A clone with the tag v1.2.0 on the first commit and a later head: (path, tag sha, head)."""
    repo = tmp_path / "clone"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a").write_text("1", encoding="utf-8")
    _git(repo, "add", "a")
    _git(repo, "commit", "-q", "-m", "one")
    _git(repo, "tag", "v1.2.0")
    tagged = _git(repo, "rev-parse", "HEAD")
    (repo / "a").write_text("2", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "two")
    return repo, tagged, _git(repo, "rev-parse", "HEAD")


@pytest.mark.req("ENF-GOV-004")
def test_publish_mirror_refuses_a_tag_that_does_not_designate_the_sha(tmp_path: Path) -> None:
    """C3: a deleted tag makes GitHub report the head of main as the sha of the run."""
    repo, tagged, head = _repo(tmp_path)
    tag = {"REF_TYPE": "tag", "REF_NAME": "v1.2.0"}
    assert _script("check", tmp_path, cwd=repo, SHA=tagged, **tag)[0] == 0
    code, out = _script("check", tmp_path, cwd=repo, SHA=head, **tag)
    assert code != 0 and "does not designate" in out
    code, out = _script("check", tmp_path, cwd=repo, SHA=head, REF_TYPE="tag", REF_NAME="v9.9.9")
    assert code != 0 and "does not designate" in out  # a tag the clone does not have
    code, _ = _script(
        "push", tmp_path, cwd=repo, SHA=head, MIRROR_USER="acme", MIRROR_TOKEN="x", **tag
    )
    assert code != 0


@pytest.mark.req("ENF-GOV-004")
def test_publish_mirror_token_only_in_the_environment_of_git_push(tmp_path: Path) -> None:
    """The token is on no command line, in no output, and only `git push` has it in its
    environment (every other command runs without it); git gets it back through the askpass."""
    repo, tagged, _head = _repo(tmp_path)
    secret = "tok-" + "z9y8x7" * 4  # synthetic
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    log = tmp_path / "git.log"
    (fakebin / "git").write_text(
        "#!/bin/sh\n"
        f'printf "ARGV %s\\n" "$*" >> "{log}"\n'
        f'env | sed "s/^/ENV[$1] /" >> "{log}"\n'
        'if [ "$1" = push ]; then\n'
        f'  printf "ASKUSER %s\\n" "$("$GIT_ASKPASS" "Username for x: ")" >> "{log}"\n'
        f'  printf "ASKPASS %s\\n" "$("$GIT_ASKPASS" "Password for x: ")" >> "{log}"\n'
        "fi\n"
        f'if [ "$1" = rev-parse ]; then echo {tagged}; fi\n',
        encoding="utf-8",
    )
    (fakebin / "git").chmod(0o755)
    code, out = _script(
        "push", tmp_path, cwd=repo, PATH=f"{fakebin}:/usr/bin:/bin", REF_TYPE="tag",
        REF_NAME="v1.2.0", SHA=tagged, MIRROR_USER="mirror-user", MIRROR_TOKEN=secret,
    )  # fmt: skip
    assert code == 0, out
    lines = log.read_text(encoding="utf-8").splitlines()
    assert any(
        line.startswith("ARGV push https://codeberg.org/acme/shelfhold.git") for line in lines
    )
    assert f"ASKPASS {secret}" in lines and "ASKUSER mirror-user" in lines  # given back by askpass
    argv = [line for line in lines if line.startswith("ARGV ")]
    assert all(secret not in line for line in argv)  # not on any command line
    for line in lines:
        if line.startswith("ENV[") and secret in line:
            assert line.startswith("ENV[push] MIRROR_TOKEN="), line  # only git push, only that
    assert not any(line.startswith("ENV[rev-parse]") and secret in line for line in lines)
    assert secret not in out and secret not in (tmp_path / "summary").read_text("utf-8")
    assert not list(tmp_path.glob("tmp.*"))  # the askpass helper is gone


@pytest.mark.req("ENF-GOV-004")
def test_publish_mirror_needs_its_own_user_and_the_mirror_environment(tmp_path: Path) -> None:
    (job,) = _wf()["jobs"].values()
    assert job["environment"] == "mirror"  # the secret is reachable from main and v* tags only
    assert "github.event.deleted != true" in str(job["if"])  # a deleted ref is never mirrored
    assert _wf()["concurrency"]["cancel-in-progress"] is False  # nor a tag run cancelled
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "vars.MIRROR_USER" in text and "SECOND_REGISTRY_USER" not in text
    detect = next(s for s in job["steps"] if s.get("id") == "token")
    assert "vars.MIRROR_USER != ''" in yaml.safe_dump(detect)  # no user: succeeds, and says so
    assert not re.search(r"\bexit [1-9]", str(detect["run"]))
    assert "MIRROR_USER" in str(detect["run"]) and "GITHUB_STEP_SUMMARY" in str(detect["run"])
    code, out = _script("push", tmp_path, MIRROR_TOKEN="x")
    assert (
        code != 0 and "MIRROR_USER" in out
    )  # no MIRROR_USER: the script itself refuses, it does not fall back


@pytest.mark.req("ENF-GOV-004")
def test_publish_mirror_default_target_and_refs(tmp_path: Path) -> None:
    code, out = _script("check", tmp_path)
    assert code == 0 and "https://codeberg.org/acme/shelfhold.git" in out
    assert "refs/heads/main" in out
    repo, tagged, _head = _repo(tmp_path)
    code, out = _script("check", tmp_path, cwd=repo, REF_TYPE="tag", REF_NAME="v1.2.0", SHA=tagged)
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
    code, out = _script("push", tmp_path, MIRROR_TOKEN=fake, MIRROR_USER="acme", **env)
    assert code != 0
    assert fake not in out


# --- the real client (simulated transport): what goes on the wire, how much is read -------------

HOST = "updates.example.org"


def _network(respond: Any = None) -> tuple[Any, Any]:
    from shelfhold.infrastructure.fetcher import Network
    from tests.fakes_fetcher import FakeResolver, Recorder

    recorder = Recorder(respond)
    return Network(
        resolver=FakeResolver({HOST: ["93.184.216.34"]}), transport=recorder.transport
    ), recorder


async def _real(network: Any, url: str = URL) -> UpdateStatus:
    from shelfhold.platform.update_check import update_fetcher

    fetcher = update_fetcher(network)
    try:
        return await check_for_update(fetcher, url, current="1.1.0")
    finally:
        await fetcher.aclose()


@pytest.mark.req("ENF-GOV-004", "ENF-PRIV-001")
def test_update_check_real_client_sends_a_neutral_agent_and_nothing_else() -> None:
    import httpx2

    from shelfhold import __version__
    from shelfhold.platform.update_check import UPDATE_USER_AGENT

    network, recorder = _network(lambda _r: httpx2.Response(200, content=OK))
    status = asyncio.run(_real(network))
    assert (status.state, status.latest, status.newer) == ("ok", "1.2.0", True)
    (request,) = recorder.requests
    headers = {k.lower(): v for k, v in request.headers.items()}
    assert headers["user-agent"] == UPDATE_USER_AGENT
    assert set(headers) <= {"host", "user-agent", "accept", "accept-encoding", "connection"}
    assert request.method == "GET" and not request.content
    assert request.url.path == "/shelfhold/latest.json" and not request.url.query
    wire = repr((str(request.url), sorted(headers.items())))
    assert __version__ not in wire and "1.1.0" not in wire and "Shelfhold/" not in wire


@pytest.mark.req("ENF-GOV-004")
def test_update_check_real_client_bounds_the_read_not_only_the_result() -> None:
    import httpx2

    from shelfhold.platform.update_check import MAX_BYTES

    network, _ = _network(lambda _r: httpx2.Response(200, content=b"x" * (MAX_BYTES + 1)))
    with structlog.testing.capture_logs() as logs:
        status = asyncio.run(_real(network))
    assert status.state == "failed"
    # the Fetcher's own bound stopped it (a FetchTooLarge), not the check made once all was read
    assert [(e["event"], e["reason"]) for e in logs] == [
        ("http.refused", "FetchTooLarge"),
        ("update_check.failed", "FetchTooLarge"),
    ]
    assert {e["log_level"] for e in logs} == {"info"}  # the Fetcher's own line too: no warning


@pytest.mark.req("ENF-GOV-004")
def test_update_check_real_client_keeps_the_ssrf_guard() -> None:
    from shelfhold.infrastructure.fetcher import Network
    from tests.fakes_fetcher import FakeResolver, Recorder

    recorder = Recorder()
    network = Network(resolver=FakeResolver({HOST: ["10.0.0.5"]}), transport=recorder.transport)
    with structlog.testing.capture_logs() as logs:
        status = asyncio.run(_real(network))
    assert status.state == "failed" and recorder.requests == []  # a private address: no request
    assert {e["log_level"] for e in logs} == {"info"}


@pytest.mark.req("ENF-GOV-004")
def test_update_check_cli_is_off_by_default_and_makes_no_connection(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
) -> None:
    from tests.auth_env import run_cli

    network, recorder = _network()
    monkeypatch.setattr("shelfhold.composition._DEFAULT_NETWORK", network)
    monkeypatch.delenv("SHELFHOLD_UPDATE_CHECK_URL", raising=False)
    assert run_cli(monkeypatch, settings, "update", "check") == 2
    assert "SHELFHOLD_UPDATE_CHECK_URL" in capsys.readouterr().err
    assert recorder.requests == []


@pytest.mark.req("ENF-GOV-004")
@pytest.mark.parametrize(
    ("status", "body", "code", "stream", "key"),
    [
        (200, OK, 0, "out", "update.newer"),
        (200, json.dumps({"version": "1.0.0"}).encode(), 0, "out", "update.current"),
        (500, b"down", 1, "err", "update.failed"),
        (200, b"[" * 16_000, 1, "err", "update.failed"),
    ],
)
def test_update_check_cli_through_the_real_client(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    status: int,
    body: bytes,
    code: int,
    stream: str,
    key: str,
) -> None:
    import httpx2

    from shelfhold.i18n import t
    from tests.auth_env import run_cli

    network, recorder = _network(lambda _r: httpx2.Response(status, content=body))
    monkeypatch.setattr("shelfhold.composition._DEFAULT_NETWORK", network)
    monkeypatch.setenv("SHELFHOLD_UPDATE_CHECK_URL", URL)
    assert run_cli(monkeypatch, settings, "update", "check") == code
    captured = capsys.readouterr()
    latest = json.loads(body)["version"] if status == 200 and body[:1] == b"{" else ""
    message = t(key, latest=latest) if key != "update.failed" else t(key)
    assert message in (captured.out if stream == "out" else captured.err)
    assert len(recorder.requests) == 1
