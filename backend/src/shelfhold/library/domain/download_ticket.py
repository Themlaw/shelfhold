"""A download request of a Unit being purged, as the service is asked about it (L17)."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DownloadTicket:
    """What the service is asked about: never a title, only the request, its opaque ticket and
    the upstream source it belongs to."""

    request_id: str
    ticket: str
    unit_id: str
    state: str  # requested | downloaded | fetched
    plugin_id: str
    source_key: str
