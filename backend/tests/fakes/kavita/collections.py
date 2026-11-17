"""The collections and reading lists of the simulated Kavita (M4 L18b, EF-EXP-025).

What Kavita does, as far as Shelfhold depends on it (`CollectionController`, `ReadingListController`
at tag v0.9.1.4):
- a collection or a list belongs to the account of the key that made it, and is seen by that account
  only, until it is *promoted*; a promoted one is seen by every account, each seeing only the series
  of the libraries it may read (`seen_by`);
- `update-for-series` with `collectionTagId` 0 creates the collection (its title is unique per
  account) and adds the series; `update-series` removes series; `promote-multiple` only works on the
  caller's own collections; a reading list is made of the chapters of the series added, in order.

The referee is the same as the server's: a body the DTO does not declare, a title twice, a promotion
of what is not the caller's, are recorded as violations.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx2

if TYPE_CHECKING:
    from tests.fakes.kavita.server import FakeKavita, RecordedRequest


@dataclass
class KavitaCollections:
    collections: dict[int, dict[str, Any]] = field(default_factory=dict)
    lists: dict[int, dict[str, Any]] = field(default_factory=dict)
    next_id: int = 500
    next_item: int = 9000
    calls: list[tuple[str, str]] = field(default_factory=list)  # (account, "METHOD path")

    # -- what a person does in Kavita, outside Shelfhold ----------------------------------------
    def by_hand_collection(
        self, account: str, title: str, series: list[int], *, promoted: bool = False
    ) -> int:
        self.next_id += 1
        self.collections[self.next_id] = {
            "id": self.next_id, "title": title, "summary": "", "promoted": promoted,
            "owner": account, "series": list(series),
        }  # fmt: skip
        return self.next_id

    def by_hand_list(self, account: str, title: str, series: list[int]) -> int:
        self.next_id += 1
        self.lists[self.next_id] = {
            "id": self.next_id, "title": title, "promoted": False, "owner": account, "items": [],
        }  # fmt: skip
        for s in series:
            self.add_items(self.next_id, s)
        return self.next_id

    def add_items(self, list_id: int, series_id: int) -> None:
        for n in (1, 2):  # two chapters a series
            self.next_item += 1
            self.lists[list_id]["items"].append({"id": self.next_item, "seriesId": series_id,
                                                 "chapterNumber": str(n)})  # fmt: skip

    # -- what an account sees --------------------------------------------------------------------
    def readable(self, fake: FakeKavita, account: str) -> set[int]:
        """The ids of the series the account may read: those of its libraries (an administrator:
        all)."""
        user = next((u for u in fake.users if u["username"] == account), None)
        if user is None:
            return set()
        if "Admin" in user["roles"]:
            return {s["id"] for s in fake.series}
        libraries = {lib["id"] for lib in user["libraries"]}
        return {s["id"] for s in fake.series if s["libraryId"] in libraries}

    def seen_by(self, fake: FakeKavita, account: str) -> dict[str, dict[str, list[int]]]:
        """`{"collections": {title: series ids}, "lists": {title: series ids}}` as the account sees
        them: its own and the promoted ones, each cut down to the series it may read. A collection
        with nothing left in it is not shown."""
        readable = self.readable(fake, account)
        out: dict[str, dict[str, list[int]]] = {"collections": {}, "lists": {}}
        for c in self.collections.values():
            if c["owner"] == account or c["promoted"]:
                shown = [s for s in c["series"] if s in readable]
                if shown or c["owner"] == account:
                    out["collections"][c["title"]] = shown
        for item in self.lists.values():
            if item["owner"] == account or item["promoted"]:
                shown = [i["seriesId"] for i in item["items"] if i["seriesId"] in readable]
                if shown or item["owner"] == account:
                    out["lists"][item["title"]] = list(dict.fromkeys(shown))
        return out

    def owned_by(self, account: str) -> list[dict[str, Any]]:
        return [c for c in (*self.collections.values(), *self.lists.values())
                if c["owner"] == account]  # fmt: skip

    # -- the routes --------------------------------------------------------------------------------
    def handle(self, fake: FakeKavita, recorded: RecordedRequest) -> httpx2.Response:
        key = recorded.headers.get("x-api-key", "")
        account = fake.key_accounts.get(key, fake.account[0])
        path = recorded.path.lower()
        self.calls.append((account, f"{recorded.method} {recorded.path}"))
        route = (recorded.method, path.removeprefix("/api/"))
        handler = {
            ("GET", "collection"): self._collection_list,
            ("POST", "collection/update-for-series"): self._collection_add,
            ("POST", "collection/update-series"): self._collection_remove,
            ("POST", "collection/promote-multiple"): self._collection_promote,
            ("DELETE", "collection"): self._collection_delete,
            ("POST", "readinglist/lists"): self._list_all,
            ("GET", "readinglist/items"): self._list_items,
            ("POST", "readinglist/create"): self._list_create,
            ("POST", "readinglist/update-by-series"): self._list_add_series,
            ("POST", "readinglist/delete-item"): self._list_delete_item,
            ("POST", "readinglist/promote-multiple"): self._list_promote,
            ("DELETE", "readinglist"): self._list_delete,
        }.get(route)
        if handler is None:
            return fake._violation(f"{recorded.method} {recorded.path}: no such route")
        return handler(fake, recorded, account)  # type: ignore[no-any-return]

    @staticmethod
    def _dto(c: dict[str, Any]) -> dict[str, Any]:
        return {"id": c["id"], "title": c["title"], "summary": c.get("summary", ""),
                "promoted": c["promoted"], "owner": c["owner"],
                "itemCount": len(c.get("series", c.get("items", [])))}  # fmt: skip

    def _own(
        self, table: dict[int, dict[str, Any]], ident: Any, account: str
    ) -> dict[str, Any] | None:
        found = table.get(ident) if isinstance(ident, int) else None
        return found if found is not None and found["owner"] == account else None

    def _collection_list(
        self, fake: FakeKavita, r: RecordedRequest, account: str
    ) -> httpx2.Response:
        owned_only = r.query.get("ownedOnly", "false") == "true"
        shown = [
            self._dto(c) for c in self.collections.values()
            if c["owner"] == account or (c["promoted"] and not owned_only)
        ]  # fmt: skip
        return fake._json(shown)

    def _collection_add(
        self, fake: FakeKavita, r: RecordedRequest, account: str
    ) -> httpx2.Response:
        body = fake._body(r, "CollectionTagBulkAddDto")
        if isinstance(body, httpx2.Response):
            return body
        ids = body.get("seriesIds") or []
        if not all(i in fake.ages for i in ids):
            return fake._violation("collection: an unknown series")
        if body["collectionTagId"] == 0:
            title = body.get("collectionTagTitle") or ""
            if not title or any(
                c["owner"] == account and c["title"] == title for c in self.collections.values()
            ):
                return fake._violation(f"collection: the title {title!r} is empty or taken")
            self.next_id += 1
            self.collections[self.next_id] = {
                "id": self.next_id, "title": title, "summary": "", "promoted": False,
                "owner": account, "series": [],
            }  # fmt: skip
            found = self.collections[self.next_id]
        else:
            found = self._own(self.collections, body["collectionTagId"], account)
            if found is None:
                return fake._json({"title": "Not Found", "status": 404}, 404)
        found["series"] += [i for i in ids if i not in found["series"]]
        return fake._text(200, "")

    def _collection_remove(
        self, fake: FakeKavita, r: RecordedRequest, account: str
    ) -> httpx2.Response:
        body = fake._body(r, "UpdateSeriesForTagDto")
        if isinstance(body, httpx2.Response):
            return body
        found = self._own(self.collections, (body.get("tag") or {}).get("id"), account)
        if found is None:
            return fake._json({"title": "Not Found", "status": 404}, 404)
        gone = set(body.get("seriesIdsToRemove") or [])
        found["series"] = [s for s in found["series"] if s not in gone]
        return fake._text(200, "")

    def _collection_promote(
        self, fake: FakeKavita, r: RecordedRequest, account: str
    ) -> httpx2.Response:
        body = fake._body(r, "PromoteCollectionsDto")
        if isinstance(body, httpx2.Response):
            return body
        mine = [self._own(self.collections, i, account) for i in body.get("collectionIds") or []]
        if not mine or any(m is None for m in mine):
            return fake._violation("promote: a collection that is not the caller's")
        for m in mine:
            assert m is not None
            m["promoted"] = bool(body["promoted"])
        return fake._text(200, "")

    def _collection_delete(
        self, fake: FakeKavita, r: RecordedRequest, account: str
    ) -> httpx2.Response:
        tag = r.query.get("tagId", "")
        found = self._own(self.collections, int(tag) if tag.isdigit() else None, account)
        if found is None:
            return fake._json({"title": "Not Found", "status": 404}, 404)
        del self.collections[found["id"]]
        return fake._text(200, "")

    def _list_all(self, fake: FakeKavita, r: RecordedRequest, account: str) -> httpx2.Response:
        include = r.query.get("includePromoted", "true") == "true"
        shown = [
            {**self._dto(x), "ownerUserName": x["owner"]} for x in self.lists.values()
            if x["owner"] == account or (include and x["promoted"])
        ]  # fmt: skip
        return fake._json(shown)

    def _list_items(self, fake: FakeKavita, r: RecordedRequest, account: str) -> httpx2.Response:
        ident = r.query.get("readingListId", "")
        found = self._own(self.lists, int(ident) if ident.isdigit() else None, account)
        if found is None:
            return fake._json({"title": "Not Found", "status": 404}, 404)
        return fake._json(
            [{"id": i["id"], "order": n, "seriesId": i["seriesId"]}
             for n, i in enumerate(found["items"])]
        )  # fmt: skip

    def _list_create(self, fake: FakeKavita, r: RecordedRequest, account: str) -> httpx2.Response:
        body = fake._body(r, "CreateReadingListDto")
        if isinstance(body, httpx2.Response):
            return body
        title = body.get("title") or ""
        if not title or any(
            x["owner"] == account and x["title"] == title for x in self.lists.values()
        ):
            return fake._violation(f"reading list: the title {title!r} is empty or taken")
        self.next_id += 1
        self.lists[self.next_id] = {"id": self.next_id, "title": title, "promoted": False,
                                    "owner": account, "items": []}  # fmt: skip
        return fake._json({**self._dto(self.lists[self.next_id]), "ownerUserName": account})

    def _list_add_series(
        self, fake: FakeKavita, r: RecordedRequest, account: str
    ) -> httpx2.Response:
        body = fake._body(r, "UpdateReadingListBySeriesDto")
        if isinstance(body, httpx2.Response):
            return body
        found = self._own(self.lists, body["readingListId"], account)
        if found is None or body["seriesId"] not in fake.ages:
            return fake._json({"title": "Not Found", "status": 404}, 404)
        if all(i["seriesId"] != body["seriesId"] for i in found["items"]):
            self.add_items(found["id"], body["seriesId"])
        return fake._text(200, "")

    def _list_delete_item(
        self, fake: FakeKavita, r: RecordedRequest, account: str
    ) -> httpx2.Response:
        body = fake._body(r, "UpdateReadingListPosition")
        if isinstance(body, httpx2.Response):
            return body
        found = self._own(self.lists, body["readingListId"], account)
        if found is None:
            return fake._json({"title": "Not Found", "status": 404}, 404)
        found["items"] = [i for i in found["items"] if i["id"] != body["readingListItemId"]]
        return fake._text(200, "")

    def _list_promote(self, fake: FakeKavita, r: RecordedRequest, account: str) -> httpx2.Response:
        body = fake._body(r, "PromoteReadingListsDto")
        if isinstance(body, httpx2.Response):
            return body
        mine = [self._own(self.lists, i, account) for i in body.get("readingListIds") or []]
        if not mine or any(m is None for m in mine):
            return fake._violation("promote: a reading list that is not the caller's")
        for m in mine:
            assert m is not None
            m["promoted"] = bool(body["promoted"])
        return fake._text(200, "")

    def _list_delete(self, fake: FakeKavita, r: RecordedRequest, account: str) -> httpx2.Response:
        ident = r.query.get("readingListId", "")
        found = self._own(self.lists, int(ident) if ident.isdigit() else None, account)
        if found is None:
            return fake._json({"title": "Not Found", "status": 404}, 404)
        del self.lists[found["id"]]
        return fake._text(200, "")

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy({"collections": self.collections, "lists": self.lists})
