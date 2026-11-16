"""EF-EXP-010 (M4 L07): the local source path of Suwayomi, read from its settings.

One read-only query on `settings`, the same root field as the other settings the adapter reads. A
server that does not expose it, or answers with something that is not an absolute path, gives
`None`: the guard then knows nothing, and says so (it never invents a path).
"""

import pytest

from shelfhold.integrations.suwayomi.settings import parse_local_source_path
from tests.fakes.suwayomi import FakeSuwayomi
from tests.unit.test_suwayomi_adapter import make, run


@pytest.mark.req("EF-EXP-010")
def test_the_adapter_reads_the_local_source_path() -> None:
    service, f, server = make(FakeSuwayomi(auth_mode="none"))
    server.state["local_source_path"] = "/data/local"
    assert run(service.local_source_path(f)) == "/data/local"
    sent = set().union(*(r.fields for r in server.requests))
    assert sent <= {"aboutServer", "settings"}  # a read of the settings, nothing else


@pytest.mark.req("EF-EXP-010")
@pytest.mark.parametrize(
    "answer", [None, "", "relative/path", "/with/nul\u0000byte", 42, ["/x"], "/" + "a" * 5000]
)
def test_what_is_not_an_absolute_path_is_no_path(answer: object) -> None:
    assert parse_local_source_path({"localSourcePath": answer}) is None
    assert parse_local_source_path({}) is None
    assert parse_local_source_path(None) is None


@pytest.mark.req("EF-EXP-010")
def test_an_absolute_path_is_kept_as_the_server_wrote_it() -> None:
    assert parse_local_source_path({"localSourcePath": "/home/x/.local/share/local"}) == (
        "/home/x/.local/share/local"
    )
