"""structlog configuration: JSON in containers, readable in development (ENF-OBS-001).

structlog renders through the standard library (`ProcessorFormatter` on a root handler), so
uvicorn and every other library also emit one JSON line, with the request context merged in.
"""

import json
import logging
import re
import sys
from collections.abc import Mapping
from typing import Literal

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

_SENSITIVE = re.compile(
    r"secret|token|password|passphrase|api[-_]?key|authorization|cookie|credential|master[-_]?key",
    re.IGNORECASE,
)
REDACTED = "***"
_HANDLER_MARK = "_shelfhold_handler"
_LIBRARY_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
_MAX_DEPTH = 12
_MIN_KNOWN_SECRET = 8  # shorter values would blank ordinary words

# `name: value`, `name=value`, `"name": "value"`, `'name': 'value'` in free text (headers, forms,
# JSON that is not parsed, reprs). Cookies and authorisation take the rest of the line / scheme.
_NAME = r"[\w-]*(?:secret|token|password|passphrase|api[-_]?key|credential|master[-_]?key)[\w-]*"
_FREE_TEXT = re.compile(
    rf"""(?P<head>(?P<q1>["']?)(?:{_NAME})(?P=q1)\s*[:=]\s*(?P<q2>["']?))"""
    r"""(?:(?:Bearer|Basic|Token)\s+)?(?P<value>[^\s"'&,;}\]]+)""",
    re.IGNORECASE,
)
_LINE_VALUE = re.compile(
    r"""(?P<head>(?P<q1>["']?)(?:set-)?(?:cookie|authorization)(?P=q1)\s*[:=]\s*(?P<q2>["']?))"""
    r"""(?P<value>[^\r\n"']+)""",
    re.IGNORECASE,
)
_known_values: set[str] = set()


def register_secret_value(value: str) -> None:
    """Scrub this exact value wherever it shows up later (key material, a passphrase)."""
    if len(value) >= _MIN_KNOWN_SECRET:
        _known_values.add(value)


def forget_secret_values() -> None:
    _known_values.clear()


def _scrub_text(text: str) -> str:
    for known in _known_values:
        text = text.replace(known, REDACTED)
    text = _LINE_VALUE.sub(lambda m: m.group("head") + REDACTED, text)
    return _FREE_TEXT.sub(lambda m: m.group("head") + REDACTED, text)


def _redact_string(text: str) -> str:
    stripped = text.lstrip()
    if stripped[:1] in ("{", "["):
        try:
            parsed = json.loads(text)
        except ValueError:
            return _scrub_text(text)
        return json.dumps(redact_value(parsed), ensure_ascii=False)
    return _scrub_text(text)


def redact_value(value: object, _depth: int = 0) -> object:
    """Redact a log value: by key name in mappings (bodies and headers, nested), by pattern in
    text (JSON text is parsed first), and any registered secret value everywhere."""
    if _depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(value, Mapping):
        return {
            key: REDACTED
            if isinstance(key, str) and _SENSITIVE.search(key)
            else redact_value(item, _depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [redact_value(item, _depth + 1) for item in value]
    if isinstance(value, bytes | bytearray):
        return _redact_string(bytes(value).decode("utf-8", errors="replace"))
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _redact(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict):
        if _SENSITIVE.search(key):
            event_dict[key] = REDACTED
        else:
            event_dict[key] = redact_value(event_dict[key])
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
                _redact,  # tracebacks and messages are text now: scrub them too
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
