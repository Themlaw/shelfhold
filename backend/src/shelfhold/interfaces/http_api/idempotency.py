"""`Idempotency-Key` on POST (EF-INT-001): the same key replays the same answer, no double effect.

Pure ASGI middleware. Only JSON (or empty) answers below 500 are retained: a server error is not
an answer, and a response that must not be stored (`Cache-Control: no-store`, e.g. a one-time
secret) is never written to the database, so its key is released instead of replayed.
"""

import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from shelfhold.infrastructure.idempotency_store import (
    Claim,
    Fresh,
    IdempotencyStore,
    InFlight,
    Replay,
    Reused,
)
from shelfhold.interfaces.http_api.catalog import error_code
from shelfhold.interfaces.http_api.problems import PROBLEM_MEDIA_TYPE, build_problem_response

IDEMPOTENCY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"
MAX_BODY_BYTES = 1024 * 1024  # a keyed request is buffered to be hashed
MAX_STORED_BYTES = 1024 * 1024
_KEY = re.compile(r"[\x21-\x7e]{1,255}")
_DETERMINISTIC_ERRORS = frozenset({400, 404, 409, 422})
_REPLAYED_HEADERS = ("location", "etag", "content-type")

ActorOf = Callable[[Scope], str]
# Set in `scope["state"]` by a route whose answer must never be kept nor replayed: a reserved
# action (review PR #41) — a replay would skip the credential and freshness checks.
NEVER_STORE = "idempotency_never_store"


class IdempotencyMiddleware:
    def __init__(self, app: ASGIApp, store: IdempotencyStore, actor_of: ActorOf) -> None:
        self.app = app
        self.store = store
        self.actor_of = actor_of

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        key = headers.get(IDEMPOTENCY_HEADER)
        if key is None:
            await self.app(scope, receive, send)
            return
        request_id = scope.get("state", {}).get("request_id")
        if not _KEY.fullmatch(key):
            await self._problem(scope, receive, send, "idempotency_key_invalid", request_id)
            return
        body = await self._read_body(receive)
        if body is None:
            await self._problem(scope, receive, send, "request_too_large", request_id)
            return

        actor = self.actor_of(scope)
        path = scope["path"]
        digest = hashlib.sha256()
        for part in (scope["query_string"], headers.get("content-type", "").encode(), body):
            digest.update(len(part).to_bytes(8, "big") + part)
        outcome = await run_in_threadpool(
            self.store.begin,
            actor_id=actor,
            key=key,
            method="POST",
            path=path,
            request_hash=f"sha256:{digest.hexdigest()}",
        )
        match outcome:
            case Replay():
                await self._replay(scope, receive, send, outcome)
            case Reused():
                await self._problem(scope, receive, send, "idempotency_key_reused", request_id)
            case InFlight():
                extensions = {"retry_after_s": 1}
                await self._problem(
                    scope, receive, send, "idempotency_in_progress", request_id,
                    extensions=extensions, headers={"Retry-After": "1"},
                )  # fmt: skip
            case Fresh():
                await self._run(scope, body, send, outcome.claim)

    @staticmethod
    async def _read_body(receive: Receive) -> bytes | None:
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                return None
            chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)

    async def _run(self, scope: Scope, body: bytes, send: Send, claim: Claim) -> None:
        sent = False

        async def receive_again() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        status = 500
        response_headers: Headers | None = None
        captured = bytearray()
        overflow = False

        async def capture(message: Message) -> None:
            nonlocal status, response_headers, overflow
            if message["type"] == "http.response.start":
                status = message["status"]
                response_headers = Headers(raw=message["headers"])
            elif message["type"] == "http.response.body":
                captured.extend(message.get("body", b""))
                overflow = overflow or len(captured) > MAX_STORED_BYTES
            await send(message)

        try:
            await self.app(scope, receive_again, capture)
        except BaseException:
            await run_in_threadpool(self.store.release, claim)
            raise
        never = bool(scope.get("state", {}).get(NEVER_STORE))
        if (
            not never
            and response_headers is not None
            and self._storable(status, response_headers, overflow, bytes(captured))
        ):
            kept = {n: response_headers[n] for n in _REPLAYED_HEADERS if n in response_headers}
            await run_in_threadpool(
                self.store.complete,
                claim,
                status=status,
                headers=kept,
                body=bytes(captured),
            )
        else:
            await run_in_threadpool(self.store.release, claim)

    @staticmethod
    def _storable(status: int, headers: Headers, overflow: bool, body: bytes) -> bool:
        # Retained: successes and deterministic client errors. 401, 403, 408, 429, 5xx and the
        # rest depend on state or time that a retry may have fixed: the key is released.
        if overflow or not (200 <= status < 300 or status in _DETERMINISTIC_ERRORS):
            return False
        if "no-store" in headers.get("cache-control", "").lower():
            return False
        content_type = headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type not in {"", "application/json", PROBLEM_MEDIA_TYPE}:
            return False
        try:
            return not body or json.loads(body) is not ...
        except ValueError:
            return False

    @staticmethod
    async def _replay(scope: Scope, receive: Receive, send: Send, replay: Replay) -> None:
        raw: list[tuple[bytes, bytes]] = []
        for name, value in replay.headers.items():
            raw.append((name.encode("latin-1"), value.encode("latin-1")))
        raw.append((b"content-length", str(len(replay.body)).encode()))
        out = MutableHeaders(raw=raw)
        out[REPLAYED_HEADER] = "true"
        await send({"type": "http.response.start", "status": replay.status, "headers": out.raw})
        await send({"type": "http.response.body", "body": replay.body})

    @staticmethod
    async def _problem(
        scope: Scope,
        receive: Receive,
        send: Send,
        code: str,
        request_id: str | None,
        *,
        extensions: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        response = build_problem_response(
            error_code(code), request_id=request_id, extensions=extensions, headers=headers
        )
        await response(scope, receive, send)
