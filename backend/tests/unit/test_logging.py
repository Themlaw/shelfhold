"""ENF-OBS-001: structlog JSON with request_id in context."""

import json

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.infrastructure.logging import configure_logging
from shelfhold.infrastructure.migrate import migrate
from shelfhold.main import create_app


def json_records(captured: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in captured.splitlines() if line.startswith("{")]


def request_record(captured: str) -> dict[str, object]:
    records = [r for r in json_records(captured) if r["event"] == "http_request"]
    assert len(records) == 1
    return records[0]


def test_log_is_json_and_contains_request_id(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(level="INFO", log_format="json")
    migrate(settings)
    with TestClient(create_app(settings)) as client:
        response = client.get("/healthz", headers={"X-Request-ID": "req-abc-123"})
    record = request_record(capsys.readouterr().out)
    assert response.headers["X-Request-ID"] == "req-abc-123"
    assert record["request_id"] == "req-abc-123"
    assert record["level"] == "info"
    assert record["status"] == 200
    assert record["path"] == "/healthz"
    assert "timestamp" in record


@pytest.mark.parametrize("header", [None, "bad id with spaces", "x" * 200])
def test_request_id_is_generated_when_absent_or_unsafe(
    settings: Settings, capsys: pytest.CaptureFixture[str], header: str | None
) -> None:
    configure_logging(level="INFO", log_format="json")
    migrate(settings)
    headers = {"X-Request-ID": header} if header else {}
    with TestClient(create_app(settings)) as client:
        response = client.get("/healthz", headers=headers)
    generated = response.headers["X-Request-ID"]
    assert len(generated) == 32
    assert request_record(capsys.readouterr().out)["request_id"] == generated


def test_secret_looking_fields_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    import structlog

    configure_logging(level="INFO", log_format="json")
    structlog.get_logger().info("probe", kavita_api_key="hunter2", note="visible")
    record = json_records(capsys.readouterr().out)[0]
    assert record["kavita_api_key"] == "***"
    assert record["note"] == "visible"


def test_console_format_is_available_for_development(capsys: pytest.CaptureFixture[str]) -> None:
    import structlog

    configure_logging(level="DEBUG", log_format="console")
    structlog.get_logger().debug("probe")
    out = capsys.readouterr().out
    assert "probe" in out
    assert not out.startswith("{")
