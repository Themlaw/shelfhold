"""The fake servers of the sources, put together once (M2 L31).

Every bench of a source (the native books, Gutenberg, Wikisource, Standard Ebooks, OPDS) needs the
same chain: a wire that answers the requests of a recorder, a network that resolves its hosts to a
public address and sends through that recorder, a registry that holds the plugin, the Fetchers of
the sources and the acquirer over the library. This module builds that chain; a bench brings its
wire, its hosts, its plugin and its settings, and keeps what is its own (the shape of its `Bench`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from shelfhold.acquisition.public import NativeAcquirer
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.fetcher import Network
from shelfhold.plugin_host.registry import PluginCandidate, PluginOrigin, PluginRegistry
from shelfhold.plugin_host.source_fetcher import SourceFetchers
from tests.fakes_fetcher import FakeResolver, Recorder
from tests.library_env import LibEnv
from tests.null_tombstones import NoCatalogHits

PUBLIC = "93.184.216.34"  # an address that is not private: the guard lets it through


class Wire(Protocol):
    def respond(self, request: Any) -> Any: ...


def no_work_hits() -> NoCatalogHits:
    """The Work tombstones of a bench that purged nothing (the factory the composition gives)."""
    return NoCatalogHits()


@dataclass
class SourceStack[W: Wire]:
    wire: W
    recorder: Recorder
    network: Network
    registry: PluginRegistry
    fetchers: SourceFetchers
    bus: EventBus
    acquirer: NativeAcquirer


def first_party(key: str, load: Callable[[], Any]) -> PluginCandidate:
    """The plugin `load` builds, as a first-party candidate under `key`."""
    return PluginCandidate(key=key, origin=PluginOrigin.FIRST_PARTY, load=load)


def source_stack[W: Wire](
    lib: LibEnv,
    make_wire: Callable[[Recorder], W],
    *,
    hosts: Iterable[str],
    candidates: list[PluginCandidate],
    fetchers: Callable[[Network], SourceFetchers] = SourceFetchers,
    settings: Mapping[str, object] | None = None,
    secrets: Mapping[str, str] | None = None,
    work_screen: Callable[[], Any] | None = no_work_hits,
    subscribe: Mapping[str, Callable[[Any], None]] | None = None,
) -> SourceStack[W]:
    """The wire answers what the recorder is asked; the acquirer reads `settings` and `secrets`
    (the sealed ones of a source) for every source key, as the host does for the one it opens.
    `subscribe`: handlers by event type on the bus. `work_screen=None`: no tombstone screen."""
    holder: list[W] = []
    recorder = Recorder(lambda request: holder[0].respond(request))
    wire = make_wire(recorder)
    holder.append(wire)
    network = Network(
        resolver=FakeResolver({h: [PUBLIC] for h in hosts}), transport=recorder.transport
    )
    registry = PluginRegistry()
    registry.load_all(candidates)
    pool = fetchers(network)
    clock = SystemClock()
    bus = EventBus(clock)
    for event_type, handler in (subscribe or {}).items():
        bus.subscribe(handler, event_type=event_type)
    staging = lib.tmp / "recv"
    staging.mkdir()
    options: dict[str, Any] = {"work_screen": work_screen}
    if settings is not None:
        options["source_settings"] = lambda _key: dict(settings)
    if secrets is not None:
        options["source_secrets"] = lambda _key: dict(secrets)
    acquirer = NativeAcquirer(
        lib.engine, clock, bus, registry, pool, staging_dir=staging, max_bytes=8 * 1024 * 1024,
        **options,
    )  # fmt: skip
    return SourceStack(wire, recorder, network, registry, pool, bus, acquirer)
