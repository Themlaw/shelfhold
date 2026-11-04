"""structlog configuration: JSON in containers, readable in development (ENF-OBS-001)."""

import logging
import re
from typing import Literal

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

_SENSITIVE = re.compile(r"secret|token|password|api_key|authorization|cookie", re.IGNORECASE)
REDACTED = "***"


def _redact(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    for key in event_dict:
        if _SENSITIVE.search(key):
            event_dict[key] = REDACTED
    return event_dict


def configure_logging(*, level: str, log_format: Literal["json", "console"]) -> None:
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(),  # stdout resolved at each call
        cache_logger_on_first_use=False,
    )
