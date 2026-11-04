"""structlog configuration: JSON in containers, readable in development (ENF-OBS-001).

structlog renders through the standard library (`ProcessorFormatter` on a root handler), so
uvicorn and every other library also emit one JSON line, with the request context merged in.
"""

import base64
import json
import logging
import re
import sys
from collections.abc import Mapping
from typing import Literal

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

_WORDS = (
    r"kavita[-_]?key|komga|secret|token|passw?o?r?d?|passphrase|pwd|pass|key|auth|cookie|session"
    r"|credential"
)
_SENSITIVE = re.compile(_WORDS, re.IGNORECASE)
REDACTED = "***"
_HANDLER_MARK = "_shelfhold_handler"
_LIBRARY_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
_MAX_DEPTH = 12
_MIN_KNOWN_SECRET = 8  # shorter values would blank ordinary words

# `name: value`, `name=value`, `"name": "value"`, `{'name': b'value'}`, `name:\"value\"` in free
# text. A quoted value runs to its closing quote (spaces, `;` and `&` included); an unquoted one
# runs to the next structural delimiter (whitespace, quote, `,` `}` `]` `)`), so `;` and `&`
# inside it are part of the secret. `Cookie`/`Authorization` take the rest of the line.
_NAME = rf"[\w-]*(?:{_WORDS})[\w-]*"
_FREE_TEXT = re.compile(
    rf"""(?P<head>(?P<q1>["']?)(?:{_NAME})(?P=q1)\s*[:=]\s*)"""
    r"""(?:b?\\?(?P<q>["'])(?:(?!\\?(?P=q)).)*\\?(?P=q)"""
    r"""|(?:(?:Bearer|Basic|Token)\s+)?[^\s"',}\])]+)""",
    re.IGNORECASE | re.DOTALL,
)
_LINE_VALUE = re.compile(
    r"""(?P<head>(?P<q1>["']?)(?:set-)?(?:cookie|authorization)(?P=q1)\s*[:=]\s*(?P<q2>["']?))"""
    r"""(?P<value>[^\r\n"']+)""",
    re.IGNORECASE,
)
_URL_USERINFO = re.compile(r"(?P<head>//[^/\s:@]*:)[^/\s@]+(?P<tail>@)")
_OPDS_KEY = re.compile(r"(?P<head>/api/opds/)[^/\s?#]+")
_known_values: set[str] = set()


def register_secret_value(value: str) -> None:
    """Scrub this exact value (and its base64 forms) wherever it shows up later."""
    if len(value) < _MIN_KNOWN_SECRET:
        return
    raw = value.encode("utf-8", errors="replace")
    _known_values.update(
        {value, base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode()}
    )


def forget_secret_values() -> None:
    _known_values.clear()


def _scrub_text(text: str) -> str:
    for known in sorted(_known_values, key=len, reverse=True):
        text = text.replace(known, REDACTED)
    text = _URL_USERINFO.sub(lambda m: m.group("head") + REDACTED + m.group("tail"), text)
    text = _OPDS_KEY.sub(lambda m: m.group("head") + REDACTED, text)
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


def _text_of(value: object) -> str | None:
    if isinstance(value, bytes | bytearray):
        return bytes(value).decode("utf-8", errors="replace")
    return value if isinstance(value, str) else None


def redact_value(value: object, _depth: int = 0) -> object:
    """Redact a log value: by key name in mappings and in `(name, value)` pairs (ASGI headers),
    by pattern in text (JSON text is parsed first), and any registered secret value everywhere."""
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
        return [_redact_item(item, _depth + 1) for item in value]
    text = _text_of(value)
    if text is not None:
        return _redact_string(text)
    return value


def _redact_item(item: object, depth: int) -> object:
    if isinstance(item, list | tuple) and len(item) == 2:
        name = _text_of(item[0])
        if name is not None and _SENSITIVE.search(name):
            return [name, REDACTED]
        if name is not None:
            return [name, redact_value(item[1], depth + 1)]
    return redact_value(item, depth)


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
