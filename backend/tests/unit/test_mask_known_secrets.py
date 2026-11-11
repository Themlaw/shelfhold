"""M2 L07 (EF-SRC-013): a registered value is masked in text that leaves the process outside a
log line, also when a URL carries it percent-encoded."""

import secrets
from collections.abc import Iterator
from urllib.parse import quote

import pytest

from shelfhold.infrastructure.logging import (
    REDACTED,
    mask_known_secrets,
    register_secret_value,
)
from tests.redaction_support import forget_secret_values

pytestmark = pytest.mark.req("EF-SRC-013")


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    forget_secret_values()
    yield
    forget_secret_values()


def test_a_percent_encoded_secret_is_masked() -> None:
    value = secrets.token_hex(8) + "/+ "
    register_secret_value(value)
    text = f"upstream refused /a?k={quote(value, safe='')}"
    masked = mask_known_secrets(text)
    assert value not in masked and quote(value, safe="") not in masked
    assert REDACTED in masked and masked.startswith("upstream refused")


def test_plain_text_and_unrelated_escapes_are_left_alone() -> None:
    register_secret_value(secrets.token_hex(8))
    assert mask_known_secrets("50% off /a?q=%20x") == "50% off /a?q=%20x"
    assert mask_known_secrets("nothing here") == "nothing here"
