"""ENF-LEG-013: the simulated Kavita and its fixtures are synthetic."""

import re

import pytest

from tests.kavita_env import OPENAPI_DIR

TESTS = OPENAPI_DIR.parents[2]
FILES = [*(TESTS / "fakes" / "kavita").glob("*.py"), TESTS / "kavita_env.py"]
HOST = re.compile(r"https?://([A-Za-z0-9.-]+)")
SYNTHETIC_HOSTS = re.compile(r"(\.lan|\.invalid|\.example|^example\.(com|org|net)|^localhost)$")


@pytest.mark.req("ENF-LEG-013")
def test_fake_kavita_is_synthetic() -> None:
    assert FILES
    for path in FILES:
        for host in HOST.findall(path.read_text("utf-8")):
            assert SYNTHETIC_HOSTS.search(host), f"{path.name}: non-synthetic host {host}"
    from tests.fakes.kavita.server import default_libraries

    for library in default_libraries():
        assert library["name"].startswith("Synthetic ")
