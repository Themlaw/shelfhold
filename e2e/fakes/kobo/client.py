"""A simulated Kobo for the E2E (M6, EF-INT-012): it replays, request by request, what the device
sends to the address it was given, and prints what Shelfhold answered. Synthetic data only.

Standard library only; it runs inside the container of the image under test, over the stack's own
loopback, so nothing leaves the E2E network namespace. What it does, in the order of a device
that wakes up: the initialization, the dummy activation of `v1/auth/device`, then the library
sync page by page (`x-kobo-synctoken` to ask for the next page, `x-kobo-sync: continue` while one
follows). It only reads: it never sends a reading state, a shelf or a book (that side of the
protocol is not delivered).

Usage: `python client.py '<json>'` where the JSON holds `token` (the path segment of the device's
address) and, optionally, `sync_token` (the one a previous sync ended with: only what changed is
asked for), `only` (`init`: stop after the initialization) and `max_pages`. The output is a JSON
document: `init`, `auth`, `pages` and the `final_token`.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8080"
USER_AGENT = "Mozilla/5.0 (Linux; U; Android 2.0) Kobo Reader"


def call(
    method: str, path: str, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], object]:
    request = urllib.request.Request(  # noqa: S310 - the loopback of the stack
        BASE + path,
        data=b"{}" if method == "POST" else None,
        method=method,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json", **(headers or {})},
    )
    try:
        reply = urllib.request.urlopen(request, timeout=30)  # noqa: S310
        status, text, head = reply.status, reply.read().decode(), dict(reply.headers)
    except urllib.error.HTTPError as error:
        status, text, head = error.code, error.read().decode(), dict(error.headers)
    try:
        body: object = json.loads(text) if text else None
    except ValueError:
        body = text
    return status, {k.lower(): v for k, v in head.items()}, body


def titles(wrapped: list[dict[str, object]]) -> list[str]:
    found = []
    for item in wrapped:
        entitlement = item.get("Entitlement", item)
        metadata = entitlement.get("BookMetadata", {})  # type: ignore[union-attr]
        found.append(str(metadata.get("Title", "")))
    return found


def main(spec: dict[str, object]) -> dict[str, object]:
    root = f"/kobo/{spec['token']}"
    status, head, body = call("GET", f"{root}/v1/initialization")
    init = {"status": status, "body": body, "no_store": head.get("cache-control") == "no-store"}
    out: dict[str, object] = {"init": init}
    if status != 200 or spec.get("only") == "init":
        return out
    out["auth"] = call("POST", f"{root}/v1/auth/device")[0]
    pages: list[dict[str, object]] = []
    sync_token = spec.get("sync_token")
    for _ in range(int(spec.get("max_pages", 20))):  # a device that is told "continue" asks again
        headers = {"x-kobo-synctoken": str(sync_token)} if sync_token else {}
        status, head, body = call("GET", f"{root}/v1/library/sync", headers)
        page: dict[str, object] = {"status": status, "continue": head.get("x-kobo-sync") == "continue"}
        if status == 200 and isinstance(body, dict):
            result = body["SyncStateResult"]
            page.update(
                new=titles(result["NewEntitlements"]),
                changed=len(result.get("ChangedProductMetadata", [])),
                deleted=len(result["DeletedEntitlements"]),
                tags=len(result.get("NewTagging", [])),
                reading_states=len(result["ChangedReadingState"]),
                token=head.get("x-kobo-synctoken"),
            )
            sync_token = head.get("x-kobo-synctoken")
        else:
            page["body"] = body
        pages.append(page)
        if status != 200 or not page["continue"]:
            break
    out["pages"] = pages
    out["final_token"] = sync_token
    return out


if __name__ == "__main__":
    print(json.dumps(main(json.loads(sys.argv[1]))))
