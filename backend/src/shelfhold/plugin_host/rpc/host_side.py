"""The core's end of `rpc_protocol` 1: one session per isolated plugin (D12, EF-SRC-016).

The core calls, the plugin answers. A plugin may call back four methods only (`fetcher.get|post|
download`, `events.publish`), and only while a host call is open: the request names that call, and
is served with *that call's* Fetcher, so what a plugin fetches always goes through the guard the
host built for the operation (SSRF check, the manifest's allow-list, rate limits, log redaction);
secrets are injected by that Fetcher, on this side, and never travel. Anything else the plugin
sends, a frame that does not parse, a request outside a call, ends the session.

A plugin that crashes, exits, outlives a deadline, runs over a limit or breaks the protocol is
marked in error with the reason (`on_failure`); the other plugins and the host go on.
"""

import asyncio
import contextlib
import itertools
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from pydantic import ValidationError
from shelfhold_sdk.events import PluginEventError, plugin_event_type
from shelfhold_sdk.models import PluginKind

from shelfhold.infrastructure.fetcher.errors import FetchError
from shelfhold.infrastructure.logging import known_secret_values, redact_value
from shelfhold.plugin_host.rpc import codec, protocol
from shelfhold.plugin_host.rpc.protocol import (
    Channel,
    DeadlineExceeded,
    PluginFailed,
    RemoteError,
    RpcError,
    RpcLimits,
)

__all__ = ["EventSink", "RpcSession"]

log = structlog.get_logger(__name__)

EventSink = Callable[[str, str, Mapping[str, Any] | None], Awaitable[object] | object]
State = Literal["ready", "error", "closed"]


@dataclass
class _Call:
    fetcher: Any
    future: "asyncio.Future[Any]"


def _secret_in(text: str) -> bool:
    return any(secret in text for secret in known_secret_values())


def _scrubbed(text: object) -> str:
    cleaned = redact_value(str(text))
    return (cleaned if isinstance(cleaned, str) else "")[:500]


class RpcSession:
    def __init__(
        self, channel: Channel, *, key: str, limits: RpcLimits, on_event: EventSink | None
    ) -> None:
        self.key = key
        self.limits = limits
        self.protocol: int = 0
        self.manifest: dict[str, Any] = {}
        self.state: State = "ready"
        self.reason: str | None = None
        self.on_failure: Callable[[str], object] | None = None
        self._channel = channel
        self._on_event = on_event
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._calls: dict[int, _Call] = {}  # the host calls that are open
        self._slots = asyncio.Semaphore(limits.max_in_flight)
        self._handlers: set[asyncio.Task[None]] = set()
        self._kind: PluginKind | None = None
        self._reader_task: asyncio.Task[None] | None = None

    # -- opening ---------------------------------------------------------------------------

    @classmethod
    async def open(
        cls, channel: Channel, *, key: str, limits: RpcLimits, on_event: EventSink | None = None
    ) -> "RpcSession":
        """Agree on `rpc_protocol` (`hello`), and only then ask for the manifest. A plugin that
        speaks no version of ours is refused here, its manifest never read."""
        session = cls(channel, key=key, limits=limits, on_event=on_event)
        session._reader_task = asyncio.ensure_future(session._read_loop())
        try:
            ours = {"rpc_protocol": list(protocol.RPC_PROTOCOL_VERSIONS)}
            hello = await session._request("hello", ours, limits.call_deadline)
            chosen = hello.get("rpc_protocol") if isinstance(hello, dict) else None
            if (
                not isinstance(chosen, int)
                or isinstance(chosen, bool)
                or chosen not in protocol.RPC_PROTOCOL_VERSIONS
            ):
                raise protocol.ProtocolOutOfRange(chosen, protocol.RPC_PROTOCOL_VERSIONS)
            session.protocol = chosen
            manifest = await session._request("manifest", {}, limits.call_deadline)
        except RemoteError as exc:
            await session._shutdown("closed")
            if exc.code == protocol.NO_COMMON_PROTOCOL:
                offered = exc.data.get("rpc_protocol") if isinstance(exc.data, dict) else None
                raise protocol.ProtocolOutOfRange(offered, protocol.RPC_PROTOCOL_VERSIONS) from exc
            raise
        except protocol.ProtocolOutOfRange:
            await session._shutdown("closed")
            raise
        except BaseException:
            await session._shutdown("closed")
            raise
        if not isinstance(manifest, dict):
            await session._shutdown("closed")
            raise RpcError("the plugin's manifest is not an object")
        session.manifest = manifest
        with contextlib.suppress(ValueError):
            session._kind = PluginKind(str(manifest.get("kind")))
        return session

    # -- calling the plugin ----------------------------------------------------------------

    async def call(
        self,
        wire: str,
        args: Mapping[str, Any],
        *,
        fetcher: Any,
        deadline: float | None = None,
    ) -> Any:
        """Call a method of the plugin's contract. `fetcher` is the host's Fetcher for this
        operation: the plugin reaches it, and nothing else of the host, until the call returns."""
        if self._kind is None or wire not in protocol.host_methods(self._kind):
            raise RpcError(f"{wire} is not a method of this plugin's contract")
        spec = protocol.host_methods(self._kind)[wire]
        self._ensure_ready()
        context: dict[str, Any] = {}
        if fetcher is not None:
            settings = getattr(fetcher, "settings", None)
            cookies = getattr(fetcher, "cookies", None)
            context = {
                "settings": codec.to_wire(settings()) if callable(settings) else {},
                "cookies": codec.to_wire(cookies()) if callable(cookies) else {},
            }
        params = {"args": codec.to_wire(dict(args)), "context": context}
        limit = deadline if deadline is not None else protocol.deadline_of(wire, self.limits)
        async with self._slots:
            self._ensure_ready()
            result = await self._request(wire, params, limit, fetcher=fetcher)
        try:
            return spec.result.validate_json(json.dumps(result))
        except ValidationError as exc:
            raise RemoteError(protocol.INVALID_RESULT, f"{wire}: result is not valid") from exc

    def _ensure_ready(self) -> None:
        if self.state != "ready":
            raise PluginFailed(self.reason or "the plugin session is closed")

    async def _request(
        self, method: str, params: dict[str, Any], deadline: float, *, fetcher: Any = None
    ) -> Any:
        number = next(self._ids)
        frame = codec.encode_frame(
            {"jsonrpc": protocol.JSONRPC, "id": number, "method": method, "params": params}
        )
        if _secret_in(frame.decode("utf-8", "replace")):
            raise protocol.SecretLeak(f"{method}: a message would carry a secret; not sent")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[number] = future
        self._calls[number] = _Call(fetcher, future)
        try:
            self._channel.writer.write(frame)
            await self._channel.writer.drain()
            async with asyncio.timeout(deadline):
                return await future
        except TimeoutError:
            reason = f"deadline of {deadline:g}s exceeded by {method}"
            await self._fail(reason)
            raise DeadlineExceeded(reason) from None
        except (ConnectionError, OSError) as exc:
            await self._fail(f"the plugin's connection broke: {type(exc).__name__}")
            raise PluginFailed(self.reason or "the plugin is gone") from exc
        finally:
            self._pending.pop(number, None)
            self._calls.pop(number, None)  # the call is over: its number opens nothing now

    # -- what comes back -------------------------------------------------------------------

    async def _read_loop(self) -> None:
        try:
            while True:
                payload = await codec.read_frame(self._channel.reader)
                if payload is None:
                    exit_reason = await self._channel.exit_reason()
                    await self._fail(exit_reason or "the plugin closed the connection")
                    return
                await self._dispatch(codec.decode_payload(payload))
        except codec.FrameError as exc:
            await self._fail(f"invalid frame: {exc}")
        except (ConnectionError, OSError) as exc:
            await self._fail(await self._channel.exit_reason() or f"connection lost: {exc}")
        except asyncio.CancelledError:
            raise

    async def _dispatch(self, message: Any) -> None:
        if not isinstance(message, dict) or message.get("jsonrpc") != protocol.JSONRPC:
            raise codec.FrameError("not a JSON-RPC 2.0 message")
        if "method" in message:
            await self._plugin_request(message)
            return
        number = message.get("id")
        pending = self._pending.get(number) if isinstance(number, int) else None
        if pending is None or pending.done():
            raise codec.FrameError("a response to a request that is not open")
        if "error" in message and isinstance(message["error"], dict):
            error = message["error"]
            pending.set_exception(
                protocol.exception_from_wire(error)
                if error.get("code") == protocol.PLUGIN_RAISED
                else RemoteError(
                    int(error.get("code", protocol.INVALID_REQUEST)),
                    str(error.get("message", "")),
                    error.get("data"),
                )
            )
        elif "result" in message:
            pending.set_result(message["result"])
        else:
            raise codec.FrameError("a response with neither result nor error")

    async def _plugin_request(self, message: dict[str, Any]) -> None:
        number, method, params = message.get("id"), message.get("method"), message.get("params")
        if not isinstance(number, int) or isinstance(number, bool):
            raise codec.FrameError("a request from the plugin has an integer id")
        if method not in protocol.PLUGIN_TO_HOST_METHODS:
            await self._answer_error(number, protocol.METHOD_NOT_FOUND, "not a method of the host")
            await self._fail(f"the plugin called {str(method)[:60]!r}, which is not a host method")
            return
        named = params.get("call") if isinstance(params, dict) else None
        call = self._calls.get(named) if isinstance(named, int) else None
        if not isinstance(params, dict) or call is None or call.future.done():
            await self._answer_error(
                number,
                protocol.OUTSIDE_HOST_CALL,
                "a plugin calls the host during a host call only",
            )
            await self._fail(f"the plugin called {method} outside a host call")
            return
        if len(self._handlers) >= self.limits.max_plugin_requests:
            await self._answer_error(number, protocol.HOST_REFUSED, "too many requests at once")
            await self._fail("the plugin sent too many requests at once")
            return
        task = asyncio.ensure_future(self._serve(number, str(method), params, call))
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _serve(self, number: int, method: str, params: dict[str, Any], call: _Call) -> None:
        try:
            model = protocol.PLUGIN_REQUESTS[method].model_validate(params)
        except ValidationError:
            await self._answer_error(number, protocol.INVALID_PARAMS, f"{method}: invalid params")
            return
        try:
            result = await self._run(method, model, call)
        except FetchError as exc:  # the guard's refusal: named, scrubbed, never hidden
            await self._answer_error(
                number, protocol.HOST_REFUSED, _scrubbed(exc), {"kind": type(exc).__name__}
            )
            return
        except (PluginEventError, ValueError) as exc:
            await self._answer_error(number, protocol.INVALID_PARAMS, _scrubbed(exc))
            return
        except _NoFetcher:
            await self._answer_error(
                number,
                protocol.HOST_REFUSED,
                "this call was given no Fetcher",
                {"kind": "NoFetcher"},
            )
            return
        except Exception as exc:  # the host's own failure: said without detail
            log.warning("plugin.rpc.host_error", plugin_id=self.key, error=type(exc).__name__)
            await self._answer_error(
                number, protocol.HOST_REFUSED, "the host could not do it", {"kind": "HostError"}
            )
            return
        try:
            await self._write({"jsonrpc": protocol.JSONRPC, "id": number, "result": result})
        except codec.FrameTooLarge:
            await self._answer_error(
                number,
                protocol.RESULT_TOO_LARGE,
                "the answer does not fit in a message",
                {"kind": "FetchTooLarge"},
            )

    async def _run(self, method: str, model: Any, call: _Call) -> Any:
        if method == "events.publish":
            if self._on_event is None:
                raise ValueError("events are not wired for this plugin")
            plugin_event_type(self.key, model.name)  # the host builds the type: own space only
            done = self._on_event(self.key, model.name, model.payload)
            if isinstance(done, Awaitable):
                await done
            return None
        if call.fetcher is None:
            raise _NoFetcher
        fetcher = call.fetcher
        if method == "fetcher.get":
            got = await fetcher.get(
                model.url,
                headers=model.headers,
                params=model.params,
                referer=model.referer,
                cache_ttl=model.cache_ttl,
            )
        elif method == "fetcher.post":
            got = await fetcher.post(
                model.url, data=model.data, json=model.json_body, headers=model.headers
            )
        else:
            got = await fetcher.download(model.req)
        wired = codec.to_wire(got)
        if isinstance(wired.get("headers"), dict):
            wired["headers"] = {
                k: v
                for k, v in wired["headers"].items()
                if k.lower() not in protocol.WITHHELD_HEADERS
            }
        return wired

    async def _answer_error(self, number: int, code: int, message: str, data: Any = None) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        with contextlib.suppress(ConnectionError, OSError, codec.FrameError):
            await self._write({"jsonrpc": protocol.JSONRPC, "id": number, "error": error})

    async def _write(self, message: dict[str, Any]) -> None:
        frame = codec.encode_frame(message)
        if _secret_in(frame.decode("utf-8", "replace")):
            raise protocol.SecretLeak("an answer would carry a secret; not sent")
        self._channel.writer.write(frame)
        await self._channel.writer.drain()

    # -- the end ---------------------------------------------------------------------------

    async def _fail(self, reason: str) -> None:
        """Mark the plugin in error and stop it. Idempotent; the host is untouched."""
        if self.state != "ready":
            return
        self.state, self.reason = "error", reason
        log.warning("plugin.rpc.failed", plugin_id=self.key, reason=_scrubbed(reason))
        await self._shutdown("error")
        if self.on_failure is not None:
            self.on_failure(reason)

    async def _shutdown(self, state: State) -> None:
        if self.state == "ready":
            self.state = state
        failure = PluginFailed(self.reason or "the plugin session is closed")
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(failure)
        current = asyncio.current_task()
        for task in [*self._handlers, self._reader_task]:
            if task is not None and task is not current and not task.done():
                task.cancel()
        with contextlib.suppress(Exception):
            await self._channel.close()

    async def close(self) -> None:
        """End the session on the host's side: ask the plugin to stop, then stop it."""
        if self.state == "ready":
            with contextlib.suppress(Exception):
                await self._request("shutdown", {}, 2.0)
        await self._shutdown("closed")


class _NoFetcher(Exception):
    pass
