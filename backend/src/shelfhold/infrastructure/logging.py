"""structlog configuration: JSON in containers, readable in development (ENF-OBS-001).

structlog renders through the standard library (`ProcessorFormatter` on a root handler), so
uvicorn and every other library also emit one JSON line, with the request context merged in.
"""

import logging
import re
import sys
from typing import Literal

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

_SENSITIVE = re.compile(r"secret|token|password|api_key|authorization|cookie", re.IGNORECASE)
REDACTED = "***"
_HANDLER_MARK = "_shelfhold_handler"
_LIBRARY_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _redact(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    for key in event_dict:
        if _SENSITIVE.search(key):
            event_dict[key] = REDACTED
    return event_dict


def configure_logging(*, level: str, log_format: Literal["json", "console"]) -> None:
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact,
        structlog.processors.StackInfoRenderer(),
    ]
    renderer: Processor = (
        structlog.processors.JSONRenderer()
        if log_format == "json"
        else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
    handler = logging.StreamHandler(sys.stdout)  # resolved now, so tests can capture it
    setattr(handler, _HANDLER_MARK, True)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                renderer,
            ],
        )
    )
    root = logging.getLogger()
    for existing in [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)
    for name in _LIBRARY_LOGGERS:  # uvicorn must not keep its own plain-text handlers
        library = logging.getLogger(name)
        library.handlers.clear()
        library.propagate = True
