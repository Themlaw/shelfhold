"""Behavioural tests of the release/cleanup scripts, run against stubbed docker, gh and curl."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

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
GH = r"""#!/usr/bin/env bash
state="$STUB_STATE"; echo "gh $*" >> "$state/log"
case "$*" in
  *check-runs*) cat "$state/ci" ;;
  *"-X DELETE"*) ;;
  *versions*) cat "$state/versions.json" ;;
  *users/*) echo "${STUB_OWNER_TYPE:-User}" ;;
esac
"""
CURL = r"""#!/usr/bin/env bash
case "$*" in *token*) echo '{"token":"t"}' ;; *) cat "$STUB_STATE/tags.json" ;; esac
"""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("docker", DOCKER), ("gh", GH), ("curl", CURL), ("sleep", "#!/bin/sh\n")):
        p = bindir / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "state"
    state.mkdir()
    (state / "ci").write_text("success")
    (state / "tags.json").write_text(json.dumps({"tags": []}))
    return {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "STUB_STATE": str(state),
        "RUNNER_TEMP": str(tmp_path),
        "GH_TOKEN": "x",
        "IMAGE": "ghcr.io/Themlaw/shelfhold",
        "REPO": "Themlaw/shelfhold",
        "SHA": "c" * 40,
        "VERSION": "0.1.0",
        "ACTOR": "me",
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
    env["VERSION"] = "0.1.1"
    put(env, f"{IMG}:sha-{'c' * 40}", DIGEST)
    (Path(env["STUB_STATE"]) / "tags.json").write_text(
        json.dumps({"tags": ["0.1.0", "0.1.2", "0.1", "edge"]})
    )
    r = run("release-retag.sh", env)
    assert r.returncode == 0, r.stderr
    assert created(env) == {f"{IMG}:0.1.1"}


# --- ghcr-cleanup.sh -----------------------------------------------------------------------


@pytest.mark.req("ENF-QUAL-007")
def test_cleanup_lowercases_the_image_name_and_defaults_to_dry_run(env: dict[str, str]) -> None:
    state = Path(env["STUB_STATE"])
    old = {
        "id": 1,
        "name": OTHER,
        "created_at": "2020-01-01T00:00:00Z",
        "metadata": {"container": {"tags": ["build-x-amd64"]}},
    }
    kept = {
        "id": 2,
        "name": DIGEST,
        "created_at": "2020-01-01T00:00:00Z",
        "metadata": {"container": {"tags": ["edge"]}},
    }
    (state / "versions.json").write_text(json.dumps(old) + json.dumps(kept))
    (state / "raw_index").write_text(json.dumps({"manifests": []}))
    e = {**env, "OWNER": "Themlaw", "PACKAGE": "shelfhold"}
    e.pop("DRY_RUN", None)
    r = run("ghcr-cleanup.sh", e)
    assert r.returncode == 0, r.stderr
    log = (state / "log").read_text()
    assert "ghcr.io/themlaw/shelfhold@" in log and "Themlaw" not in log.replace(
        "users/Themlaw", ""
    ).replace("orgs/Themlaw", "").replace("users/themlaw", "")
    assert "-X DELETE" not in log  # dry-run unless DRY_RUN=false


@pytest.mark.req("ENF-QUAL-007")
def test_cleanup_deletes_only_when_dry_run_is_explicitly_false(env: dict[str, str]) -> None:
    state = Path(env["STUB_STATE"])
    old = {
        "id": 1,
        "name": OTHER,
        "created_at": "2020-01-01T00:00:00Z",
        "metadata": {"container": {"tags": ["build-x-amd64"]}},
    }
    (state / "versions.json").write_text(json.dumps(old))
    (state / "raw_index").write_text(json.dumps({"manifests": []}))
    e = {**env, "OWNER": "Themlaw", "PACKAGE": "shelfhold", "DRY_RUN": "false"}
    assert run("ghcr-cleanup.sh", e).returncode == 0
    assert "-X DELETE" in (state / "log").read_text()


# --- is-reversible.py ----------------------------------------------------------------------


def reversible(tmp_path: Path, body: str) -> bool:
    f = tmp_path / "rev.py"
    f.write_text(f"def upgrade():\n    pass\n\n\ndef downgrade():\n{body}\n")
    return (
        subprocess.run(  # noqa: S603
            [sys.executable, "-I", str(SCRIPTS / "is-reversible.py"), str(f)], check=False
        ).returncode
        == 0
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('    """nothing to undo"""', False),
        ("    pass", False),
        ('    """doc"""\n    pass', False),
        ("    raise NotImplementedError", False),
        ('    op.drop_table("t")', True),
        ('    """doc"""\n    op.drop_table("t")', True),
    ],
)
@pytest.mark.req("ENF-OPS-002")
def test_downgrade_with_only_docstring_or_pass_is_not_reversible(
    tmp_path: Path, body: str, expected: bool
) -> None:
    assert reversible(tmp_path, body) is expected
