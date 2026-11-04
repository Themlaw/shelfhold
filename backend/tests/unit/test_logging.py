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
    structlog.get_logger().info("probe", api_token="hunter2", note="visible")  # noqa: S106
    record = json_records(capsys.readouterr().out)[0]
    assert record["api_token"] == "***"  # noqa: S105
    assert record["note"] == "visible"


def test_console_format_is_available_for_development(capsys: pytest.CaptureFixture[str]) -> None:
    import structlog

    configure_logging(level="DEBUG", log_format="console")
    structlog.get_logger().debug("probe")
    out = capsys.readouterr().out
    assert "probe" in out
    assert not out.startswith("{")


def test_unhandled_http_exception_and_library_logs_are_json_with_request_id(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    import logging

    configure_logging(level="INFO", log_format="json")
    migrate(settings)
    app = create_app(settings)

    def boom() -> None:
        logging.getLogger("uvicorn.error").warning("library line")
        raise RuntimeError("kaboom")

    app.add_api_route("/boom", boom)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom", headers={"X-Request-ID": "req-boom"})
    assert response.status_code == 500
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    records = [json.loads(line) for line in lines]  # every single line is JSON
    library = next(r for r in records if r["event"] == "library line")
    assert library["request_id"] == "req-boom"
    failure = next(r for r in records if r["event"] == "http_unhandled_exception")
    assert failure["request_id"] == "req-boom"
    assert failure["level"] == "error"
    assert "kaboom" in str(failure["exception"])
