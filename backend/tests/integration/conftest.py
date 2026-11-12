import pytest

from tests import audit_links


@pytest.fixture(scope="session", autouse=True)
def _link_audit():  # type: ignore[no-untyped-def]
    """EF-EXP-015: no hard or symbolic link from the source outside the known roads."""
    audit_links.install()
    yield
    assert audit_links.violations == []
