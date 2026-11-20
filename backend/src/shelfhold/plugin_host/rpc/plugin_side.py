"""The plugin's end of `rpc_protocol` 1, run inside the plugin's own subprocess (D12).

``python -m shelfhold.plugin_host.rpc.plugin_side --entry module:attr [--path DIR]`` loads the
plugin and serves the host over stdin and stdout, in length-prefixed frames. Standard output belongs
to the protocol from the first instruction: what a plugin prints is sent to standard error, so a
stray ``print`` cannot forge a frame.

Imports the SDK, `plugin_host.rpc` and the standard library only (the runner's import contract):
nothing of the core is reachable from here.
"""

import argparse
import asyncio
import contextlib
import contextvars
import importlib
import inspect
import json
import operator
import os
import sys
import traceback
from collections.abc import Mapping
from typing import Any

from shelfhold_sdk.fetcher import CookieJarView, DownloadedFile, Response
from shelfhold_sdk.refs import ImageRequest

from shelfhold.plugin_host.rpc import codec, protocol
from shelfhold.plugin_host.rpc.protocol import MethodSpec, RpcError

_CALL: contextvars.ContextVar[int | None] = contextvars.ContextVar("rpc_call", default=None)
_CONTEXT: contextvars.ContextVar[Mapping[str, Any] | None] = contextvars.ContextVar(
    "rpc_context", default=None
)


class HostRefused(RpcError):
    """The host refused or failed a request of the plugin (the guard said no, the answer was too
    large...). `kind` is the name of the host's refusal, for a plugin to tell them apart."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class _Wire:
    """Requests of the plugin to the host, answered by the read loop."""

    def __init__(self, writer: Any) -> None:
        self._writer = writer
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next = 1
        self._lock = asyncio.Lock()

    def settle(self, message: dict[str, Any]) -> bool:
        number = message.get("id")
        future = self._pending.get(number) if isinstance(number, int) else None
        if future is None or future.done():
            return False
        if isinstance(message.get("error"), dict):
            error = message["error"]
            data = error.get("data")
            kind = data.get("kind") if isinstance(data, dict) else None
            refused = HostRefused(str(kind or error.get("code")), str(error.get("message")))
            future.set_exception(refused)
        else:
            future.set_result(message.get("result"))
        return True

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        call = _CALL.get()
        if call is None:  # outside a host call there is nobody to ask
            raise HostRefused("OutsideHostCall", "the host can be called during a host call only")
        number, self._next = self._next, self._next + 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[number] = future
        try:
            await self.send(
                {
                    "jsonrpc": protocol.JSONRPC,
                    "id": number,
                    "method": method,
                    "params": {"call": call, **params},
                }
            )
            return await future
        finally:
            self._pending.pop(number, None)

    async def send(self, message: dict[str, Any]) -> None:
        async with self._lock:
            await codec.write_frame(self._writer, message)

    def fail_all(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)


class RpcFetcher:
    """The `Fetcher` the plugin is handed: every call is a request to the host, which does it (and
    injects whatever credential the source needs) on its side."""

    def __init__(self, wire: _Wire) -> None:
        self._wire = wire

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        referer: str | None = None,
        cache_ttl: int | None = None,
    ) -> Response:
        got = await self._wire.request(
            "fetcher.get",
            {
                "url": url,
                "headers": headers,
                "params": params,
                "referer": referer,
                "cache_ttl": cache_ttl,
            },
        )
        return Response.model_validate_json(_dumps(got))

    async def post(
        self,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Response:
        got = await self._wire.request(
            "fetcher.post",
            {"url": url, "data": data, "json_body": json, "headers": headers},
        )
        return Response.model_validate_json(_dumps(got))

    async def download(self, req: ImageRequest) -> DownloadedFile:
        got = await self._wire.request("fetcher.download", {"req": codec.to_wire(req)})
        return DownloadedFile.model_validate_json(_dumps(got))

    def cookies(self) -> CookieJarView:
        return CookieJarView.model_validate((_CONTEXT.get() or {}).get("cookies") or {})

    def settings(self) -> dict[str, Any]:
        value = (_CONTEXT.get() or {}).get("settings")
        return dict(value) if isinstance(value, dict) else {}


def _dumps(value: Any) -> str:
    return json.dumps(value)


_WIRE: _Wire | None = None


async def publish_event(name: str, payload: Mapping[str, Any] | None = None) -> None:
    """Publish `plugin.<key>.<name>` through the host (never another type: the host names it)."""
    if _WIRE is None:
        raise HostRefused("NotServing", "no host to publish to")
    await _WIRE.request("events.publish", {"name": name, "payload": codec.to_wire(payload)})


# -- serving ----------------------------------------------------------------------------------


def _kind_table(instance: Any) -> Mapping[str, MethodSpec]:
    return protocol.host_methods(instance.manifest.kind, protocol.OPTIONAL_METHODS)


async def _call_method(instance: Any, spec: MethodSpec, params: Any, fetcher: RpcFetcher) -> Any:
    if not isinstance(params, dict) or not isinstance(params.get("args"), dict):
        raise _BadParams("params is {args, context}")
    args: dict[str, Any] = params["args"]
    unknown = set(args) - {p.name for p in spec.params}
    if unknown:
        raise _BadParams(f"unknown argument(s): {sorted(unknown)}")
    positional: list[Any] = [fetcher] if spec.takes_fetcher else []
    keywords: dict[str, Any] = {}
    for p in spec.params:
        if p.name not in args:
            if p.required:
                raise _BadParams(f"missing argument {p.name!r}")
            continue
        try:
            value = p.adapter.validate_json(json.dumps(args[p.name]))
        except ValueError as exc:
            raise _BadParams(f"argument {p.name!r} is not valid: {type(exc).__name__}") from exc
        keywords[p.name] = value  # by the names the SDK contract gives them
    outcome = operator.attrgetter(spec.name)(instance)(*positional, **keywords)
    if inspect.isawaitable(outcome):
        outcome = await outcome
    if outcome is None:
        return None
    return spec.result.dump_python(outcome, mode="json", warnings=False)


def _error_message(number: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": protocol.JSONRPC,
        "id": number,
        "error": {"code": code, "message": message},
    }


class _BadParams(Exception):
    pass


async def serve(instance: Any, reader: asyncio.StreamReader, writer: Any) -> None:
    """Serve one host on (`reader`, `writer`) until it closes or says `shutdown`."""
    global _WIRE
    wire = _Wire(writer)
    _WIRE = wire
    fetcher = RpcFetcher(wire)
    table = _kind_table(instance)
    greeted = False
    tasks: set[asyncio.Task[None]] = set()

    async def answer(
        number: Any, *, result: Any = None, error: dict[str, Any] | None = None
    ) -> None:
        message: dict[str, Any] = {"jsonrpc": protocol.JSONRPC, "id": number}
        message.update({"error": error} if error else {"result": result})
        try:
            await wire.send(message)
        except codec.FrameTooLarge:
            too_large = "the result is larger than a message"
            await wire.send(_error_message(number, protocol.RESULT_TOO_LARGE, too_large))
        except codec.LiveObjectError as exc:
            await wire.send(_error_message(number, protocol.INVALID_RESULT, str(exc)))

    async def handle(request: dict[str, Any]) -> None:
        number, method, params = request["id"], request["method"], request.get("params")
        spec = table.get(method)
        if spec is None or not hasattr(instance, spec.name):  # an optional one it does not have
            unknown = {"code": protocol.METHOD_NOT_FOUND, "message": f"{method}: unknown"}
            await answer(number, error=unknown)
            return
        ctx = params.get("context") if isinstance(params, dict) else None
        _CALL.set(number)
        _CONTEXT.set(ctx if isinstance(ctx, dict) else {})
        try:
            result = await _call_method(instance, spec, params, fetcher)
            await answer(number, result=codec.to_wire(result))
        except _BadParams as exc:
            await answer(number, error={"code": protocol.INVALID_PARAMS, "message": str(exc)})
        except codec.LiveObjectError as exc:
            await answer(number, error={"code": protocol.INVALID_RESULT, "message": str(exc)})
        except Exception as exc:  # the plugin's own failure goes back to the host as an error
            print(traceback.format_exc(limit=4), file=sys.stderr, flush=True)
            await answer(number, error=protocol.exception_to_wire(exc))

    try:
        while True:
            payload = await codec.read_frame(reader)
            if payload is None:
                return
            message = codec.decode_payload(payload)
            if not isinstance(message, dict) or message.get("jsonrpc") != protocol.JSONRPC:
                raise codec.FrameError("not a JSON-RPC 2.0 message")
            if "method" not in message:  # an answer to one of our requests
                if not wire.settle(message):
                    raise codec.FrameError("an answer to a request that is not open")
                continue
            number, method = message.get("id"), message["method"]
            if not isinstance(number, int):
                raise codec.FrameError("a request has an integer id")
            if method == "hello":
                offered = (message.get("params") or {}).get("rpc_protocol")
                ours = set(protocol.RPC_PROTOCOL_VERSIONS)
                common = sorted(ours & set(offered)) if isinstance(offered, list) else []
                if not common:
                    await answer(
                        number,
                        error={
                            "code": protocol.NO_COMMON_PROTOCOL,
                            "message": "no rpc_protocol in common",
                            "data": {"rpc_protocol": list(protocol.RPC_PROTOCOL_VERSIONS)},
                        },
                    )
                    return
                greeted = True
                await answer(number, result={"rpc_protocol": common[-1]})
            elif not greeted:  # nothing before the handshake, and the manifest least of all
                raise codec.FrameError("a request before hello")
            elif method == "manifest":
                await answer(number, result=codec.to_wire(instance.manifest))
            elif method == "shutdown":
                await answer(number, result=None)
                return
            else:
                task = asyncio.ensure_future(handle(message))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
    except (codec.FrameError, ConnectionError):
        return
    finally:
        wire.fail_all(RpcError("the host closed the connection"))
        for task in tasks:
            task.cancel()
        with contextlib.suppress(Exception):
            writer.close()  # the host sees the end of the stream, whatever ended the session


def _load(entry: str, path: str | None) -> Any:
    if path:
        sys.path.insert(0, path)
    module_name, _, attr = entry.partition(":")
    factory = getattr(importlib.import_module(module_name), attr)
    return factory()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--entry", required=True)
    parser.add_argument("--path")
    args = parser.parse_args(argv)
    # Standard output is the protocol's: whatever the plugin prints goes to standard error.
    real_stdout_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    try:
        instance = _load(args.entry, args.path)
    except Exception:
        traceback.print_exc()
        return 2

    async def run() -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=2**16)
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(0, "rb", 0)
        )
        transport, proto = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, os.fdopen(real_stdout_fd, "wb", 0)
        )
        await serve(instance, reader, asyncio.StreamWriter(transport, proto, None, loop))

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
