"""A simulated KOReader for the E2E (M5 L24, EF-LEC-009): it replays, request by request, what the
kosync plugin of KOReader sends, and prints what Shelfhold answered.

Standard library only; it runs inside the container of the image under test, over the stack's own
loopback, so that nothing leaves the E2E network namespace. The vectors are the ones read in the
source of KOReader (`plugins/kosync.koplugin/KOSyncClient.lua` and `api.json` of the same plugin):

* every call carries `Accept: application/vnd.koreader.v1+json`, `x-auth-user` (here the public
  part of a device token) and `x-auth-key` (the md5 of the token's secret, in lowercase hex);
* the document is named by the partial MD5 of the file (`util.partialMD5`: twelve samples of at
  most 1 KiB at offsets `1024 << 2i` for `i` from -1 to 10, the first one at 0), computed here
  again from the algorithm, never taken from Shelfhold;
* `GET /kosync/users/auth` is the "Login" of the plugin, `POST /kosync/users/create` its
  "Register" (closed here: 402), `PUT /kosync/syncs/progress` sends where the reader is
  (`document`, `progress`, `percentage`, `device`, `device_id`) and `GET
  /kosync/syncs/progress/<document>` asks where the other devices left it.

Usage: `python client.py '<json>'` where the JSON holds `public_id`, `secret`, `file` (the book as
KOReader holds it, a path in the container), `progress` (an xpointer or a page, as text),
`percentage`, `device` and `device_id`. The output is a JSON list `[[name, status, body], ...]`.
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8080"
ACCEPT = "application/vnd.koreader.v1+json"
SAMPLE = 1024
MASK = 0xFFFFFFFF


def partial_md5(path: str) -> str:
    """`util.partialMD5` of KOReader: the digest of the samples, lowercase hex. LuaJIT's
    `bit.lshift` takes its count modulo 32, so the first offset (`i = -1`) is 0."""
    digest = hashlib.md5()  # noqa: S324 - KOReader's own identifier of a file, not a secret
    with open(path, "rb") as handle:
        for i in range(-1, 11):
            handle.seek((SAMPLE << ((2 * i) & 31)) & MASK)
            sample = handle.read(SAMPLE)
            if not sample:
                break
            digest.update(sample)
    return digest.hexdigest()


def call(
    method: str, path: str, headers: dict[str, str], body: dict[str, object] | None = None
) -> tuple[int, object]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(  # noqa: S310 - the loopback of the stack
        BASE + path, data=data, method=method,
        headers={"Accept": ACCEPT, **({"Content-Type": "application/json"} if data else {}), **headers},
    )  # fmt: skip
    try:
        reply = urllib.request.urlopen(request, timeout=20)  # noqa: S310
        status, text = reply.status, reply.read().decode()
    except urllib.error.HTTPError as error:
        status, text = error.code, error.read().decode()
    try:
        return status, json.loads(text) if text else None
    except ValueError:
        return status, text


def play(spec: dict[str, str]) -> list[list[object]]:
    key = hashlib.md5(spec["secret"].encode()).hexdigest()  # noqa: S324 - the plugin's own scheme
    auth = {"x-auth-user": spec["public_id"], "x-auth-key": key}
    document = partial_md5(spec["file"])
    steps: list[list[object]] = [["document", 0, document]]

    def record(name: str, answer: tuple[int, object]) -> None:
        steps.append([name, answer[0], answer[1]])

    record("login", call("GET", "/kosync/users/auth", auth))
    record("register", call("POST", "/kosync/users/create", {}, {"username": "x", "password": "y"}))
    record("login_wrong_key", call("GET", "/kosync/users/auth", {**auth, "x-auth-key": "0" * 32}))
    record("pull_on_open", call("GET", f"/kosync/syncs/progress/{document}", auth))
    record(
        "push",
        call(
            "PUT", "/kosync/syncs/progress", auth,
            {"document": document, "progress": spec["progress"],
             "percentage": float(spec["percentage"]), "device": spec["device"],
             "device_id": spec["device_id"]},
        ),
    )  # fmt: skip
    record("pull_back", call("GET", f"/kosync/syncs/progress/{document}", auth))
    record("pull_unknown", call("GET", "/kosync/syncs/progress/" + "0" * 32, auth))
    return steps


if __name__ == "__main__":
    print(json.dumps(play(json.loads(sys.argv[1]))))
