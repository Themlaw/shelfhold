"""The real application over the reference profile, for the API benches (ENF-PERF-004, -005, -007).

A migrated database holds the CI profile of ENF-PERF-008 (2 000 Works, 60 000 Units, seed fixed in
`generator.py`, so two runs read the same rows), the search index is rebuilt from it, and the
application (`create_app`, every middleware, the real routes) is served in process to an
authenticated administrator. Nothing here reaches the network, and nothing is replaced: the boot,
its workers and its start-up repair are the ones of production, and the clock of a measure is the
wall clock.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from shelfhold.catalog.public import rebuild_search_index
from shelfhold.config import Settings
from shelfhold.identity.adapters.passwords import Argon2Hasher
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import identity
from tests.auth_env import PASSWORD, AuthEnv, auth_env
from tests.bench.generator import CI_PROFILE, Manifest, generate_catalog
from tests.bench.harness import load_reference

SETTLE_S = 1200.0  # the start-up repair writes the descriptors of 60 000 Units: wait for it
WARM_UP = 5
SAMPLES = 100


def budgets() -> dict[str, float]:
    """The thresholds of the spec, read from the versioned reference and never set here."""
    return dict(load_reference()["budgets"]["api"])


@dataclass(frozen=True, slots=True)
class Reference:
    env: AuthEnv
    manifest: Manifest

    @property
    def client(self) -> TestClient:
        return self.env.client


ADMIN = "user0"  # the administrator of the profile (6 users, the first is the admin)


def load_profile(settings: Settings, *, search_index: bool = True) -> Manifest:
    """Migrate, then write the reference profile (and the search index) in bulk. The profile's
    own administrator gets a password: the benches sign in as her, no user is added."""
    migrate(settings)
    engine = create_db_engine(settings.database_path)
    try:
        manifest = generate_catalog(engine, CI_PROFILE)
        give_password(engine)
        if search_index:
            with engine.begin() as connection:
                rebuild_search_index(connection)
            with engine.connect() as connection:
                # what the engine writes itself after a batch: the planner's statistics
                connection.execute(sa.text("PRAGMA optimize"))
    finally:
        engine.dispose()
    return manifest


def give_password(engine: sa.Engine) -> None:
    with engine.begin() as connection:
        changed = connection.execute(
            sa.update(identity.user)
            .where(identity.user.c.username == ADMIN, identity.user.c.role == "admin")
            .values(password_hash=Argon2Hasher().hash(PASSWORD))
        )
    assert changed.rowcount == 1


def record(name: str, **numbers: float) -> None:
    """Keep what a bench measured, for the release (L58): appended to the JSON file named by
    `SHELFHOLD_BENCH_RESULTS` when it is set (the nightly job), nowhere otherwise."""
    print(f"BENCH {name} {json.dumps(numbers, sort_keys=True)}")
    target = os.environ.get("SHELFHOLD_BENCH_RESULTS")
    if not target:
        return
    path = Path(target)
    found = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    found[name] = {**numbers, "profile": CI_PROFILE.name}
    path.write_text(json.dumps(found, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def settled() -> None:
    """Wait for the start-up repair (a thread of the runtime) so it never overlaps a measure."""
    deadline = time.monotonic() + SETTLE_S
    while time.monotonic() < deadline:
        if not any(t.name == "meta-repair" and t.is_alive() for t in threading.enumerate()):
            return
        time.sleep(0.05)
    raise AssertionError(f"the start-up repair is still running after {SETTLE_S:.0f} s")


def sign_in(env: AuthEnv) -> None:
    answer = env.client.post("/api/v1/auth/login", json={"username": ADMIN, "password": PASSWORD})
    assert answer.status_code == 200, answer.text


@contextmanager
def reference_app(settings: Settings) -> Iterator[Reference]:
    with pytest.MonkeyPatch.context() as patch:
        manifest = load_profile(settings)
        with auth_env(patch, settings, with_admin=False) as env:
            sign_in(env)
            settled()
            yield Reference(env, manifest)


def timed(client: TestClient, paths: Sequence[str], *, expect: int = 200) -> list[float]:
    """The wall time of one GET per path, in seconds. A bad status fails here, never later: a
    fast error would otherwise count as a fast answer."""
    out: list[float] = []
    for path in paths:
        started = time.perf_counter()
        answer = client.get(path)
        out.append(time.perf_counter() - started)
        assert answer.status_code == expect, f"{path}: {answer.status_code} {answer.text[:200]}"
    return out


def warm(client: TestClient, paths: Sequence[str]) -> None:
    for path in list(paths)[:WARM_UP]:
        assert client.get(path).status_code == 200


def during(
    work: Callable[[], object], probe: Callable[[], float], *, give_up_s: float = 1800
) -> tuple[list[float], object]:
    """Run `work` on a thread and call `probe` (which returns a latency) back to back until it
    ends: the samples taken while it ran, and what it returned."""
    box: dict[str, object] = {}

    def run() -> None:
        try:
            box["result"] = work()
        except BaseException as error:
            box["error"] = error

    thread = threading.Thread(target=run, name="bench-load", daemon=True)
    thread.start()
    samples: list[float] = []
    deadline = time.monotonic() + give_up_s
    while thread.is_alive() and time.monotonic() < deadline:
        samples.append(probe())
    thread.join(timeout=5)
    assert not thread.is_alive(), f"the load did not finish within {give_up_s:.0f} s"
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return samples, box.get("result")
