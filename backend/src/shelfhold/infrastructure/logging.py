"""structlog configuration: JSON in containers, readable in development (ENF-OBS-001).

structlog renders through the standard library (`ProcessorFormatter` on a root handler), so
uvicorn and every other library also emit one JSON line, with the request context merged in.
"""

import base64
import functools
import inspect
import json
import logging
import re
import sys
import time
import traceback
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

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
# The HTTP client logs `HTTP Request: GET <full url>` at INFO, query string included: that is a
# key leak and a line per call. Our own `http.call` line (route only) replaces it, always.
_NEVER_BELOW_WARNING = ("httpx", "httpx2", "httpcore", "httpcore2")
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
_SOURCE_URL = re.compile(r"""\b[a-z][a-z0-9+.-]*://[^\s'"<>)\]}]+""", re.IGNORECASE)
_known_values: set[str] = set()


_QUERY_OF_URL = re.compile(
    r"(?P<url>\b[a-z][a-z0-9+.-]*://[^\s?#'\"]*)[?#][^\s'\"]*", re.IGNORECASE
)


def strip_url_queries(value: object) -> object:
    """The value with the query string and fragment cut off every URL it holds (strings, lists,
    mappings): a key can travel there."""
    if isinstance(value, str):
        return _QUERY_OF_URL.sub(lambda m: m.group("url"), value)
    if isinstance(value, list | tuple):
        return [strip_url_queries(item) for item in value]
    if isinstance(value, Mapping):
        return {key: strip_url_queries(item) for key, item in value.items()}
    return value


def debug_enabled() -> bool:
    """True when a DEBUG line would be written: guards any work only a DEBUG line needs."""
    return logging.getLogger().isEnabledFor(logging.DEBUG)


def elapsed_ms(started: float) -> int:
    """Milliseconds since `started` (a `time.perf_counter()` reading): the `duration_ms` field."""
    return round((time.perf_counter() - started) * 1000)


def route_of(url: str) -> str:
    """`host/path` of a URL: no scheme credentials, no query string and no fragment, whatever the
    query carried (an API key travels there). The path is scrubbed like any other text."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "?"
    return _scrub_text(f"{host}{port}{parts.path}")


def untrusted_name(name: str, *, limit: int = 200) -> str:
    """A name taken from an untrusted archive, for a DEBUG line only: escaped (`repr`) and cut."""
    return repr(name[:limit])


def exception_detail(error: BaseException, *, depth: int = 12) -> dict[str, object]:
    """A failure for a DEBUG line: its class, the frames (module:line in function) and its message
    escaped (`repr`): the message can quote a name from an untrusted archive or a title, and a raw
    traceback would print it as is."""
    frames = [
        f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"
        for frame in traceback.extract_tb(error.__traceback__)[-depth:]
    ]
    return {"error": type(error).__name__, "message": repr(str(error)[:500]), "frames": frames}


def log_outcome[F: Callable[..., Any]](
    event: str, summarize: Callable[[Any], Mapping[str, object]], *, level: str = "info"
) -> Callable[[F], F]:
    """Decorator for a use case: one line when it returns, with `duration_ms` and the counts
    `summarize` takes from the result (numbers and closed words, never a title or a path)."""

    def decorate(func: F) -> F:
        logger = structlog.get_logger(func.__module__)

        def emit(result: Any, started: float) -> None:
            getattr(logger, level)(event, duration_ms=elapsed_ms(started), **summarize(result))

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def run_async(*args: Any, **kwargs: Any) -> Any:
                started = time.perf_counter()
                result = await func(*args, **kwargs)
                emit(result, started)
                return result

            return cast("F", run_async)

        @functools.wraps(func)
        def run(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            result = func(*args, **kwargs)
            emit(result, started)
            return result

        return cast("F", run)

    return decorate


def register_secret_value(value: str) -> None:
    """Scrub this exact value (and its base64 forms) wherever it shows up later."""
    if len(value) < _MIN_KNOWN_SECRET:
        return
    raw = value.encode("utf-8", errors="replace")
    _known_values.update(
        {value, base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode()}
    )


def redact_source_urls(text: str) -> str:
    """Mask every URL in a text (a diagnostic bundle must not name where content comes from,
    EF-EXP-063 / I33); the rest of the text is left as it is."""
    return _SOURCE_URL.sub(REDACTED, text)


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
        # filter_by_level first: a DEBUG call at INFO is dropped before any processor runs
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
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
    for name in _NEVER_BELOW_WARNING:
        logging.getLogger(name).setLevel(logging.WARNING)
    # Huey says each task it runs at INFO: `job.started` / `job.finished` say it with the counts
    logging.getLogger("huey").setLevel(logging.DEBUG if level == "DEBUG" else logging.WARNING)
    for name in _LIBRARY_LOGGERS:  # uvicorn must not keep its own plain-text handlers
        library = logging.getLogger(name)
        library.handlers.clear()
        library.propagate = True
