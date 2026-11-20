"""The plugin, as the host sees it: an object with the contract's methods, each one an RPC call.

``await proxy.search(fetcher, q, filters, cursor)`` sends `source.search` with `q`, `filters` and
`cursor` as JSON (never `fetcher`: the live Fetcher stays here and is served to the plugin, call by
call, by the session) and returns what the SDK declares, as SDK models. Methods the manifest's kind
does not have do not exist, so `hasattr` tells optional capabilities apart as it does in process.
Every method is a coroutine, including the ones the contract writes as plain functions: a call
that crosses a process is waited for.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from shelfhold_sdk.models import PluginManifest

from shelfhold.plugin_host.rpc import protocol
from shelfhold.plugin_host.rpc.host_side import RpcSession


class RpcPluginProxy:
    def __init__(self, session: RpcSession, manifest: PluginManifest) -> None:
        self._session = session
        self.manifest = manifest

    @property
    def session(self) -> RpcSession:
        return self._session

    async def aclose(self) -> None:
        await self._session.close()

    def __getattr__(self, name: str) -> Callable[..., Awaitable[Any]]:
        if name.startswith("_"):
            raise AttributeError(name)
        table = protocol.host_methods(self.manifest.kind)
        spec = next((s for s in table.values() if s.name == name), None)
        if spec is None:
            raise AttributeError(f"{self.manifest.key} has no method {name!r}")

        async def call(*args: Any, **kwargs: Any) -> Any:
            bound = spec.signature.bind(None, *args, **kwargs)  # None stands for `self`
            values = dict(list(bound.arguments.items())[1:])
            fetcher = values.pop("f", None) if spec.takes_fetcher else None
            return await self._session.call(spec.wire, values, fetcher=fetcher)

        return call
