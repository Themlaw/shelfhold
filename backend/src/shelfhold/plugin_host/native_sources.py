"""The host's own native sources, built with what an adapter may not import (the hardened XML
parser). An adapter imports only the SDK; the host hands it the parser."""

from shelfhold.infrastructure.xml import safe_parse_xml
from shelfhold.plugin_host.adapters.native.opds import OpdsSource
from shelfhold.plugin_host.adapters.native.standardebooks import StandardEbooksSource
from shelfhold.plugin_host.registry import PluginCandidate, PluginOrigin

__all__ = ["first_party_native_candidates", "opds_source", "standardebooks_source"]


def opds_source() -> OpdsSource:
    return OpdsSource(safe_parse_xml)


def standardebooks_source() -> StandardEbooksSource:
    return StandardEbooksSource(safe_parse_xml)


def first_party_native_candidates() -> list[PluginCandidate]:
    return [
        PluginCandidate(key="opds", origin=PluginOrigin.FIRST_PARTY, load=opds_source),
        PluginCandidate(
            key="standardebooks", origin=PluginOrigin.FIRST_PARTY, load=standardebooks_source
        ),
    ]
