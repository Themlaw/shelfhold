"""`/lite`: the pages of Shelfhold for a browser that runs no script (M5 L17a, EF-LEC-010).

Server-rendered HTML, one stylesheet, no `<script>` anywhere (a CI control reads every page and
every template). A reader signs in with the secret of a device token, browses the shelves, the
series and the Works her profile may see, and downloads the block of a series the way the OPDS
feed offers it. See `session` for the door, `routes` for the pages, `render` for how a page is
made. This package never imports the OPDS surface (import-linter).
"""

from shelfhold.interfaces.http_api.lite.routes import LiteBundles, build_lite_router

__all__ = ["LiteBundles", "build_lite_router"]
