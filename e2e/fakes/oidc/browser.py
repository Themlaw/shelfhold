"""The person's browser, for the OpenID Connect journey of the E2E (M5 L24, EF-USR-004, EF-USR-024).

Standard library only; it runs inside the container of the image under test, over the stack's own
loopback, so nothing leaves the E2E network namespace. It does what a browser does on the way
through the provider, by hand and one redirect at a time (a redirect is never followed on its own,
so that each step can be read and a cookie kept or refused as the browser would):

* `login`: the address of the sign-in, the provider's page (`/authorize` of the simulated provider,
  over TLS, trusted through the test authority the stack installs), the return to the callback of
  Shelfhold (the redirect address is that of the public address of the instance, which only exists
  for the provider; the browser would be sent there, here the callback is asked on the loopback);
* `reauth`: the same, from a session already open, for a reserved action: the provider is asked to
  prove the person again (`prompt=login`, `max_age`).

Usage: `python browser.py '<json>'`: `{"steps": [...]}`, each step one of
`["login"]`, `["reauth"]`, `["request", method, path, body]` (the cookies and the CSRF header of
the page, as the SPA sends them; the cookie jar here is a browser's, so a `Secure` cookie of an
instance whose public address is `https` goes back over the loopback too); the output is the JSON list of
what each step saw (`status`, `location`, `body` and what the cookies held).
"""

from __future__ import annotations

import http.cookies
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

LOCAL = "http://127.0.0.1:8080"
PUBLIC = "https://shelfhold.e2e.test"
CSRF_COOKIE = "__Host-csrf"


class NoFollow(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        return None


OPENER = urllib.request.build_opener(NoFollow)
JAR: dict[str, str] = {}


def send(
    method: str, url: str, body: dict[str, object] | None = None, *, csrf: bool = False
) -> dict[str, object]:
    headers = {"Cookie": "; ".join(f"{k}={v}" for k, v in JAR.items())} if JAR else {}
    if csrf and CSRF_COOKIE in JAR:
        headers["X-CSRF-Token"] = JAR[CSRF_COOKIE]
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
    try:
        reply = OPENER.open(request, timeout=30)
    except urllib.error.HTTPError as error:
        reply = error
    for line in reply.headers.get_all("Set-Cookie") or []:
        parsed: http.cookies.SimpleCookie = http.cookies.SimpleCookie(line)
        for name, morsel in parsed.items():
            if morsel.value and morsel["max-age"] != "0":
                JAR[name] = morsel.value
            else:
                JAR.pop(name, None)  # a cookie that is cleared goes away, as in a browser
    text = reply.read().decode(errors="replace")
    try:
        parsed_body: object = json.loads(text) if text else None
    except ValueError:
        parsed_body = text[:300]
    return {
        "status": reply.status if hasattr(reply, "status") else reply.code,
        "location": reply.headers.get("Location"),
        "body": parsed_body,
        "cookies": sorted(JAR),
    }


def back_to_instance(location: str) -> str:
    """The provider sends the browser to the public address; the loopback is where it is served."""
    assert location.startswith(PUBLIC), location
    return LOCAL + location[len(PUBLIC) :]


def pass_through_provider(start: str) -> dict[str, object]:
    """Follow `start` (the address of the provider's page) and come back to the callback."""
    seen = send("GET", start)  # the provider's own cookies do not matter: it keeps none here
    assert seen["status"] == 302, seen
    answer = send("GET", back_to_instance(str(seen["location"])))
    answer["asked"] = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(start).query))
    return answer


def play(steps: list[list[object]]) -> list[dict[str, object]]:
    saw: list[dict[str, object]] = []
    for step in steps:
        kind = step[0]
        if kind == "login":
            first = send("GET", LOCAL + "/api/v1/auth/oidc/login")
            if first["status"] == 302:
                saw.append(pass_through_provider(str(first["location"])))
            else:
                saw.append(first)
        elif kind == "reauth":
            first = send("POST", LOCAL + "/api/v1/auth/oidc/reauth", {}, csrf=True)
            if first["status"] == 200:
                url = str(first["body"]["authorization_url"])  # type: ignore[index]
                saw.append(pass_through_provider(url))
            else:
                saw.append(first)
        elif kind == "request":  # ["request", method, path, body or null]
            body = step[3] if len(step) > 3 else None
            saw.append(
                send(str(step[1]), LOCAL + str(step[2]), dict(body) if body is not None else None, csrf=True)  # type: ignore[arg-type]
            )
        else:
            raise SystemExit(f"unknown step {step!r}")
    return saw


if __name__ == "__main__":
    print(json.dumps(play(json.loads(sys.argv[1])["steps"])))
