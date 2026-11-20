"""What both ends of `rpc_protocol` 1 agree on: errors, limits, and the methods (D12).

Host to plugin, the methods of the contracts the SDK defines for each kind of plugin (`Source`,
`ExportTarget`, `MetadataProvider`; `Tracker` has none until its contract exists) under the name
`<kind>.<method>`, plus the session's own `hello`, `manifest` and `shutdown`. Plugin to host, four
methods and no more, each only while a host call is under way and bound to it by its number.

The parameter and result types of a method are read from the SDK's `Protocol` classes, so that the
wire cannot drift from the contract. Imports the SDK, pydantic and the standard library only.
"""

import asyncio
import inspect
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import cache
from types import MappingProxyType
from typing import Any, Final

from pydantic import TypeAdapter
from shelfhold_sdk.export_target import ExportTarget, MetadataUpdater
from shelfhold_sdk.fetcher import DownloadedFile, Response
from shelfhold_sdk.metadata import MetadataProvider, ProviderUnavailable
from shelfhold_sdk.models import RPC_PROTOCOL_VERSIONS, PluginKind, StrictSdkModel
from shelfhold_sdk.refs import ImageRequest
from shelfhold_sdk.source import (
    AuthRequired,
    ContentRemoved,
    NotConfigured,
    RateLimited,
    Source,
    SourceChanged,
    TemporarilyUnavailable,
)

__all__ = [
    "PLUGIN_TO_HOST_METHODS",
    "RPC_PROTOCOL_VERSIONS",
    "Channel",
    "MethodSpec",
    "RpcLimits",
    "host_methods",
]

JSONRPC: Final = "2.0"

# JSON-RPC 2.0 codes, then ours (-32000 to -32099 are left to the application by the spec).
PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
PLUGIN_RAISED: Final = -32000  # the plugin's own exception, named in `data.type`
OUTSIDE_HOST_CALL: Final = -32001
NO_COMMON_PROTOCOL: Final = -32010
HOST_REFUSED: Final = -32020  # the host refused or failed what the plugin asked (Fetcher...)
RESULT_TOO_LARGE: Final = -32022
INVALID_RESULT: Final = -32023

PLUGIN_TO_HOST_METHODS: Final = frozenset(
    {"fetcher.get", "fetcher.post", "fetcher.download", "events.publish"}
)
SESSION_METHODS: Final = frozenset({"hello", "manifest", "shutdown"})


class RpcError(Exception):
    """A call that failed; the plugin itself may be fine."""


class RemoteError(RpcError):
    """The other end answered a request with an error."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class PluginFailed(RpcError):
    """The plugin is out: it crashed, exited, ran over a limit, timed out or broke the protocol.
    The host goes on; the plugin is marked in error with this reason."""


class DeadlineExceeded(PluginFailed):
    """A call outlived its deadline: the plugin is stopped and marked in error."""


class ProtocolOutOfRange(RpcError):
    """No `rpc_protocol` in common: refused before the manifest is read."""

    def __init__(self, offered: object, supported: tuple[int, ...]) -> None:
        super().__init__(f"rpc_protocol {offered!r} is outside what this host speaks: {supported}")
        self.offered, self.supported = offered, supported


class SecretLeak(RpcError):
    """A message was about to carry a value the host holds as a secret: it is not sent."""


@dataclass(frozen=True)
class RpcLimits:
    """The bounds of one plugin (D12). Defaults are the ADR's."""

    call_deadline: float = 30.0
    download_deadline: float = 300.0
    max_in_flight: int = 4  # host calls at once, per plugin
    max_plugin_requests: int = 8  # plugin requests at once, over every call
    memory_bytes: int = 512 * 1024 * 1024  # RLIMIT_AS of the plugin's process
    cpu_seconds: int = 600  # RLIMIT_CPU: the process's total, a backstop to the wall deadline


@dataclass
class Channel:
    """One connection to a plugin: its two ends, and how to close it and to say why it ended."""

    reader: asyncio.StreamReader
    writer: Any  # write(bytes), drain(), close(): an `asyncio.StreamWriter`
    close: Callable[[], Awaitable[None]]
    exit_reason: Callable[[], Awaitable[str | None]]


# -- the methods of the contracts ----------------------------------------------------------------


@dataclass(frozen=True)
class Param:
    name: str
    adapter: TypeAdapter[Any]
    keyword_only: bool
    required: bool


@dataclass(frozen=True)
class MethodSpec:
    wire: str  # `source.search`
    name: str  # `search`
    params: tuple[Param, ...]
    result: TypeAdapter[Any]
    takes_fetcher: bool  # the contract's `f`: never a parameter, the host serves it by RPC
    signature: inspect.Signature


_SOURCE: Final = [
    "filters", "sub_sources", "search", "resolve_url", "get_series", "list_units", "fetch_unit",
    "fetch_range", "resolve_page", "login", "refresh", "migrate_ref", "migrate_memo",
]  # fmt: skip
_EXPORT: Final = [
    "config_schema", "default_template", "render_sidecars", "inventory_calls", "scan_calls",
    "interpret", "supported_versions",
]  # fmt: skip
_METADATA: Final = ["identify", "get_cover", "url_for", "ids_from_url", "login", "refresh"]
_LONG: Final = frozenset({"source.fetch_unit", "source.fetch_range", "source.resolve_page"})

_KINDS: Final[dict[PluginKind, tuple[str, type, list[str]]]] = {
    PluginKind.SOURCE: ("source", Source, _SOURCE),
    PluginKind.EXPORT_TARGET: ("export_target", ExportTarget, _EXPORT),
    PluginKind.METADATA: ("metadata", MetadataProvider, _METADATA),
}


def _spec(prefix: str, contract: type, name: str) -> MethodSpec:
    method = getattr(contract, name)
    signature = inspect.signature(method)
    hints = typing.get_type_hints(method)
    params: list[Param] = []
    takes_fetcher = False
    for pname, p in list(signature.parameters.items())[1:]:  # past `self`
        if pname == "f":
            takes_fetcher = True
            continue
        params.append(
            Param(
                pname,
                TypeAdapter(hints.get(pname, Any)),
                p.kind is inspect.Parameter.KEYWORD_ONLY,
                p.default is inspect.Parameter.empty,
            )
        )
    wire = f"{prefix}.{name}"
    return MethodSpec(
        wire, name, tuple(params), TypeAdapter(hints.get("return", Any)), takes_fetcher, signature
    )


@cache
def _table() -> dict[PluginKind, dict[str, MethodSpec]]:
    table: dict[PluginKind, dict[str, MethodSpec]] = {kind: {} for kind in PluginKind}
    for kind, (prefix, contract, names) in _KINDS.items():
        for name in names:
            table[kind][f"{prefix}.{name}"] = _spec(prefix, contract, name)
    update = _spec("export_target", MetadataUpdater, "update_metadata")  # the optional capability
    table[PluginKind.EXPORT_TARGET][update.wire] = update
    return table


def host_methods(kind: PluginKind) -> MappingProxyType[str, MethodSpec]:
    """The methods the host may call on a plugin of this kind. `Tracker`: none yet (L13)."""
    return MappingProxyType(_table()[kind])


def deadline_of(wire: str, limits: RpcLimits) -> float:
    return limits.download_deadline if wire in _LONG else limits.call_deadline


# -- what a plugin may ask of the host -----------------------------------------------------------
#
# Every request carries `call`, the number of the host call it serves; the rest is checked against
# these models, which forbid unknown fields: nothing is passed on that was not described.


class GetParams(StrictSdkModel):
    call: int
    url: str
    headers: dict[str, str] | None = None
    params: dict[str, Any] | None = None
    referer: str | None = None
    cache_ttl: int | None = None


class PostParams(StrictSdkModel):
    call: int
    url: str
    data: dict[str, Any] | None = None
    json_body: Any = None  # `json` on the Fetcher; renamed here, not to shadow the module
    headers: dict[str, str] | None = None


class DownloadParams(StrictSdkModel):
    call: int
    req: ImageRequest


class PublishParams(StrictSdkModel):
    call: int
    name: str
    payload: dict[str, Any] | None = None


PLUGIN_REQUESTS: Final[dict[str, type[StrictSdkModel]]] = {
    "fetcher.get": GetParams,
    "fetcher.post": PostParams,
    "fetcher.download": DownloadParams,
    "events.publish": PublishParams,
}
FETCHER_RESULTS: Final[dict[str, type]] = {
    "fetcher.get": Response,
    "fetcher.post": Response,
    "fetcher.download": DownloadedFile,
}

# The headers of an answer that the host keeps to itself: the plugin never learns a session.
WITHHELD_HEADERS: Final = frozenset(
    {
        "set-cookie",
        "set-cookie2",
        "authorization",
        "proxy-authorization",
        "www-authenticate",
        "proxy-authenticate",
    }
)

# -- exceptions across the wire ------------------------------------------------------------------

_SDK_ERRORS: Final[dict[str, type[Exception]]] = {
    cls.__name__: cls
    for cls in (
        AuthRequired,
        ContentRemoved,
        NotConfigured,
        RateLimited,
        SourceChanged,
        TemporarilyUnavailable,
        ProviderUnavailable,
    )
}


def exception_to_wire(exc: BaseException) -> dict[str, Any]:
    """The error object of a plugin's exception: its class name (SDK errors are rebuilt on the
    other side), its message and, for a rate limit, the delay."""
    data: dict[str, Any] = {"type": type(exc).__name__}
    if isinstance(exc, RateLimited):
        data["retry_after"] = exc.retry_after
    message = getattr(exc, "reason", None) if isinstance(exc, ProviderUnavailable) else None
    return {"code": PLUGIN_RAISED, "message": str(message or exc)[:2000], "data": data}


def exception_from_wire(error: dict[str, Any]) -> Exception:
    """An SDK exception again where the plugin raised one, else a ``RemoteError``."""
    code, message, data = error.get("code"), str(error.get("message", "")), error.get("data")
    if code == PLUGIN_RAISED and isinstance(data, dict):
        cls = _SDK_ERRORS.get(str(data.get("type")))
        if cls is RateLimited:
            retry = data.get("retry_after")
            return RateLimited(message, retry if isinstance(retry, int | float) else None)
        if cls is not None:
            return cls(message)
    return RemoteError(code if isinstance(code, int) else INVALID_REQUEST, message, data)
