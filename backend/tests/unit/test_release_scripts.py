"""Behavioural tests of release-retag.sh, run against stubbed docker, gh and curl."""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from tests.support.tools import require_tool

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / ".github" / "scripts"
DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64
IMG = "ghcr.io/themlaw/shelfhold"

DOCKER = r"""#!/usr/bin/env bash
# stub: state dir has one file per ref (digest, or "ERR:<message>"); `create` is logged.
state="$STUB_STATE"; echo "docker $*" >> "$state/log"
if [ "$3" = inspect ] && [ "$4" != "--raw" ]; then
  f="$state/ref_$(printf '%s' "$4" | tr '/:@' '___')"
  if [ ! -f "$f" ]; then echo "ERROR: $4: not found" >&2; exit 1; fi
  c="$(cat "$f")"
  case "$c" in ERR:*) echo "${c#ERR:}" >&2; exit 1;; esac
  echo "$c"; exit 0
fi
if [ "$3" = inspect ]; then cat "$state/raw_index"; exit 0; fi
if [ "$3" = create ]; then
  shift 3; args=("$@")
  for ((i=0;i<${#args[@]};i++)); do
    if [ "${args[$i]}" = -t ]; then
      t="${args[$((i+1))]}"
      k="$(printf '%s' "$t" | tr '/:@' '___')"
      printf '%s' "$t" > "$state/created_$k"
      printf '%s' "${args[$((${#args[@]}-1))]#*@}" > "$state/ref_$k"
    fi
  done
  exit 0
fi
exit 0
"""
COSIGN = r"""#!/usr/bin/env bash
echo "cosign $*" >> "$STUB_STATE/log"
[ ! -f "$STUB_STATE/cosign_fail" ]
"""
GH = r"""#!/usr/bin/env bash
state="$STUB_STATE"; echo "gh $*" >> "$state/log"
case "$*" in
  *check-runs*) cat "$state/ci" ;;
  *commits/v*) cat "$state/tag_sha" ;;
esac
"""
CURL = r"""#!/usr/bin/env bash
case "$*" in *token*) echo '{"token":"t"}' ;; *) cat "$STUB_STATE/tags.json" ;; esac
"""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    require_tool("jq")  # release-retag.sh pipes the registry answers through jq
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (
        ("docker", DOCKER),
        ("gh", GH),
        ("curl", CURL),
        ("cosign", COSIGN),
        ("sleep", "#!/bin/sh\n"),
    ):
        p = bindir / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state"
    state.mkdir()
    (state / "ci").write_text("success")
    (state / "tag_sha").write_text("c" * 40)
    (state / "tags.json").write_text(json.dumps({"tags": []}))
    return {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "STUB_STATE": str(state),
        "RUNNER_TEMP": str(tmp_path),
        "GH_TOKEN": "x",
        "IMAGE": "ghcr.io/Themlaw/shelfhold",
        "REPO": "Themlaw/shelfhold",
        "TAG": "v0.1.0",
        "WAIT_SECONDS": "0",
    }


def ref(name: str) -> str:
    return "ref_" + name.replace("/", "_").replace(":", "_").replace("@", "_")


def put(env: dict[str, str], name: str, content: str) -> None:
    (Path(env["STUB_STATE"]) / ref(name)).write_text(content)


def created(env: dict[str, str]) -> set[str]:
    return {p.read_text() for p in Path(env["STUB_STATE"]).glob("created_*")}


def run(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["bash", str(SCRIPTS / script)],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


# --- release-retag.sh ----------------------------------------------------------------------


@pytest.mark.req("ENF-SEC-007", "ENF-OPS-002")
def test_retag_uses_the_given_sha_and_tags_version_and_minor(env: dict[str, str]) -> None:
    put(env, f"{IMG}:sha-{'c' * 40}", DIGEST)
    r = run("release-retag.sh", env)
    assert r.returncode == 0, r.stderr
    assert created(env) == {f"{IMG}:0.1.0", f"{IMG}:0.1"}


@pytest.mark.req("ENF-SEC-007")
def test_retag_refuses_an_unsigned_digest_and_writes_nothing(env: dict[str, str]) -> None:
    put(env, f"{IMG}:sha-{'c' * 40}", DIGEST)
    (Path(env["STUB_STATE"]) / "cosign_fail").write_text("")
    r = run("release-retag.sh", env)
    assert r.returncode != 0 and "no valid cosign signature" in r.stdout + r.stderr
    assert created(env) == set()


@pytest.mark.req("ENF-SEC-007")
def test_retag_verifies_the_signature_of_the_digest_before_tagging(env: dict[str, str]) -> None:
    put(env, f"{IMG}:sha-{'c' * 40}", DIGEST)
    assert run("release-retag.sh", env).returncode == 0
    log = (Path(env["STUB_STATE"]) / "log").read_text().splitlines()
    verify = next(i for i, line in enumerate(log) if line.startswith("cosign verify"))
    create = next(i for i, line in enumerate(log) if " create " in line)
    assert verify < create
    assert "image-publish" in log[verify] and "token.actions.githubusercontent.com" in log[verify]


@pytest.mark.req("ENF-SEC-007")
def test_retag_rerun_of_a_published_version_does_not_need_the_signature(
    env: dict[str, str],
) -> None:
    put(env, f"{IMG}:sha-{'c' * 40}", DIGEST)
    put(env, f"{IMG}:0.1.0", DIGEST)
    (Path(env["STUB_STATE"]) / "cosign_fail").write_text("")
    assert run("release-retag.sh", env).returncode == 0


@pytest.mark.req("ENF-SEC-007", "ENF-OPS-002")
def test_retag_refuses_to_rewrite_an_existing_version_with_another_digest(
    env: dict[str, str],
) -> None:
    put(env, f"{IMG}:sha-{'c' * 40}", DIGEST)
    put(env, f"{IMG}:0.1.0", OTHER)
    r = run("release-retag.sh", env)
    assert r.returncode != 0 and "refusing to rewrite" in r.stdout + r.stderr
    assert created(env) == set()  # nothing written, not even X.Y


@pytest.mark.req("ENF-SEC-007", "ENF-OPS-002")
def test_retag_is_idempotent_when_the_version_already_points_to_the_digest(
    env: dict[str, str],
) -> None:
    put(env, f"{IMG}:sha-{'c' * 40}", DIGEST)
    put(env, f"{IMG}:0.1.0", DIGEST)
    assert run("release-retag.sh", env).returncode == 0


@pytest.mark.req("ENF-SEC-007", "ENF-OPS-002")
def test_retag_fails_on_a_registry_error_instead_of_treating_it_as_absent(
    env: dict[str, str],
) -> None:
    put(env, f"{IMG}:sha-{'c' * 40}", DIGEST)
    put(env, f"{IMG}:0.1.0", "ERR:denied: permission_denied: 500 Internal Server Error")
    r = run("release-retag.sh", env)
    assert r.returncode != 0
    assert created(env) == set()


@pytest.mark.req("ENF-SEC-007", "ENF-OPS-002")
def test_retag_waits_boundedly_for_the_image(env: dict[str, str]) -> None:
    r = run("release-retag.sh", env)  # sha-<long> never appears
    assert r.returncode != 0 and "absent" in r.stdout + r.stderr


@pytest.mark.parametrize("conclusion", ["failure", "pending", "cancelled"])
@pytest.mark.req("ENF-SEC-007", "ENF-OPS-002")
def test_retag_requires_ci_success_on_the_commit(env: dict[str, str], conclusion: str) -> None:
    put(env, f"{IMG}:sha-{'c' * 40}", DIGEST)
    (Path(env["STUB_STATE"]) / "ci").write_text(conclusion)
    r = run("release-retag.sh", env)
    assert r.returncode != 0
    assert created(env) == set()


@pytest.mark.req("ENF-SEC-007", "ENF-OPS-002")
def test_retag_never_moves_the_minor_tag_backwards(env: dict[str, str]) -> None:
    env["TAG"] = "v0.1.1"
    put(env, f"{IMG}:sha-{'c' * 40}", DIGEST)
    (Path(env["STUB_STATE"]) / "tags.json").write_text(
        json.dumps({"tags": ["0.1.0", "0.1.2", "0.1", "edge"]})
    )
    r = run("release-retag.sh", env)
    assert r.returncode == 0, r.stderr
    assert created(env) == {f"{IMG}:0.1.1"}


@pytest.mark.parametrize("tag", ["0.1.0", "v0.1", "v0.1.0-rc1", "vX.Y.Z", "v1.2.3; id"])
@pytest.mark.req("ENF-SEC-007", "ENF-OPS-002")
def test_retag_refuses_anything_but_a_vx_y_z_tag(env: dict[str, str], tag: str) -> None:
    put(env, f"{IMG}:sha-{'c' * 40}", DIGEST)
    env["TAG"] = tag
    r = run("release-retag.sh", env)
    assert r.returncode != 0 and "vX.Y.Z" in r.stdout + r.stderr
    assert created(env) == set()
