"""structlog configuration: JSON in containers, readable in development (ENF-OBS-001).

structlog renders through the standard library (`ProcessorFormatter` on a root handler), so
uvicorn and every other library also emit one JSON line, with the request context merged in.
"""

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
from urllib.parse import unquote, urlsplit

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from shelfhold.infrastructure.secret_text import (
    SecretIndex,
    contains_secret,
    decoded_views,
    secret_forms,
    sensitive_name,
    tokenlike,
    without_internal_ids,
)

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
MIN_KNOWN_SECRET = 8  # shorter values would blank ordinary words

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
# `/token/<value>`, `/apikey/<value>`...: the segment after a word that names a credential. A path
# segment that is shaped like one (a JWT, a long hexadecimal digest) is masked wherever it sits.
_PATH_KEYWORD = r"(?:api[-_]?key|apikey|key|token|access[-_]?token|secret|auth|bearer|session)"
_PATH_KEY = re.compile(rf"(?P<head>/{_PATH_KEYWORD}/)[^/\s?#]+", re.IGNORECASE)
_TOKEN_SEGMENT = re.compile(
    r"eyJ[\w-]+\.[\w-]+\.[\w-]*|[0-9a-fA-F]{32,}|[A-Za-z0-9_-]{20,}={0,2}(?=[A-Za-z0-9_-]*\d)"
    r"(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*[A-Z])"
)
_URL_IN_TEXT = re.compile(r"""\b[a-z][a-z0-9+.-]*://[^\s'"<>)\]}]+""", re.IGNORECASE)
_SOURCE_URL = re.compile(r"""\b[a-z][a-z0-9+.-]*://[^\s'"<>)\]}]+""", re.IGNORECASE)


class _Registry(set[str]):
    """The registered values. Every change bumps `generation`, which is what the cached views of
    it (`known_secret_values`, `known_secret_index`) are keyed by; a test that empties it with
    `clear()` is seen too."""

    generation = 0

    def update(self, *others: Any) -> None:
        super().update(*others)
        self.generation += 1

    def add(self, element: str) -> None:
        super().add(element)
        self.generation += 1

    def clear(self) -> None:
        super().clear()
        self.generation += 1


_known_values = _Registry()
_views: dict[str, Any] = {"generation": -1, "values": frozenset(), "index": SecretIndex()}


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
    query carried (an API key travels there). The path is scrubbed like any other text, and each
    segment that holds a known secret, or is shaped like one, is masked whole."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "?"
    return _scrub_text(f"{host}{port}{_redact_path(parts.path)}")


def _mask_known(text: str) -> str:
    if not known_secret_index().find(text):  # nearly always: nothing to mask
        return text
    for known in sorted(_known_values, key=len, reverse=True):
        text = text.replace(known, REDACTED)
    return text


def mask_known_secrets(text: str) -> str:
    """Every registered secret value masked in `text`, in any form a URL carries it in, and
    nothing else changed: for text that leaves the process outside a log line (an HTTP problem
    detail, EF-SRC-013)."""
    text = _mask_known(text)
    if "%" in text and _known_values:
        for view in decoded_views(text)[1:]:
            if contains_secret(view, known_secret_index()):
                return _mask_known(view)
    return text


def _redact_path(path: str) -> str:
    """The path of a URL with every segment that holds a credential masked: a registered secret
    (as written, or once or twice percent-encoded), the segment that follows a word naming a
    credential (`/token/<x>`, `/api/opds/<x>`), a JWT, a long digest."""
    segments = path.split("/")
    previous = ""
    for index, segment in enumerate(segments):
        decoded = unquote(segment)
        named = bool(_PATH_KEYWORD_ONLY.fullmatch(previous)) or (
            previous.lower() == "opds" and index >= 2 and segments[index - 2].lower() == "api"
        )
        if segment and (
            contains_secret(segment, known_secret_index())
            or (named and segment != _PLACEHOLDER)
            or _TOKEN_SEGMENT.fullmatch(without_internal_ids(decoded))
            or tokenlike(decoded)
        ):
            segments[index] = REDACTED
        previous = decoded
    return "/".join(segments)


_PATH_KEYWORD_ONLY = re.compile(_PATH_KEYWORD, re.IGNORECASE)
_PLACEHOLDER = "{secret}"  # the template slot of a reference (shelfhold.sdk.refs)


def _redact_pairs(text: str, separators: str = "&;") -> str:
    """`name=value` pairs of a query or a fragment: the value goes when the name is sensitive or
    the value holds a known secret. A piece without `=` is kept unless it holds one."""
    pieces = re.split(f"([{re.escape(separators)}])", text)
    for index, piece in enumerate(pieces):
        if piece in tuple(separators):
            continue
        name, equals, value = piece.partition("=")
        if equals and (
            sensitive_name(name)
            or contains_secret(value, known_secret_index())
            or _TOKEN_SEGMENT.fullmatch(without_internal_ids(unquote(value)))
            or tokenlike(unquote(value))
        ):
            pieces[index] = f"{name}={REDACTED}"
        elif contains_secret(piece, known_secret_index()) or (
            not equals and tokenlike(unquote(piece))  # `#<token>`: a bare fragment
        ):
            pieces[index] = REDACTED
    return "".join(pieces)


def redact_url(url: str) -> str:
    """A URL for a log line or an error: the host and the shape of the path stay, the secrets go.

    The userinfo is masked whole; each path segment that holds a credential is masked (see
    `_redact_path`); in the query and in the fragment, the value of every sensitive parameter and
    of any parameter that holds a registered secret. A relative reference is handled the same way.
    """
    url = _mask_known(url)  # a key holding `/`, `&` or `=` would be cut apart by the split below
    rest, hash_mark, fragment = url.partition("#")
    rest, question, query = rest.partition("?")
    prefix = ""
    scheme_end = rest.find("//")
    if scheme_end != -1 and re.fullmatch(r"(?:[A-Za-z][A-Za-z0-9+.-]*:)?", rest[:scheme_end]):
        after = rest[scheme_end + 2 :]
        authority, slash, tail = after.partition("/")
        host = authority.rpartition("@")[2]
        masked = f"{REDACTED}@{host}" if "@" in authority else authority
        prefix = f"{rest[: scheme_end + 2]}{masked}"
        rest = f"{slash}{tail}"
    elif re.match(r"[^/\s?#]*:[^/\s?#@]*@", rest):  # `user:secret@host/...` without a scheme
        authority, slash, tail = rest.partition("/")
        prefix, rest = f"{REDACTED}@{authority.rpartition('@')[2]}", f"{slash}{tail}"
    out = prefix + _redact_path(rest)
    if question:
        out += "?" + _redact_pairs(query)
    if hash_mark:
        out += "#" + _redact_pairs(fragment)
    return out


def escaped(value: object, *, limit: int = 500) -> str:
    """Free text for a log line: masked first (a known secret must still match whole), then cut,
    then escaped with `repr`. Any other order lets a cut or an escape hide a secret from the
    masking by value (a backslash or a quote changes the text, a cut splits the secret)."""
    return repr(_scrub_text(value if isinstance(value, str) else str(value))[:limit])


def untrusted_name(name: str, *, limit: int = 200) -> str:
    """A name taken from an untrusted archive, for a DEBUG line only: masked, cut, escaped."""
    return escaped(name, limit=limit)


def exception_detail(error: BaseException, *, depth: int = 12) -> dict[str, object]:
    """A failure for a DEBUG line: its class, the frames (module:line in function) and its message
    escaped (`repr`): the message can quote a name from an untrusted archive or a title, and a raw
    traceback would print it as is."""
    frames = [
        f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"
        for frame in traceback.extract_tb(error.__traceback__)[-depth:]
    ]
    return {"error": type(error).__name__, "message": escaped(str(error)), "frames": frames}


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
    """Scrub this exact value (and its base64 forms, and the forms a URL carries it in: percent
    encoded once or twice, `+` for a space) wherever it shows up later."""
    if len(value) < MIN_KNOWN_SECRET:
        return
    _known_values.update(secret_forms(value))


def _refresh() -> None:
    if _views["generation"] != _known_values.generation:
        values = frozenset(_known_values)
        _views.update(generation=_known_values.generation, values=values, index=SecretIndex(values))


def known_secret_values() -> frozenset[str]:
    """Every value registered so far, with its encoded forms: what the guards check references,
    snapshots and database writes against (EF-SRC-035)."""
    _refresh()
    values: frozenset[str] = _views["values"]
    return values


def known_secret_index() -> SecretIndex:
    """The same values, ready to be looked for in a text in one pass."""
    _refresh()
    index: SecretIndex = _views["index"]
    return index


def redact_source_urls(text: str, replace: Callable[[str], str] | None = None) -> str:
    """Mask every URL in a text (a diagnostic bundle must not name where content comes from,
    EF-EXP-063 / I33, ENF-LEG-009); the rest of the text is left as it is. `replace` gives the
    stand-in of a URL (the bundle's `<source-N>`), `***` without it."""
    return _SOURCE_URL.sub(lambda m: REDACTED if replace is None else replace(m.group()), text)


def _scrub_text(text: str) -> str:
    text = _mask_known(text)
    if "%" in text and _known_values:
        # a secret written in a form nobody registered (one character in percent form, the rest as
        # is) shows once decoded; the line is then written decoded and masked
        for view in decoded_views(text)[1:]:
            if contains_secret(view, known_secret_index()):
                text = _mask_known(view)
                break
    text = _URL_IN_TEXT.sub(lambda m: redact_url(m.group()), text)
    text = _URL_USERINFO.sub(lambda m: m.group("head") + REDACTED + m.group("tail"), text)
    text = _OPDS_KEY.sub(lambda m: m.group("head") + REDACTED, text)
    text = _PATH_KEY.sub(lambda m: m.group("head") + REDACTED, text)
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


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _escape_controls(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Console mode writes text as it is: a newline would forge a line and an ANSI sequence would
    drive the terminal. Control characters of every text value become their written form."""
    for key, value in event_dict.items():
        if isinstance(value, str):
            event_dict[key] = _CONTROL.sub(
                lambda m: m.group().encode("unicode_escape").decode("ascii"), value
            )
    return event_dict


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
                *([_escape_controls] if log_format == "console" else []),
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
