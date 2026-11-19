"""Signed "this browser already logged in to this account" cookie (review PR #41, ENF-SEC-008).

It only lets its holder skip the per-account attempt counter (never the per-address one), so a
third party flooding an account cannot keep its owner out. HMAC-SHA256 under a random key kept in
`/data/device-cookie.key` (0600, created on first use); whoever reads `/data` owns the instance
anyway. Value: `<base64url payload>.<base64url mac>`, payload `{"u": <casefolded username>,
"iat": <unix seconds>}`; valid for one year.

The same key also signs the proofs of `/lite` (M5 L17a, EF-LEC-010; `LiteCookieSigner`): a session
bound to the secret of one device token, and the form tokens that stand for the CSRF check. Each
kind of value is MACed under its own label, so none of them is worth anything as another.
"""

import base64
import contextlib
import hashlib
import hmac
import json
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path

from shelfhold.identity.application.ports import LiteClaims
from shelfhold.infrastructure.secrets.atomic import write_private

KEY_FILE = "device-cookie.key"
MAX_AGE = timedelta(days=365)
_MAX_VALUE = 512


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class _KeyedSigner:
    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / KEY_FILE
        self._key: bytes | None = None
        self._lock = threading.Lock()

    def _secret(self) -> bytes:
        with self._lock:
            if self._key is None:
                with contextlib.suppress(FileExistsError):  # another process made it first
                    write_private(self._path, secrets.token_bytes(32), overwrite=False)
                self._key = self._path.read_bytes()
            return self._key


class DeviceCookieSigner(_KeyedSigner):
    def _mac(self, payload: str) -> str:
        return _b64(hmac.new(self._secret(), payload.encode("ascii"), hashlib.sha256).digest())

    def issue(self, username: str, now: datetime) -> str:
        body = json.dumps({"u": username.casefold(), "iat": int(now.timestamp())})
        payload = _b64(body.encode("utf-8"))
        return f"{payload}.{self._mac(payload)}"

    def verify(self, value: str | None, username: str, now: datetime) -> bool:
        if not value or len(value) > _MAX_VALUE or value.count(".") != 1:
            return False
        payload, mac = value.split(".")
        try:
            if not hmac.compare_digest(mac.encode("ascii"), self._mac(payload).encode("ascii")):
                return False
            claims = json.loads(_unb64(payload))
        except (ValueError, UnicodeError):
            return False
        if not isinstance(claims, dict) or claims.get("u") != username.casefold():
            return False
        issued = claims.get("iat")
        if not isinstance(issued, int):
            return False
        age = now.timestamp() - issued
        return 0 <= age <= MAX_AGE.total_seconds()


LITE_MAX_AGE = timedelta(days=30)
_MAX_LITE_VALUE = 512
_MAX_NONCE = 64


class LiteCookieSigner(_KeyedSigner):
    """The proofs of `/lite`. The session cookie is `<payload>.<mac>`: payload `{"t": <public
    identifier of the token>, "b": <binding>, "iat": <unix seconds>}`, where the binding is a
    keyed digest of the stored digest of the token's secret, and a random `j` that tells one session
    from another. It holds nothing that opens a door by
    itself: the server reads the token again at each request and compares the binding, so a
    revoked, retired, expired or **rotated** token ends the session at the next request. Valid for
    `MAX_AGE` at most."""

    MAX_AGE = LITE_MAX_AGE

    def _keyed(self, label: str, text: str) -> str:
        message = label.encode("ascii") + b"\0" + text.encode("utf-8")
        return _b64(hmac.new(self._secret(), message, hashlib.sha256).digest())

    def issue(self, public_id: str, secret_digest: str, now: datetime) -> str:
        body = {
            "t": public_id,
            "b": self._keyed("lite-bind", secret_digest),
            "iat": int(now.timestamp()),
            "j": _b64(secrets.token_bytes(9)),  # one session is not another, even in one second
        }
        payload = _b64(json.dumps(body).encode("utf-8"))
        return f"{payload}.{self._keyed('lite-session', payload)}"

    def verify(self, value: str | None, now: datetime) -> LiteClaims | None:
        if not value or len(value) > _MAX_LITE_VALUE or value.count(".") != 1:
            return None
        payload, mac = value.split(".")
        try:
            if not hmac.compare_digest(
                mac.encode("ascii"), self._keyed("lite-session", payload).encode("ascii")
            ):
                return None
            claims = json.loads(_unb64(payload))
        except (ValueError, UnicodeError):
            return None
        if not isinstance(claims, dict):
            return None
        public_id, binding, issued = claims.get("t"), claims.get("b"), claims.get("iat")
        if not isinstance(public_id, str) or not isinstance(binding, str):
            return None
        if not isinstance(issued, int) or isinstance(issued, bool):
            return None
        if not 0 <= now.timestamp() - issued <= LITE_MAX_AGE.total_seconds():
            return None
        return LiteClaims(public_id, binding)

    def bound(self, claims: LiteClaims, secret_digest: str) -> bool:
        wanted = self._keyed("lite-bind", secret_digest)
        return hmac.compare_digest(claims.binding.encode("utf-8"), wanted.encode("ascii"))

    def new_flow(self) -> tuple[str, str]:
        """A nonce for the cookie of a browser that opens the sign-in form, and the token the
        form carries: only this browser holds the nonce, so only it can send the form back."""
        nonce = _b64(secrets.token_bytes(16))
        return nonce, self._keyed("lite-flow", nonce)

    def flow_ok(self, nonce: str | None, token: str | None) -> bool:
        if not nonce or not token or len(nonce) > _MAX_NONCE or len(token) > _MAX_NONCE:
            return False
        return hmac.compare_digest(
            token.encode("utf-8"), self._keyed("lite-flow", nonce).encode("ascii")
        )

    def csrf_of(self, session: str) -> str:
        return self._keyed("lite-csrf", session)

    def csrf_ok(self, session: str | None, token: str | None) -> bool:
        if not session or not token or len(session) > _MAX_LITE_VALUE or len(token) > _MAX_NONCE:
            return False
        return hmac.compare_digest(token.encode("utf-8"), self.csrf_of(session).encode("ascii"))
