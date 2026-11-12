"""lncrawl adapter: the only code that knows lncrawl (EF-SRC-010, EF-SRC-033).

It speaks to the instance the user runs and to nothing else; Shelfhold activates, lists and
suggests no crawler.
"""

from shelfhold.plugin_host.adapters.meta.lncrawl.service import LncrawlService

__all__ = ["LncrawlService"]
