"""ci.yml aggregation: the required check `ci-success` must depend on every gate."""

from pathlib import Path
from typing import Any

import pytest
import yaml

CI = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml"


@pytest.mark.req("ENF-SEC-007")
def test_ci_success_needs_every_gate_including_dco_title_and_traceability() -> None:
    ci: dict[str, Any] = yaml.safe_load(CI.read_text(encoding="utf-8"))
    jobs = ci["jobs"]
    assert {"dco", "conventional-title"} <= set(jobs)  # names required by the ruleset
    traceability = jobs["traceability"]
    assert not traceability.get("continue-on-error")
    needs = set(jobs["ci-success"]["needs"])
    assert needs >= {"lint", "denylist", "dco", "conventional-title", "traceability"}
    # every job of the workflow is aggregated, nothing can be forgotten
    assert needs == set(jobs) - {"ci-success"}
    assert jobs["ci-success"]["if"] == "always()"
