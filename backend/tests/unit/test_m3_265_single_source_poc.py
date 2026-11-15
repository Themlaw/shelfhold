"""Adversarial review of #265, the "one definition of the family shelf" half: proofs of concept,
red on purpose (`xfail` strict until fixed). See `docs/decisions/M3.md`, "#265 : contradicteur".
The production code is not touched here."""

from __future__ import annotations

import pytest

from tests.unit.test_family_shelf_single_source import SRC, second_definitions

pytestmark = pytest.mark.req("EF-USR-006", "ENF-SEC-010")


@pytest.mark.xfail(
    strict=True,
    reason="#265 contradicteur M-c: `new_edition_gate._family` is a second family rule, unseen",
)
def test_265_poc_the_admission_gate_is_a_second_family_rule_the_scan_misses() -> None:
    """`catalog/application/new_edition_gate.py:_family` says "family shelf" as
    `ceiling_of(c) < UNRATED_FLOOR` (an `AgeRating(X_MIN_AGE)`), and `catalog/domain/age.py`
    `admission` says it again as `guarded = ceiling < UNRATED_FLOOR`. That is the rule of
    `bindings.is_family`, written twice more, in the gate that decides what a family shelf takes.
    The anti-copy scan sees neither: it only knows the literal 18 and the names ADULT/FAMILY."""
    source = (SRC / "catalog/application/new_edition_gate.py").read_text("utf-8")
    assert second_definitions(source), "the scan does not see `new_edition_gate._family`"


@pytest.mark.xfail(strict=True, reason="#265 contradicteur m-d: the anti-copy scan is bypassed")
@pytest.mark.parametrize(
    "copy",
    [
        "ok = ceiling <= 17\n",
        "ok = ceiling > 17\n",
        "MAJORITY = 18\nok = ceiling < MAJORITY\n",
        "def family(ceiling, adult=18):\n    return ceiling < adult\n",
        'q = sa.text("SELECT id FROM shelf WHERE max_age_rating < 18")\n',
        "ok = ceiling < X_MIN_AGE\n",
    ],
    ids=["le-17", "gt-17", "renamed-constant", "default-argument", "raw-sql", "x-min-age"],
)
def test_265_poc_the_scan_sees_a_disguised_copy(copy: str) -> None:
    assert second_definitions(copy), f"a second family rule the scan lets through: {copy!r}"
