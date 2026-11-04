"""The --shard option of tests/conftest.py partitions the suite: no test lost, none duplicated."""

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

_SUITE = "\n".join(f"def test_{i}() -> None:\n    pass\n" for i in range(40))


@pytest.mark.req("ENF-SEC-007")
def test_shards_partition_the_suite(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_suite=_SUITE)
    pytester.makeconftest(Path(__file__).parents[1].joinpath("conftest.py").read_text("utf-8"))
    seen: list[str] = []
    for index in (1, 2, 3):
        result = pytester.runpytest_subprocess("--shard", f"{index}/3", "--collect-only", "-q")
        seen += [ln for ln in result.outlines if "::" in ln]
    assert len(seen) == len(set(seen)) == 40


@pytest.mark.req("ENF-SEC-007")
def test_shard_option_is_validated(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_suite=_SUITE)
    pytester.makeconftest(Path(__file__).parents[1].joinpath("conftest.py").read_text("utf-8"))
    result = pytester.runpytest_subprocess("--shard", "5/4")
    assert result.ret != 0
