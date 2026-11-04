"""Hypothesis profiles: deterministic in CI so that a red run is reproducible from the log."""

import os

from hypothesis import HealthCheck, settings

settings.register_profile(
    "ci",
    max_examples=200,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile("dev", max_examples=50, deadline=None)
settings.load_profile("ci" if os.environ.get("CI") else "dev")
