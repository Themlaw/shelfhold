"""Synthetic MediaWiki API and export-tool answers in the shape Wikisource serves them (M2 L24).

Nothing here is copied from a real wiki or a real book: titles, contributors and identifiers are
made up. The builders return bytes so a test serves them as a server would.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["Page", "info_answer", "search_answer"]


class Page:
    """One synthetic page of the main namespace (or another one, to test the refusal)."""

    def __init__(
        self,
        title: str,
        *,
        contributors: tuple[str, ...] = ("Alice Synth", "Bruno Synth"),
        anonymous: int = 0,
        ns: int = 0,
        more: bool = False,
    ) -> None:
        self.title = title
        self.contributors = contributors
        self.anonymous = anonymous
        self.ns = ns
        self.more = more  # the API says it has more contributors than it gave


def search_answer(pages: list[Page], *, offset: int = 0, more: bool = False) -> bytes:
    body: dict[str, Any] = {
        "batchcomplete": True,
        "query": {
            "search": [{"ns": p.ns, "title": p.title, "pageid": i + 1} for i, p in enumerate(pages)]
        },
    }
    if more:
        body["continue"] = {"sroffset": offset, "continue": "-||"}
    return json.dumps(body).encode()


def info_answer(page: Page | None, title: str) -> bytes:
    if page is None:
        return json.dumps({"query": {"pages": [{"title": title, "missing": True}]}}).encode()
    entry: dict[str, Any] = {
        "pageid": 7,
        "ns": page.ns,
        "title": page.title,
        "contributors": [{"userid": i + 1, "name": n} for i, n in enumerate(page.contributors)],
        "anoncontributors": page.anonymous,
    }
    body: dict[str, Any] = {"batchcomplete": not page.more, "query": {"pages": [entry]}}
    if page.more:
        body["continue"] = {"pccontinue": "9|x", "continue": "||"}
    return json.dumps(body).encode()
