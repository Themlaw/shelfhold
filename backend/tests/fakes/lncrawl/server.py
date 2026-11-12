"""A simulated lncrawl instance, in process, synthetic only (EF-SRC-010, D11, ENF-LEG-013).

Nothing runs a crawler here: a real instance cannot produce anything in CI because Shelfhold
activates none. This is a faithful stand-in for the REST subset the adapter pins (`/api/v1`, see
`docs/admin/lncrawl.md`): the library of the account, the chapters of a novel, a job that builds one
EPUB for a range of chapters, its artifact. It also plays the referee: a request outside the
allow-list of routes is answered 400 and recorded in `violations`, and no route lists, names or
switches a crawler (the allow-list is written here on purpose, independently of the adapter).

The hostile knobs turn the instance into what the attacks need: a job that never ends, a job whose
answer points to another host or another range, an endless listing, a reply far too large, a name
that must never reach a log (`CANARY`).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2

from shelfhold.sdk.fetcher import CookieJarView, DownloadedFile, Response
from shelfhold.sdk.refs import ImageRequest

BASE_URL = "http://lncrawl.lan:8080"
API_KEY = "synthetic-test-key-not-a-real-one"
CANARY = "synthetic-canary-name-7f3a"  # a name the instance may repeat and Shelfhold must not
PAGE_SIZE = 4

ALLOWED_ROUTES = (
    re.compile(r"^/api/v1/status$"),
    re.compile(r"^/api/v1/library$"),
    re.compile(r"^/api/v1/novels/[A-Za-z0-9_-]+$"),
    re.compile(r"^/api/v1/novels/[A-Za-z0-9_-]+/chapters$"),
    re.compile(r"^/api/v1/jobs$"),
    re.compile(r"^/api/v1/jobs/[A-Za-z0-9_-]+$"),
    re.compile(r"^/api/v1/jobs/[A-Za-z0-9_-]+/cancel$"),
    re.compile(r"^/api/v1/artifacts/[A-Za-z0-9_-]+/file$"),
)


@dataclass
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes = b""
    query: dict[str, list[str]] = field(default_factory=dict)


def default_novels() -> list[dict[str, Any]]:
    """One novel of ten chapters: Vol.1 (closed) by a first team, Vol.2 (open) by the next."""
    chapters = [
        {
            "id": f"ch-{n}", "order": n, "title": f"Chapter {n}",
            "volume": "Vol.1" if n <= 6 else "Vol.2", "closed": n <= 6,
            "team": "Synthetic Team A" if n <= 5 else "Synthetic Team B",
        }
        for n in range(1, 11)
    ]  # fmt: skip
    return [
        {"id": "nv-alpha", "title": "Novel Alpha", "authors": ["Nobody"], "language": "en",
         "status": "ongoing", "chapters": chapters}
    ]  # fmt: skip


class FakeLncrawl:
    def __init__(self, *, api_key: str | None = API_KEY) -> None:
        self.api_key = api_key
        self.novels = default_novels()
        self.instance = "inst-0001"
        self.version = "1.4.0"
        self.requests: list[RecordedRequest] = []
        self.violations: list[str] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.cancelled: list[str] = []
        self.down = False
        self.queue_polls = 2  # `queued` this many polls, then `running` once, then `done`
        self.page_size = PAGE_SIZE
        # hostile knobs
        self.stuck = False  # a job that never leaves the queue
        self.endless_pages = False  # a listing whose `next` never ends
        self.huge_reply = False  # a status reply far past any bound
        self.raw_reply: bytes | None = None  # every answer is this, whatever was asked
        self.leak_name = False  # the instance repeats a name everywhere it can
        self.tamper_job: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        self.epub_override: bytes | None = None  # served instead of the built EPUB
        self.artifact_hits: list[str] = []

    # -- helpers ----------------------------------------------------------------------------
    def novel(self, novel_id: str) -> dict[str, Any] | None:
        return next((n for n in self.novels if n["id"] == novel_id), None)

    def add_chapter(self, novel_id: str, *, team: str = "Synthetic Team B") -> str:
        novel = self.novel(novel_id)
        assert novel is not None
        number = len(novel["chapters"]) + 1
        novel["chapters"].append(
            {"id": f"ch-{number}", "order": number, "title": f"Chapter {number}",
             "volume": "Vol.2", "closed": False, "team": team}
        )  # fmt: skip
        return f"ch-{number}"

    def reinstall(self) -> None:
        self.instance = "inst-0002"
        self.jobs.clear()

    def build_epub(self, count: int) -> bytes:
        from tests.ranged_env import volume_epub

        return volume_epub(count)

    def requests_to(self, fragment: str) -> list[RecordedRequest]:
        return [r for r in self.requests if fragment in r.path]

    # -- HTTP -------------------------------------------------------------------------------
    def handle(
        self, method: str, target: str, headers: dict[str, str] | None = None, body: bytes = b""
    ) -> Response:
        if self.down:
            raise OSError("connection refused: the instance is stopped")
        lowered = {k.lower(): v for k, v in (headers or {}).items()}
        split = urlsplit(target)
        recorded = RecordedRequest(method, split.path, lowered, body, parse_qs(split.query))
        self.requests.append(recorded)
        if not any(route.match(split.path) for route in ALLOWED_ROUTES):
            return self._violation(f"{method} {split.path} is not an allow-listed route")
        if self.api_key is not None and lowered.get("x-api-key") != self.api_key:
            return self._json({"detail": f"refused, {CANARY}"}, 401)
        if self.raw_reply is not None:
            return Response(status=200, body=self.raw_reply)
        path = split.path.removeprefix("/api/v1")
        if method == "GET":
            return self._get(path, recorded)
        if method == "POST":
            if not lowered.get("content-type", "").startswith("application/json") and body:
                return self._violation("a POST body is JSON and nothing else")
            return self._post(path, json.loads(body or b"{}"))
        return self._violation(f"{method} is not a verb of the contract")

    def _violation(self, why: str) -> Response:
        self.violations.append(why)
        return self._json({"detail": why}, 400)

    @staticmethod
    def _json(payload: Any, status: int = 200) -> Response:
        return Response(
            status=status, headers={"content-type": "application/json"},
            body=json.dumps(payload).encode(),
        )  # fmt: skip

    def _leaky_chapter(self, chapter: dict[str, Any]) -> dict[str, Any]:
        """The instance repeats the name in the fields a host stores: title and team."""
        out = dict(chapter)
        if self.leak_name:
            out["title"] = f"{out['title']} {CANARY}"
            out["team"] = f"{out.get('team', '')} {CANARY}"
            out["crawler"] = CANARY
        return out

    def _leaky_novel(self, novel: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in novel.items() if k != "chapters"}
        if self.leak_name:
            out["title"] = f"{out['title']} {CANARY}"
            out["authors"] = [*out.get("authors", []), CANARY]
            out["crawler"] = CANARY
        return out

    def _get(self, path: str, request: RecordedRequest) -> Response:
        if path == "/status":
            if self.huge_reply:
                return self._json({"version": self.version, "instance": self.instance,
                                   "pad": "x" * (4 << 20)})  # fmt: skip
            return self._json({"version": self.version, "instance": self.instance})
        if path == "/library":
            return self._page(
                [
                    {k: v for k, v in self._leaky_novel(n).items() if k in ("title", "crawler")}
                    | {"id": n["id"]}
                    for n in self.novels
                ],
                request,
            )
        if m := re.fullmatch(r"/novels/([\w-]+)/chapters", path):
            novel = self.novel(m.group(1))
            if novel is None:
                return self._json({"detail": "no such novel"}, 404)
            return self._page([self._leaky_chapter(c) for c in novel["chapters"]], request)
        if m := re.fullmatch(r"/novels/([\w-]+)", path):
            novel = self.novel(m.group(1))
            if novel is None:
                return self._json({"detail": "no such novel"}, 404)
            return self._json(self._leaky_novel(novel))
        if m := re.fullmatch(r"/jobs/([\w-]+)", path):
            return self._job(m.group(1))
        if m := re.fullmatch(r"/artifacts/([\w-]+)/file", path):
            self.artifact_hits.append(m.group(1))
            job = self.jobs.get(m.group(1).removeprefix("art-"))
            if job is None or job["state"] != "done":
                return self._json({"detail": "no such artifact"}, 404)
            body = self.epub_override or self.build_epub(len(job["covers"]))
            return Response(status=200, headers={"content-type": "application/epub+zip"}, body=body)
        return self._violation(f"GET {path}")

    def _page(self, items: list[dict[str, Any]], request: RecordedRequest) -> Response:
        start = int((request.query.get("cursor") or ["0"])[0])
        window = items[start : start + self.page_size]
        more = start + self.page_size < len(items) or self.endless_pages
        if self.endless_pages and not window:
            window = [dict(items[0], id=f"x-{start}")] if items else []
        return self._json({"items": window, "next": str(start + self.page_size) if more else None})

    def _post(self, path: str, body: dict[str, Any]) -> Response:
        if path == "/jobs":
            novel = self.novel(str(body.get("novel")))
            if novel is None:
                return self._json({"detail": "no such novel"}, 404)
            ids = [c["id"] for c in novel["chapters"]]
            first, last = str(body.get("first")), str(body.get("last"))
            if first not in ids or last not in ids or ids.index(first) > ids.index(last):
                return self._json({"detail": "bad range"}, 422)
            job_id = f"job-{len(self.jobs) + 1}"
            span = ids[ids.index(first) : ids.index(last) + 1]
            chapters = {c["id"]: c for c in novel["chapters"]}
            self.jobs[job_id] = {
                "id": job_id, "state": "queued", "polls": 0, "novel": novel["id"],
                "first": first, "last": last, "covers": span,
                "closed": all(chapters[c]["closed"] for c in span),
            }  # fmt: skip
            return self._json({"job": job_id}, 202)
        if m := re.fullmatch(r"/jobs/([\w-]+)/cancel", path):
            self.cancelled.append(m.group(1))
            job = self.jobs.get(m.group(1))
            if job is not None:
                job["state"] = "cancelled"
            return self._json({"job": m.group(1)})
        return self._violation(f"POST {path}")

    def _job(self, job_id: str) -> Response:
        job = self.jobs.get(job_id)
        if job is None:
            return self._json({"detail": "no such job"}, 404)
        if job["state"] in ("queued", "running") and not self.stuck:
            job["polls"] += 1
            if job["polls"] > self.queue_polls:
                job["state"] = "done"
            elif job["polls"] == self.queue_polls:
                job["state"] = "running"
        answer: dict[str, Any] = {"job": job_id, "state": job["state"], "novel": job["novel"]}
        if job["state"] == "done":
            answer.update(
                first=job["first"], last=job["last"], covers=job["covers"],
                closed=job["closed"], artifact=f"art-{job_id}",
                filename=f"{job['novel']}-{job['first']}-{job['last']}.epub",
            )  # fmt: skip
        if self.leak_name:
            answer.update(crawler=CANARY, source=CANARY, detail=f"fetched by {CANARY}")
        if self.tamper_job is not None:
            answer = self.tamper_job(answer)
        return self._json(answer)

    # -- as a transport, to exercise the real IntegrationFetcher ----------------------------
    def transport_handler(self, request: httpx2.Request) -> httpx2.Response:
        if self.down:
            raise httpx2.ConnectError("connection refused", request=request)
        reply = self.handle(
            request.method, str(request.url), dict(request.headers), request.content or b""
        )
        return httpx2.Response(reply.status, headers=reply.headers, content=reply.body)

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.transport_handler)


class FakeLncrawlFetcher:
    """The SDK `Fetcher` seen by the adapter, wired to the simulated instance and nothing else."""

    record_mode = "none"

    def __init__(self, server: FakeLncrawl, base_url: str = BASE_URL, *, key: str | None = API_KEY):
        self.server = server
        self.base_url = base_url
        self._key = key
        self.posts: list[dict[str, Any]] = []
        self.downloads: list[ImageRequest] = []

    @property
    def unscripted(self) -> list[str]:
        return list(self.server.violations)

    async def aclose(self) -> None:
        """Nothing to release."""

    def settings(self) -> dict[str, Any]:
        return {"base_url": self.base_url}

    def _target(self, url: str, params: dict[str, Any] | None = None) -> str:
        if not url.startswith(self.base_url + "/"):
            self.server.violations.append(f"request outside the declared target: {url}")
            raise OSError(f"{url} is outside the declared target (record_mode=none)")
        target = url[len(self.base_url) :]
        if params:
            target += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        return target

    def _with_key(self, headers: dict[str, str] | None) -> dict[str, str]:
        merged = dict(headers or {})
        if self._key is not None:
            merged["X-API-Key"] = self._key  # what the host's injecting wrapper does
        return merged

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None, referer: str | None = None,
        cache_ttl: int | None = None,
    ) -> Response:  # fmt: skip
        return self.server.handle("GET", self._target(url, params), self._with_key(headers))

    async def post(
        self, url: str, *, data: dict[str, Any] | None = None, json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Response:  # fmt: skip
        self.posts.append({"url": url, "json": json})
        merged = self._with_key(headers)
        body = b""
        if json is not None:
            merged.setdefault("Content-Type", "application/json")
            body = _dumps(json)
        return self.server.handle("POST", self._target(url), merged, body)

    async def download(self, req: ImageRequest) -> DownloadedFile:
        self.downloads.append(req)
        reply = self.server.handle("GET", self._target(req.url), self._with_key(req.headers))
        if reply.status != 200:
            raise OSError(f"download answered {reply.status}")
        return DownloadedFile(
            sha256=hashlib.sha256(reply.body).hexdigest(), size=len(reply.body), content=reply.body
        )

    def cookies(self) -> CookieJarView:
        return CookieJarView()


def _dumps(value: Any) -> bytes:
    return json.dumps(value).encode()
