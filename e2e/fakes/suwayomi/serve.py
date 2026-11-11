"""Simulated Suwayomi over HTTP for the E2E (EF-SRC-008): the in-process fake of the backend
tests behind a socket. Basic authentication, the synthetic library of
`backend/tests/fixtures/suwayomi_states/library.json`, the CBZ of `fixtures/synthetic/`.

POST /_fake/publish releases the second chapter of the first series upstream;
POST /_fake/twin gives the second series the title of the first (another language)
and a second chapter, published with the first one's; POST /_fake/sleep and /_fake/wake stop and start it (Sablier); POST /_fake/reinstall renumbers every local id (the tool reinstalled, EF-SRC-036); GET /_fake/state
tells what was asked of it. Never used by Shelfhold.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from tests.fakes.suwayomi import FakeSuwayomi  # type: ignore[import-not-found]

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from httpshell import serve  # noqa: E402

fake = FakeSuwayomi(auth_mode=os.environ.get("FAKE_SUWAYOMI_AUTH", "basic_auth"))
# The series starts with its first chapter only; the test publishes the next one upstream.
_alpha = fake.state["mangas"][0]["chapters"]
unpublished = [_alpha.pop()] if len(_alpha) > 1 else []
# What /store held when Suwayomi was asked to erase a download (the volume is mounted read-only
# by the M1b overlay): "the copy is put to safety before the upstream purge" is read there.
erased_with_store: list[dict[str, object]] = []
_STORE = Path(os.environ.get("FAKE_STORE_ROOT", "/nonexistent"))
_erase = fake._q_deleteDownloadedChapter  # noqa: SLF001


def _erase_observed(query: str, variables: dict[str, object]):  # type: ignore[no-untyped-def]
    kept = sorted(str(p) for p in _STORE.rglob("*.cbz")) if _STORE.is_dir() else []
    erased_with_store.append({"variables": variables, "store_files": kept})
    return _erase(query, variables)


fake._q_deleteDownloadedChapter = _erase_observed  # type: ignore[method-assign]  # noqa: SLF001
twin_chapters: list[dict[str, object]] = []  # filled by /_fake/twin, published with the rest


def handle(method: str, target: str, headers: dict[str, str], body: bytes):  # type: ignore[no-untyped-def]
    path = target.split("?", 1)[0]
    if path == "/_fake/publish_hidden" and method == "POST":
        # published upstream, but Suwayomi does not know it until it refreshes the series
        # (`fetchChapters`): what a tool that slept through its own schedule looks like
        while unpublished:
            fake.publish_upstream(unpublished.pop(), fake.state["mangas"][0]["id"])
        return 200, {}, b"{}"
    if path == "/_fake/expire" and method == "POST":
        fake._authorized = lambda headers: False  # type: ignore[method-assign]  # noqa: SLF001
        return 200, {}, b"{}"  # the credentials stop working: every call is answered 401
    if path == "/_fake/sleep" and method == "POST":
        fake.asleep = True  # Sablier stopped the container: nothing answers until the wake
        return 200, {}, b"{}"
    if path == "/_fake/wake" and method == "POST":
        fake.wake_up()
        return 200, {}, b"{}"
    if path == "/_fake/reinstall" and method == "POST":
        fake.reinstall()
        return 200, {}, b"{}"
    if path == "/_fake/publish" and method == "POST":
        _alpha.extend(unpublished)
        unpublished.clear()
        fake.state["mangas"][1]["chapters"].extend(twin_chapters)
        twin_chapters.clear()
        return 200, {}, b"{}"
    if path == "/_fake/twin" and method == "POST":
        # Two series with one title in two languages (en, fr): the same series name twice.
        beta = fake.state["mangas"][1]
        beta["title"] = fake.state["mangas"][0]["title"]
        first = beta["chapters"][0]
        twin_chapters.append(
            {**first, "id": 5010, "url": "/series-beta/ch-002", "name": "Chapter 2",
             "chapterNumber": 2.0, "sourceOrder": 1}  # fmt: skip
        )
        return 200, {}, b"{}"
    if path == "/_fake/category" and method == "POST":
        # ?name=<n>&exclude=1&assign=<manga url>: a category of the library, `exclude` holding its
        # series out of the scheduled updates
        from urllib.parse import parse_qs, urlsplit

        query = parse_qs(urlsplit(target).query)
        ident = fake.add_category(
            query["name"][0], include_in_update="EXCLUDE" if "exclude" in query else "UNSET"
        )
        for url in query.get("assign", []):
            fake.assign(url, ident)
        return 200, {"content-type": "application/json"}, json.dumps({"id": ident}).encode()
    if path == "/_fake/state":
        state = {"requests": [f"{r.method} {r.path}" for r in fake.requests],
                 "violations": fake.violations,
                 "categories": fake.categories,
                 "erased_with_store": erased_with_store, "refreshed": fake.refreshed,
                 "deleted": fake.deleted,  # chapters whose download Shelfhold asked to erase
                 "files_removed": fake.files_removed,
                 "logins": fake.logins, "login_users": fake.login_users,
                 "library": {a: sorted(acc.library) if acc.library is not None else None
                             for a, acc in fake.accounts.items()},
                 "assigned": {m["url"]: sorted(m.get("categoryIds", []))
                              for m in fake.state["mangas"]}}  # fmt: skip
        return 200, {"content-type": "application/json"}, json.dumps(state).encode()
    response = fake.handle(method, target, headers, body)
    return response.status, dict(response.headers), response.body


if __name__ == "__main__":
    serve("127.0.0.1", int(os.environ.get("PORT", "4567")), handle, "fake-suwayomi")
