# SPDX-License-Identifier: MIT
"""The import folder as a native source (EF-SRC-012): a manifest and nothing else.

It declares no domain, no LAN target, no browser and no capability: it never reaches the network,
and the files it takes in are read by the library (`library/adapters/import_scan.py`), which owns
every guard (what a file is, protected files, links, names, stability, removal after a hash-verified
storage). `content_trust` is `untrusted`: an import is `publisher` only when the person declares it
a purchase, per import, never by this manifest.
"""

from shelfhold.sdk.models import API_VERSION, Permissions, PluginKind, RateLimit
from shelfhold.sdk.source import Capability, SourceManifest

KEY = "import_folder"


def build_manifest() -> SourceManifest:
    return SourceManifest(
        key=KEY,
        kind=PluginKind.SOURCE,
        version="0.1.0",
        api=API_VERSION,
        name="Import folder",
        permissions=Permissions(domains=[]),
        terms_url="https://github.com/Themlaw/shelfhold/blob/main/docs/admin/dossier-import.md",
        respects_robots=True,
        content_warning="mixed",
        attribution=None,
        homepage=None,
        langs=[],
        content_kinds={"file"},
        url_patterns=[],
        capabilities=Capability(0),
        rate_limit=RateLimit(requests_per_second=1.0, concurrency=1),
        rights_basis="user_supplied",
        content_trust="untrusted",
    )


__all__ = ["KEY", "build_manifest"]
