"""ENF-SEC-005: secrets are redacted in logs, bodies and headers included."""

import json
import logging
import secrets

import pytest
import structlog

from shelfhold.infrastructure.logging import (
    REDACTED,
    configure_logging,
    forget_secret_values,
    redact_value,
    register_secret_value,
)

# Fake secrets are built at run time so that no literal in the repository looks like one.
API_KEY = "kv" + "-" + secrets.token_hex(10)
TOKEN = secrets.token_hex(8)
BASIC = secrets.token_urlsafe(9)


def last_record(captured: str) -> dict[str, object]:
    lines = [line for line in captured.splitlines() if line.startswith("{")]
    return json.loads(lines[-1])


@pytest.mark.req("ENF-SEC-005")
def test_secrets_redacted_including_api_key_in_bodies(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="INFO", log_format="json")
    log = structlog.get_logger()
    body = {"url": "http://kavita.local", "apiKey": API_KEY, "nested": [{"x-api-key": API_KEY}]}
    log.info("request_body_dict", body=body)
    log.info("request_body_json", body=json.dumps(body))
    log.info("request_body_bytes", body=json.dumps(body).encode())
    log.info("form", body=f"name=a&apiKey={API_KEY}&other=1")
    log.info("header_line", line=f"x-api-key: {API_KEY}")
    out = capsys.readouterr().out
    assert API_KEY not in out
    assert out.count(REDACTED) >= 5


@pytest.mark.req("ENF-SEC-005")
def test_non_secret_parts_of_a_body_stay_readable() -> None:
    redacted = redact_value({"url": "http://kavita.local", "apiKey": API_KEY, "n": 3})
    assert redacted == {"url": "http://kavita.local", "apiKey": REDACTED, "n": 3}
    as_text = redact_value(json.dumps({"url": "u", "apiKey": API_KEY}))
    assert json.loads(str(as_text)) == {"url": "u", "apiKey": REDACTED}


@pytest.mark.req("ENF-SEC-005")
@pytest.mark.parametrize(
    "key",
    ["apiKey", "api_key", "api-key", "x-api-key", "X-Api-Key", "Authorization", "Set-Cookie",
     "password", "passphrase", "client_secret", "access_token", "master_key"],
)  # fmt: skip
def test_sensitive_key_names(key: str) -> None:
    assert redact_value({key: TOKEN}) == {key: REDACTED}


@pytest.mark.req("ENF-SEC-005")
@pytest.mark.parametrize(
    "template",
    [
        "Authorization: Bearer {t}",
        "authorization=Basic {b}",
        "Cookie: sid={t}; theme=dark",
        "apiKey={t}&x=1",
        "'apiKey': '{t}'",
        '{{"password":"{t}"}}',
    ],
)
def test_secrets_inside_free_text(template: str) -> None:
    scrubbed = str(redact_value(template.format(t=TOKEN, b=BASIC)))
    assert TOKEN not in scrubbed
    assert BASIC not in scrubbed


@pytest.mark.req("ENF-SEC-005")
def test_registered_secret_values_are_scrubbed_wherever_they_appear(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="INFO", log_format="json")
    secret = secrets.token_urlsafe(24)
    register_secret_value(secret)
    try:
        structlog.get_logger().info(f"cannot decrypt with {secret}", detail={"note": secret})
        logging.getLogger("uvicorn.error").warning("library saw %s", secret)
        try:
            raise ValueError(f"bad key {secret}")
        except ValueError:
            structlog.get_logger().exception("failed")
    finally:
        forget_secret_values()
    assert secret not in capsys.readouterr().out


@pytest.mark.req("ENF-SEC-005")
def test_short_registered_values_are_ignored() -> None:
    register_secret_value("ab")
    try:
        assert redact_value("about a cab") == "about a cab"
    finally:
        forget_secret_values()


@pytest.mark.req("ENF-SEC-005")
def test_existing_redaction_contract_is_unchanged() -> None:
    event = {"event": "login", "user": "alice", "api_key": TOKEN, "count": 3}
    assert redact_value(event) == {
        "event": "login",
        "user": "alice",
        "api_key": REDACTED,
        "count": 3,
    }
