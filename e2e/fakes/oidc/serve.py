"""Simulated OpenID Connect provider for the E2E (M5 L03, EF-USR-004): the in-process fake of the
backend tests behind a socket.

Discovery, the keys and the token endpoint, as the fake of `backend/tests/fakes/oidc` answers them
(the keys are drawn when it starts; the client secret is drawn by the run that configures
Shelfhold and given to both, never written anywhere). `GET /authorize` stands for the person
signing in at the provider: it sends the browser straight back with a code for the subject named
by `FAKE_OIDC_SUB` (a person who is already linked, or not, as the scenario needs). Nothing but the
E2E network namespace is reached.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
from tests.fakes.oidc.provider import FakeOidcProvider

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from httpshell import make_server

PORT = int(os.environ.get("FAKE_OIDC_PORT", "9443"))
ISSUER = os.environ.get("FAKE_OIDC_ISSUER", f"https://idp.e2e.test:{PORT}")
PKI = Path(os.environ.get("FAKE_OIDC_PKI", "/pki"))
SUBJECT = os.environ.get("FAKE_OIDC_SUB", "e2e-subject")

provider = FakeOidcProvider(issuer=ISSUER)
provider.sub = SUBJECT
# the run draws the secret and gives the same one to the provider and to Shelfhold (the test
# fixture does, at each run); without it the provider keeps the one it drew
provider.client_secret = os.environ.get("FAKE_OIDC_CLIENT_SECRET") or provider.client_secret


def handle(
    method: str, target: str, headers: dict[str, str], body: bytes
) -> tuple[int, dict[str, str], bytes]:
    parts = urlsplit(target)
    if method == "GET" and parts.path == "/authorize":
        back = provider.authorize(f"{ISSUER}{target}")
        return 302, {"location": back}, b""
    if method == "GET" and parts.path == "/_fake/client":
        info = {"client_id": provider.client_id, "client_secret": provider.client_secret}
        return 200, {"content-type": "application/json"}, json.dumps(info).encode()
    request = httpx2.Request(method, f"{ISSUER}{target}", headers=headers, content=body)
    answer = provider._handle(request)
    kind = {"content-type": answer.headers.get("content-type", "application/json")}
    return answer.status_code, kind, answer.content


if __name__ == "__main__":
    # TLS, as a provider is: the stack trusts the test authority of the run and nothing else
    tls = (str(PKI / "server.pem"), str(PKI / "server.key")) if PKI.is_dir() else None
    make_server("0.0.0.0", PORT, handle, "[fake-oidc]", tls).serve_forever()  # noqa: S104
